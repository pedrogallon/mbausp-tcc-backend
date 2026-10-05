#!/usr/bin/env python3
"""Fetch TCC load-test metrics from CloudWatch and write a CSV.

Throughput and processing time come from the app counters in
ECS/AWSOTel/Application. Each series is one ECS task, so those queries SEARCH
the full OpenTelemetry dimension set and aggregate across tasks.

Latency percentiles (p50/p95/p99) come from the ALB (TargetResponseTime) for
request. For event, tcc.events.processing.time is exported to CloudWatch only
as Average (OTEL/ECS); p50/p95/p99 stats are zero and are not used. A target
response of 15s or more is counted as an error from that same ALB distribution
(TC(15:)). Service failures come from tcc.*.processed.error and from
target/ELB 5xx. CPU and memory per task come from Container Insights.
Cost is an on-demand estimate for the window: Fargate task-time plus SQS API
requests. Prices are South America (Sao Paulo), AWS Price List, 2026-10-03.
"""

import argparse
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

REGION = "sa-east-1"
PERIOD_SECONDS = 60
LOCAL_TZ = timezone(timedelta(hours=-3))
RESULTS_DIR = Path(__file__).resolve().parent / "results"

OTEL_SCHEMA = (
    "{ECS/AWSOTel/Application,ClusterARN,aws.log.stream.arns,telemetry.sdk.language,"
    "TaskDefinitionRevision,service.name,TaskDefinitionFamily,telemetry.sdk.version,"
    "aws.ecs.task.id,LaunchType,telemetry.sdk.name,TaskARN}"
)
TASK_SCHEMA = "{ECS/ContainerInsights,ClusterName,ServiceName,TaskId}"

CLUSTER = "ecs-1"
ALB_DIMENSION = "app/lb-3/db932d36b85127dd"
SQS_QUEUE = "processing-queue-2"
REQUEST_SERVICE = "backend-request-service"
EVENT_SERVICE = "backend-event-service-2"

# On-demand, Linux/x86, South America (Sao Paulo).
# Fargate: $0.0696 per vCPU-hour, $0.0076 per GB-hour.
# SQS standard, tier 1: $0.40 per million requests.
FARGATE_VCPU_HOUR_USD = 0.0696
FARGATE_GB_HOUR_USD = 0.0076
SQS_USD_PER_MILLION = 0.40
TASK_VCPU = 1.0
TASK_MEMORY_GB = 3.0
TASK_MEMORY_MIB = 3072.0
TASK_HOUR_USD = TASK_VCPU * FARGATE_VCPU_HOUR_USD + TASK_MEMORY_GB * FARGATE_GB_HOUR_USD
SLOW_THRESHOLD_SECONDS = 15

TASK_ID_RE = re.compile(r"\b([0-9a-f]{32})\b")

CSV_FIELDS = [
    "record_type",
    "minute",
    "workload",
    "task_id",
    "requests_processed_per_s",
    "events_processed_per_s",
    "alb_requests_per_s",
    "sqs_sent_per_s",
    "sqs_received_per_s",
    "sqs_deleted_per_s",
    "sqs_empty_receives",
    "sqs_api_requests",
    "alb_latency_p50_ms",
    "alb_latency_p95_ms",
    "alb_latency_p99_ms",
    "requests_processing_avg_ms",
    "events_processing_avg_ms",
    "requests_app_error_count",
    "requests_app_error_rate",
    "events_app_error_count",
    "events_app_error_rate",
    "requests_target_5xx_count",
    "requests_elb_5xx_count",
    "requests_slow_15s_count",
    "requests_slow_15s_rate",
    "requests_error_count",
    "requests_error_rate",
    "slow_threshold_seconds",
    "request_running_tasks",
    "event_running_tasks",
    "request_cpu_units_avg",
    "request_cpu_percent_avg",
    "request_memory_mib_avg",
    "request_memory_percent_avg",
    "request_ecs_cpu_util_avg",
    "request_ecs_memory_util_avg",
    "event_cpu_units_avg",
    "event_cpu_percent_avg",
    "event_memory_mib_avg",
    "event_memory_percent_avg",
    "event_ecs_cpu_util_avg",
    "event_ecs_memory_util_avg",
    "cpu_units",
    "cpu_percent",
    "memory_mib",
    "memory_percent",
    "fargate_cost_usd",
    "sqs_cost_usd",
    "total_cost_usd",
    "fargate_vcpu_hour_usd",
    "fargate_gb_hour_usd",
    "task_vcpu",
    "task_memory_gb",
    "sqs_usd_per_million",
]


def otel_agg(wrapper, metric, family, stat):
    return (
        f"{wrapper}(SEARCH('{OTEL_SCHEMA} MetricName=\"{metric}\" "
        f"TaskDefinitionFamily=\"{family}\"', '{stat}', {PERIOD_SECONDS}))"
    )


def task_search(metric, service):
    return (
        f"SEARCH('{TASK_SCHEMA} MetricName=\"{metric}\" ClusterName=\"{CLUSTER}\" "
        f"ServiceName=\"{service}\"', 'Average', {PERIOD_SECONDS})"
    )


def alb_metric(name):
    return {
        "Namespace": "AWS/ApplicationELB",
        "MetricName": name,
        "Dimensions": [{"Name": "LoadBalancer", "Value": ALB_DIMENSION}],
    }


def sqs_metric(name):
    return {
        "Namespace": "AWS/SQS",
        "MetricName": name,
        "Dimensions": [{"Name": "QueueName", "Value": SQS_QUEUE}],
    }


def running_tasks_metric(service):
    return {
        "Namespace": "ECS/ContainerInsights",
        "MetricName": "RunningTaskCount",
        "Dimensions": [
            {"Name": "ClusterName", "Value": CLUSTER},
            {"Name": "ServiceName", "Value": service},
        ],
    }


def ecs_service_metric(name, service):
    return {
        "Namespace": "AWS/ECS",
        "MetricName": name,
        "Dimensions": [
            {"Name": "ClusterName", "Value": CLUSTER},
            {"Name": "ServiceName", "Value": service},
        ],
    }


# id -> query. "multi" marks SEARCH expressions that return one series per task.
QUERIES = {
    "requests_processed": {"expression": otel_agg("SUM", "tcc.requests.processed.total", "tf-request", "Sum")},
    "events_processed": {"expression": otel_agg("SUM", "tcc.events.processed.total", "tf-event", "Sum")},
    "requests_app_errors": {"expression": otel_agg("SUM", "tcc.requests.processed.error", "tf-request", "Sum")},
    "events_app_errors": {"expression": otel_agg("SUM", "tcc.events.processed.error", "tf-event", "Sum")},
    "requests_processing": {"expression": otel_agg("AVG", "tcc.requests.processing.time", "tf-request", "Average")},
    "events_processing": {"expression": otel_agg("AVG", "tcc.events.processing.time", "tf-event", "Average")},
    "alb_requests": {"metric": alb_metric("RequestCount"), "stat": "Sum"},
    "alb_p50": {"metric": alb_metric("TargetResponseTime"), "stat": "p50"},
    "alb_p95": {"metric": alb_metric("TargetResponseTime"), "stat": "p95"},
    "alb_p99": {"metric": alb_metric("TargetResponseTime"), "stat": "p99"},
    "alb_fast": {"metric": alb_metric("TargetResponseTime"), "stat": "TC(:15)"},
    "alb_slow": {"metric": alb_metric("TargetResponseTime"), "stat": "TC(15:)"},
    "alb_target_5xx": {"metric": alb_metric("HTTPCode_Target_5XX_Count"), "stat": "Sum"},
    "alb_elb_5xx": {"metric": alb_metric("HTTPCode_ELB_5XX_Count"), "stat": "Sum"},
    "sqs_sent": {"metric": sqs_metric("NumberOfMessagesSent"), "stat": "Sum"},
    "sqs_received": {"metric": sqs_metric("NumberOfMessagesReceived"), "stat": "Sum"},
    "sqs_deleted": {"metric": sqs_metric("NumberOfMessagesDeleted"), "stat": "Sum"},
    "sqs_empty": {"metric": sqs_metric("NumberOfEmptyReceives"), "stat": "Sum"},
    "request_tasks": {"metric": running_tasks_metric(REQUEST_SERVICE), "stat": "Average"},
    "event_tasks": {"metric": running_tasks_metric(EVENT_SERVICE), "stat": "Average"},
    "request_ecs_cpu": {"metric": ecs_service_metric("CPUUtilization", REQUEST_SERVICE), "stat": "Average"},
    "request_ecs_memory": {"metric": ecs_service_metric("MemoryUtilization", REQUEST_SERVICE), "stat": "Average"},
    "event_ecs_cpu": {"metric": ecs_service_metric("CPUUtilization", EVENT_SERVICE), "stat": "Average"},
    "event_ecs_memory": {"metric": ecs_service_metric("MemoryUtilization", EVENT_SERVICE), "stat": "Average"},
    "request_cpu": {"expression": task_search("CpuUtilized", REQUEST_SERVICE), "multi": True},
    "request_memory": {"expression": task_search("MemoryUtilized", REQUEST_SERVICE), "multi": True},
    "event_cpu": {"expression": task_search("CpuUtilized", EVENT_SERVICE), "multi": True},
    "event_memory": {"expression": task_search("MemoryUtilized", EVENT_SERVICE), "multi": True},
}


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch CloudWatch metrics for a TCC load-test window.")
    parser.add_argument("--minutes", type=int, default=30, help="Lookback when --start/--end are omitted (default: 30).")
    parser.add_argument("--start", help="Window start, ISO-8601. Example: 2026-10-03T21:00:00-03:00")
    parser.add_argument("--end", help="Window end, ISO-8601. Default: now.")
    parser.add_argument("--output", help="CSV output path. Default: results/cloudwatch-metrics-<utc-now>.csv")
    return parser.parse_args()


def parse_time(value, default):
    if not value:
        return default
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def floor_minute(timestamp):
    timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.replace(second=0, microsecond=0)


def build_queries():
    queries = []
    for query_id, spec in QUERIES.items():
        if "expression" in spec:
            queries.append({"Id": query_id, "Expression": spec["expression"], "ReturnData": True})
            continue
        queries.append(
            {
                "Id": query_id,
                "MetricStat": {
                    "Metric": spec["metric"],
                    "Period": PERIOD_SECONDS,
                    "Stat": spec["stat"],
                },
                "ReturnData": True,
            }
        )
    return queries


def fetch(start, end):
    client = boto3.client("cloudwatch", region_name=REGION)
    single = {query_id: {} for query_id, spec in QUERIES.items() if not spec.get("multi")}
    multi = {query_id: {} for query_id, spec in QUERIES.items() if spec.get("multi")}
    messages = []
    next_token = None

    while True:
        kwargs = {
            "MetricDataQueries": build_queries(),
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = client.get_metric_data(**kwargs)
        messages.extend(response.get("Messages") or [])
        for result in response.get("MetricDataResults", []):
            query_id = result["Id"]
            stamps = [floor_minute(ts) for ts in result.get("Timestamps", [])]
            values = result.get("Values", [])
            if query_id in multi:
                label = result.get("Label") or query_id
                bucket = multi[query_id].setdefault(label, {})
            else:
                bucket = single[query_id]
            for timestamp, value in zip(stamps, values):
                bucket[timestamp] = value
        next_token = response.get("NextToken")
        if not next_token:
            return single, multi, messages


def task_id_from_label(label):
    match = TASK_ID_RE.search(label or "")
    return match.group(1) if match else (label or "")


def index_tasks(multi, cpu_id, memory_id):
    """Map task id -> {'cpu': {minute: units}, 'memory': {minute: MiB}}."""
    tasks = {}
    for kind, query_id in (("cpu", cpu_id), ("memory", memory_id)):
        for label, points in multi.get(query_id, {}).items():
            if not points:
                continue
            tasks.setdefault(task_id_from_label(label), {}).setdefault(kind, {}).update(points)
    return tasks


def mask_idle_timers(single):
    """Drop processing-time samples from minutes with no completed work.

    Stopped tasks keep publishing the last timer average, which is not the
    processing time of that minute.
    """
    for timer_id, counter_id in (
        ("requests_processing", "requests_processed"),
        ("events_processing", "events_processed"),
    ):
        for minute in list(single[timer_id]):
            if not single[counter_id].get(minute):
                single[timer_id][minute] = None


def mean(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def per_second(count):
    if count is None:
        return None
    return count / PERIOD_SECONDS


def seconds_to_ms(value):
    if value is None:
        return None
    return value * 1000.0


def cpu_percent(units):
    if units is None:
        return None
    return units / (TASK_VCPU * 1024.0) * 100.0


def memory_percent(mib):
    if mib is None:
        return None
    return mib / TASK_MEMORY_MIB * 100.0


def minute_resource(tasks, minute, kind):
    return mean(resources.get(kind, {}).get(minute) for resources in tasks.values())


def sqs_api_requests(single, minute):
    parts = [single["sqs_sent"].get(minute), single["sqs_received"].get(minute), single["sqs_deleted"].get(minute), single["sqs_empty"].get(minute)]
    if all(part is None for part in parts):
        return None
    return sum(part or 0.0 for part in parts)


def fargate_cost(single, minute):
    request_tasks = single["request_tasks"].get(minute)
    event_tasks = single["event_tasks"].get(minute)
    if request_tasks is None and event_tasks is None:
        return None
    tasks = (request_tasks or 0.0) + (event_tasks or 0.0)
    return tasks * TASK_HOUR_USD * (PERIOD_SECONDS / 3600.0)


def sqs_cost(api_requests):
    if api_requests is None:
        return None
    return api_requests * SQS_USD_PER_MILLION / 1_000_000.0


def count_or_zero(value, traffic):
    if value is None and traffic:
        return 0.0
    return value


def request_errors(single, minute):
    """Service failures plus responses that took 15s or more.

    tcc.requests.processed.error and target 5xx describe the same failed
    request when the exception is turned into an HTTP 500, so the larger of
    the two is kept. ELB 5xx never produced a target response. TC(15:) counts
    target responses at or above 15 seconds, including a slow 5xx, so that
    overlap can push the rate slightly up.
    """
    processed = single["requests_processed"].get(minute) or 0.0
    alb_requests = single["alb_requests"].get(minute)
    traffic = bool(processed or alb_requests)
    app_errors = count_or_zero(single["requests_app_errors"].get(minute), traffic)
    target_5xx = count_or_zero(single["alb_target_5xx"].get(minute), traffic)
    elb_5xx = count_or_zero(single["alb_elb_5xx"].get(minute), alb_requests)
    slow = count_or_zero(single["alb_slow"].get(minute), alb_requests is not None)
    if app_errors is None and target_5xx is None and elb_5xx is None and slow is None:
        return None

    app_errors = app_errors or 0.0
    target_5xx = target_5xx or 0.0
    elb_5xx = elb_5xx or 0.0
    slow = slow or 0.0
    fast = single["alb_fast"].get(minute)
    slow_denominator = None
    if fast is not None or single["alb_slow"].get(minute) is not None:
        slow_denominator = (fast or 0.0) + slow
    elif alb_requests:
        slow_denominator = alb_requests

    error_count = max(app_errors, target_5xx) + elb_5xx + slow
    denominator = alb_requests or processed or 0.0
    error_rate = min(1.0, error_count / denominator) if denominator else None
    app_rate = (app_errors / processed) if processed else None
    slow_rate = (slow / slow_denominator) if slow_denominator else None
    return {
        "app_errors": app_errors if traffic else None,
        "app_rate": app_rate,
        "target_5xx": target_5xx if traffic else None,
        "elb_5xx": elb_5xx if alb_requests is not None else None,
        "slow": slow if alb_requests is not None else None,
        "slow_rate": slow_rate,
        "error_count": error_count if denominator else None,
        "error_rate": error_rate,
    }


def event_errors(single, minute):
    processed = single["events_processed"].get(minute) or 0.0
    app_errors = count_or_zero(single["events_app_errors"].get(minute), processed > 0)
    if app_errors is None:
        return None, None
    if not processed:
        return None, None
    return app_errors, app_errors / processed


def collect_minutes(single, request_tasks, event_tasks):
    minutes = set()
    for points in single.values():
        minutes.update(points)
    for tasks in (request_tasks, event_tasks):
        for resources in tasks.values():
            for points in resources.values():
                minutes.update(points)
    return sorted(minutes)


def fmt(value, digits=4):
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def blank_row():
    return {field: "" for field in CSV_FIELDS}


def meta_row():
    row = blank_row()
    row["record_type"] = "meta"
    row["fargate_vcpu_hour_usd"] = f"{FARGATE_VCPU_HOUR_USD:.4f}"
    row["fargate_gb_hour_usd"] = f"{FARGATE_GB_HOUR_USD:.4f}"
    row["task_vcpu"] = f"{TASK_VCPU:.0f}"
    row["task_memory_gb"] = f"{TASK_MEMORY_GB:.0f}"
    row["sqs_usd_per_million"] = f"{SQS_USD_PER_MILLION:.2f}"
    row["slow_threshold_seconds"] = str(SLOW_THRESHOLD_SECONDS)
    return row


def minute_row(minute, single, request_tasks, event_tasks):
    request_cpu = minute_resource(request_tasks, minute, "cpu")
    request_mem = minute_resource(request_tasks, minute, "memory")
    event_cpu = minute_resource(event_tasks, minute, "cpu")
    event_mem = minute_resource(event_tasks, minute, "memory")
    api_requests = sqs_api_requests(single, minute)
    fargate = fargate_cost(single, minute)
    sqs = sqs_cost(api_requests)
    total = None
    if fargate is not None or sqs is not None:
        total = (fargate or 0.0) + (sqs or 0.0)
    http_errors = request_errors(single, minute) or {}
    event_error_count, event_error_rate = event_errors(single, minute) or (None, None)

    row = blank_row()
    row.update(
        {
            "record_type": "minute",
            "minute": minute.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S-03:00"),
            "requests_processed_per_s": fmt(per_second(single["requests_processed"].get(minute))),
            "events_processed_per_s": fmt(per_second(single["events_processed"].get(minute))),
            "alb_requests_per_s": fmt(per_second(single["alb_requests"].get(minute))),
            "sqs_sent_per_s": fmt(per_second(single["sqs_sent"].get(minute))),
            "sqs_received_per_s": fmt(per_second(single["sqs_received"].get(minute))),
            "sqs_deleted_per_s": fmt(per_second(single["sqs_deleted"].get(minute))),
            "sqs_empty_receives": fmt(single["sqs_empty"].get(minute), 0),
            "sqs_api_requests": fmt(api_requests, 0),
            "alb_latency_p50_ms": fmt(seconds_to_ms(single["alb_p50"].get(minute)), 2),
            "alb_latency_p95_ms": fmt(seconds_to_ms(single["alb_p95"].get(minute)), 2),
            "alb_latency_p99_ms": fmt(seconds_to_ms(single["alb_p99"].get(minute)), 2),
            "requests_processing_avg_ms": fmt(single["requests_processing"].get(minute), 2),
            "events_processing_avg_ms": fmt(single["events_processing"].get(minute), 2),
            "requests_app_error_count": fmt(http_errors.get("app_errors"), 0),
            "requests_app_error_rate": fmt(http_errors.get("app_rate"), 6),
            "events_app_error_count": fmt(event_error_count, 0),
            "events_app_error_rate": fmt(event_error_rate, 6),
            "requests_target_5xx_count": fmt(http_errors.get("target_5xx"), 0),
            "requests_elb_5xx_count": fmt(http_errors.get("elb_5xx"), 0),
            "requests_slow_15s_count": fmt(http_errors.get("slow"), 0),
            "requests_slow_15s_rate": fmt(http_errors.get("slow_rate"), 6),
            "requests_error_count": fmt(http_errors.get("error_count"), 0),
            "requests_error_rate": fmt(http_errors.get("error_rate"), 6),
            "request_running_tasks": fmt(single["request_tasks"].get(minute), 2),
            "event_running_tasks": fmt(single["event_tasks"].get(minute), 2),
            "request_cpu_units_avg": fmt(request_cpu, 2),
            "request_cpu_percent_avg": fmt(cpu_percent(request_cpu), 2),
            "request_memory_mib_avg": fmt(request_mem, 2),
            "request_memory_percent_avg": fmt(memory_percent(request_mem), 2),
            "request_ecs_cpu_util_avg": fmt(single["request_ecs_cpu"].get(minute), 2),
            "request_ecs_memory_util_avg": fmt(single["request_ecs_memory"].get(minute), 2),
            "event_cpu_units_avg": fmt(event_cpu, 2),
            "event_cpu_percent_avg": fmt(cpu_percent(event_cpu), 2),
            "event_memory_mib_avg": fmt(event_mem, 2),
            "event_memory_percent_avg": fmt(memory_percent(event_mem), 2),
            "event_ecs_cpu_util_avg": fmt(single["event_ecs_cpu"].get(minute), 2),
            "event_ecs_memory_util_avg": fmt(single["event_ecs_memory"].get(minute), 2),
            "fargate_cost_usd": fmt(fargate, 8),
            "sqs_cost_usd": fmt(sqs, 8),
            "total_cost_usd": fmt(total, 8),
        }
    )
    return row


def task_rows(minute, workload, tasks):
    rows = []
    local = minute.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    for task_id, resources in sorted(tasks.items()):
        cpu = resources.get("cpu", {}).get(minute)
        memory = resources.get("memory", {}).get(minute)
        if cpu is None and memory is None:
            continue
        row = blank_row()
        row.update(
            {
                "record_type": "task",
                "minute": local,
                "workload": workload,
                "task_id": task_id,
                "cpu_units": fmt(cpu, 2),
                "cpu_percent": fmt(cpu_percent(cpu), 2),
                "memory_mib": fmt(memory, 2),
                "memory_percent": fmt(memory_percent(memory), 2),
            }
        )
        rows.append(row)
    return rows


def write_csv(path, single, request_tasks, event_tasks):
    path.parent.mkdir(parents=True, exist_ok=True)
    minutes = collect_minutes(single, request_tasks, event_tasks)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerow(meta_row())
        for minute in minutes:
            writer.writerow(minute_row(minute, single, request_tasks, event_tasks))
            writer.writerows(task_rows(minute, "request", request_tasks))
            writer.writerows(task_rows(minute, "event", event_tasks))
    return len(minutes)


def active_values(points):
    return [value for value in points.values() if value and value > 0]


def print_report(start, end, csv_path, single, request_tasks, event_tasks):
    print(f"\n{'=' * 88}")
    print("CLOUDWATCH METRICS")
    print(f"{'=' * 88}")
    print(f"Window: {start.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S} -> {end.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S} (UTC-3)")
    print(f"CSV: {csv_path}")
    print()

    throughput = (
        ("requests_processed", "app requests/s", True),
        ("events_processed", "app events/s", True),
        ("alb_requests", "ALB req/s", True),
        ("sqs_sent", "SQS sent/s", True),
    )
    minutes = sorted({ts for key, _, _ in throughput for ts in single[key]})
    header = f"{'minute':<22}" + "".join(f"{label:>18}" for _, label, _ in throughput)
    print(header)
    print("-" * len(header))
    for minute in minutes:
        row = f"{minute.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M}   "
        for key, _, as_rate in throughput:
            value = single[key].get(minute)
            if value is None:
                row += f"{'':>18}"
            else:
                shown = value / PERIOD_SECONDS if as_rate else value
                row += f"{shown:17.2f} "
        print(row)

    print("\nLatency and processing (minutes with samples)")
    for key, label, scale in (
        ("alb_p50", "ALB p50 ms", 1000.0),
        ("alb_p95", "ALB p95 ms", 1000.0),
        ("alb_p99", "ALB p99 ms", 1000.0),
        ("requests_processing", "request processing avg ms", 1.0),
        ("events_processing", "event processing avg ms", 1.0),
    ):
        values = [value * scale for value in active_values(single[key])]
        if not values:
            print(f"  {label}: no data")
            continue
        print(f"  {label}: avg {mean(values):.1f}    peak {max(values):.1f}    minutes {len(values)}")

    print(f"\nErrors (response >= {SLOW_THRESHOLD_SECONDS}s counts as an error)")
    totals = {
        "app": 0.0,
        "target_5xx": 0.0,
        "elb_5xx": 0.0,
        "slow": 0.0,
        "errors": 0.0,
        "alb": 0.0,
        "processed": 0.0,
        "event_errors": 0.0,
        "events": 0.0,
    }
    for minute in collect_minutes(single, request_tasks, event_tasks):
        http_errors = request_errors(single, minute)
        if http_errors:
            totals["app"] += http_errors["app_errors"] or 0.0
            totals["target_5xx"] += http_errors["target_5xx"] or 0.0
            totals["elb_5xx"] += http_errors["elb_5xx"] or 0.0
            totals["slow"] += http_errors["slow"] or 0.0
            totals["errors"] += http_errors["error_count"] or 0.0
        totals["alb"] += single["alb_requests"].get(minute) or 0.0
        totals["processed"] += single["requests_processed"].get(minute) or 0.0
        event_error_count, _event_rate = event_errors(single, minute) or (None, None)
        totals["event_errors"] += event_error_count or 0.0
        totals["events"] += single["events_processed"].get(minute) or 0.0
    request_rate = (totals["errors"] / totals["alb"]) if totals["alb"] else None
    event_rate = (totals["event_errors"] / totals["events"]) if totals["events"] else None
    print(f"  request app errors: {totals['app']:.0f}")
    print(f"  request target 5xx: {totals['target_5xx']:.0f}")
    print(f"  request ELB 5xx: {totals['elb_5xx']:.0f}")
    print(f"  request >= {SLOW_THRESHOLD_SECONDS}s: {totals['slow']:.0f}")
    if request_rate is None:
        print("  request error rate: no data")
    else:
        print(f"  request error rate: {request_rate * 100:.4f}%  ({totals['errors']:.0f} / {totals['alb']:.0f})")
    if event_rate is None:
        print("  event error rate: no data")
    else:
        print(f"  event error rate: {event_rate * 100:.4f}%  ({totals['event_errors']:.0f} / {totals['events']:.0f})")

    print("\nTasks (window average)")
    for workload, tasks in (("request", request_tasks), ("event", event_tasks)):
        if not tasks:
            print(f"  {workload}: no task series")
            continue
        for task_id, resources in sorted(tasks.items()):
            cpu = mean(resources.get("cpu", {}).values())
            memory = mean(resources.get("memory", {}).values())
            cpu_text = f"{cpu_percent(cpu):.1f}%" if cpu is not None else "-"
            mem_text = f"{memory:.0f} MiB ({memory_percent(memory):.1f}%)" if memory is not None else "-"
            print(f"  {workload} {task_id}  cpu {cpu_text}  memory {mem_text}")

    minutes = collect_minutes(single, request_tasks, event_tasks)
    fargate_total = 0.0
    sqs_total = 0.0
    api_total = 0.0
    for minute in minutes:
        api_requests = sqs_api_requests(single, minute)
        fargate = fargate_cost(single, minute)
        sqs = sqs_cost(api_requests)
        fargate_total += fargate or 0.0
        sqs_total += sqs or 0.0
        api_total += api_requests or 0.0
    print("\nCost estimate (on-demand, sa-east-1)")
    print(f"  Fargate: ${fargate_total:.6f}   ({TASK_VCPU:.0f} vCPU + {TASK_MEMORY_GB:.0f} GB at ${TASK_HOUR_USD:.4f}/task-hour)")
    print(f"  SQS:     ${sqs_total:.6f}   ({api_total:.0f} API requests at ${SQS_USD_PER_MILLION:.2f}/million)")
    print(f"  Total:   ${fargate_total + sqs_total:.6f}")
    print(f"{'=' * 88}\n")


def iso_stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%f")[:-3] + "Z"


def main():
    args = parse_args()
    end = parse_time(args.end, datetime.now(timezone.utc))
    start = parse_time(args.start, end - timedelta(minutes=args.minutes))
    if start >= end:
        raise SystemExit("ERROR: --start must be before --end")

    single, multi, messages = fetch(start, end)
    for message in messages:
        print(f"CloudWatch: {message.get('Code')}: {message.get('Value')}")

    mask_idle_timers(single)
    request_tasks = index_tasks(multi, "request_cpu", "request_memory")
    event_tasks = index_tasks(multi, "event_cpu", "event_memory")
    if args.output:
        csv_path = Path(args.output)
        if not csv_path.is_absolute():
            csv_path = Path(__file__).resolve().parent / csv_path
    else:
        csv_path = RESULTS_DIR / f"cloudwatch-metrics-{iso_stamp(datetime.now(timezone.utc))}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_path, single, request_tasks, event_tasks)
    print_report(start, end, csv_path, single, request_tasks, event_tasks)


if __name__ == "__main__":
    main()
