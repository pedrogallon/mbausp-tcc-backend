#!/usr/bin/env python3
"""Gráficos por perfil a partir dos CSV CloudWatch em gold-new (+ resumo k6)."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "results" / "gold-new"
DEFAULT_OUTPUT = DEFAULT_INPUT / "charts"
PROFILES_FILE = ROOT / "k6_profiles.json"
CSV_RE = re.compile(r"^cloudwatch-(request|event)-(.+)\.csv$")
K6_RE = re.compile(r"^k6-(request|event)-(.+)-(\d{4}-\d{2}-\d{2}T.+)\.json$")

PROFILE_PT = {
    "steady": "carga constante (steady)",
    "spike": "pico (spike)",
    "long": "longa duração (long)",
    "warmup": "aquecimento (warmup)",
    "test": "teste",
}

REQUEST_COLOR = "#1f77b4"
EVENT_COLOR = "#ff7f0e"
COST_COLORS = {"fargate": "#4c78a8", "sqs": "#f58518"}
Y_PAD = 1.2

ZERO_FILL_KEYS = {
    "requests_processed_per_s",
    "events_processed_per_s",
    "alb_requests_per_s",
    "sqs_sent_per_s",
    "alb_latency_p50_ms",
    "alb_latency_p95_ms",
    "alb_latency_p99_ms",
    "requests_processing_avg_ms",
    "events_processing_avg_ms",
    "requests_error_rate",
    "events_app_error_rate",
    "requests_app_error_rate",
    "fargate_cost_usd",
    "sqs_cost_usd",
    "total_cost_usd",
    "k6_failed_rate_pct",
    "request_cpu_percent_avg",
    "event_cpu_percent_avg",
    "request_memory_percent_avg",
    "event_memory_percent_avg",
    "request_memory_mib_avg",
    "event_memory_mib_avg",
    "request_ecs_cpu_util_avg",
    "event_ecs_cpu_util_avg",
    "request_ecs_memory_util_avg",
    "event_ecs_memory_util_avg",
}

RESOURCE_CSV_FIELDS = [
    "kind",
    "minute",
    "minute_rel",
    "cpu_percent_avg",
    "memory_percent_avg",
    "memory_mib_avg",
    "running_tasks",
    "cpu_source",
    "memory_source",
]


def parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_duration(value: str) -> timedelta:
    value = (value or "0s").strip().lower()
    if value.endswith("ms"):
        return timedelta(milliseconds=float(value[:-2]))
    if value.endswith("s"):
        return timedelta(seconds=float(value[:-1]))
    if value.endswith("m"):
        return timedelta(minutes=float(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=float(value[:-1]))
    raise ValueError(value)


def load_profiles() -> dict:
    if not PROFILES_FILE.exists():
        return {}
    return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))


def run_length(profile_name: str, run: dict, profiles: dict) -> timedelta:
    ramp = run.get("ramp_up") or (profiles.get(profile_name) or {}).get("rampUp", "0s")
    duration = run.get("duration") or (profiles.get(profile_name) or {}).get("duration", "0s")
    return parse_duration(ramp) + parse_duration(duration)


def load_minute_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("record_type") != "minute":
                continue
            minute = datetime.fromisoformat(row["minute"])
            parsed = {"minute": minute}
            for key, value in row.items():
                if key in {"record_type", "minute", "workload", "task_id"}:
                    continue
                parsed[key] = parse_float(value)
            rows.append(parsed)
    rows.sort(key=lambda r: r["minute"])
    return rows


def fill_minute_grid(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    start = rows[0]["minute"].replace(second=0, microsecond=0)
    end = rows[-1]["minute"].replace(second=0, microsecond=0)
    by_minute = {r["minute"].replace(second=0, microsecond=0): r for r in rows}

    filled = []
    cursor = start
    while cursor <= end:
        if cursor in by_minute:
            row = dict(by_minute[cursor])
        else:
            row = {"minute": cursor}
            for key in ZERO_FILL_KEYS:
                row[key] = 0.0
        for key in ZERO_FILL_KEYS:
            if row.get(key) is None:
                row[key] = 0.0
        filled.append(row)
        cursor += timedelta(minutes=1)
    return filled


def relative_minutes(rows: list[dict], origin: datetime | None = None) -> np.ndarray:
    if not rows:
        return np.array([])
    start = origin or rows[0]["minute"]
    return np.array([(r["minute"] - start).total_seconds() / 60.0 for r in rows], dtype=float)


def first_k6_origin(
    kind: str,
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> datetime | None:
    """Origem do eixo X = início da 1ª execução k6 (ignora padding do CloudWatch)."""
    runs = k6_groups.get((kind, profile), [])
    starts = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        start, _ = resolve_run_window(run, profile, profiles, path)
        starts.append(start.replace(second=0, microsecond=0))
    return min(starts) if starts else None


def trim_rows_to_k6_window(
    rows: list[dict],
    kind: str,
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
    lead_minutes: int = 2,
    trail_minutes: int = 5,
) -> tuple[list[dict], datetime | None]:
    """Recorta a série para [1ª k6 − lead, última k6 + trail], alinhando falhas e AWS."""
    runs = k6_groups.get((kind, profile), [])
    if not runs or not rows:
        return rows, (rows[0]["minute"] if rows else None)

    starts = []
    ends = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        start, end = resolve_run_window(run, profile, profiles, path)
        starts.append(start)
        ends.append(end)

    origin = min(starts).replace(second=0, microsecond=0) - timedelta(minutes=lead_minutes)
    finish = max(ends).replace(second=0, microsecond=0) + timedelta(minutes=trail_minutes)
    trimmed = [r for r in rows if origin <= r["minute"] <= finish]
    return trimmed, origin


def series(rows: list[dict], key: str) -> np.ndarray:
    return np.array([row.get(key) if row.get(key) is not None else 0.0 for row in rows], dtype=float)


def discover_csvs(input_dir: Path) -> dict[str, dict[str, Path]]:
    found: dict[str, dict[str, Path]] = defaultdict(dict)
    for path in sorted(input_dir.glob("cloudwatch-*.csv")):
        match = CSV_RE.match(path.name)
        if not match:
            continue
        kind, profile = match.group(1), match.group(2)
        found[profile][kind] = path
    return dict(found)


K6_RE = re.compile(r"^k6-(request|event)-(.+)-(\d{4}-\d{2}-\d{2}T.+)\.json$")
FILENAME_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d+)?)Z\.json$")
LOCAL_TZ = timezone(timedelta(hours=-3))


def parse_filename_end(path: Path) -> datetime | None:
    """Timestamp do nome do arquivo = instante em que o handleSummary gravou (fim do teste)."""
    match = FILENAME_TS_RE.search(path.name)
    if not match:
        return None
    raw = match.group(1).replace("-", ":", 2)  # date keeps -, time uses :
    # raw like 2026-10-05T01:25:40.051 after fixing time separators only
    # filename is 2026-10-05T01-25-40.051Z — replace last three - in time part
    stamp = match.group(1)
    date_part, time_part = stamp.split("T", 1)
    time_part = time_part.replace("-", ":", 2)
    return datetime.fromisoformat(f"{date_part}T{time_part}+00:00").astimezone(LOCAL_TZ)


def resolve_run_window(run: dict, profile: str, profiles: dict, path: Path | None = None) -> tuple[datetime, datetime]:
    """Corrige start/end quando o k6 gravou start_time == end_time (bug do handleSummary).

    Nesse caso o timestamp do arquivo (e o start_time) refletem o FIM do teste.
    """
    length = run_length(profile, run, profiles)
    start_raw = run.get("start_time")
    end_raw = run.get("end_time")
    start = datetime.fromisoformat(start_raw) if start_raw else None
    end = datetime.fromisoformat(end_raw) if end_raw else None
    file_end = parse_filename_end(path) if path else None

    if start and end and start != end:
        return start, end

    # start == end (ou end ausente): tratar como fim e derivar início
    finish = file_end or end or start
    if finish is None:
        raise ValueError(f"sem timestamp utilizável em {path}")
    return finish - length, finish


def load_k6_summaries(input_dir: Path) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted(input_dir.glob("k6-*.json")):
        match = K6_RE.match(path.name)
        if not match:
            continue
        kind, profile = match.group(1), match.group(2)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        groups[(kind, profile)].append(data)
    return groups


def attach_k6_failure_rates(
    rows: list[dict],
    kind: str,
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> None:
    """Preenche k6_failed_rate_pct por minuto a partir do failed_rate de cada execução k6."""
    runs = k6_groups.get((kind, profile), [])
    windows = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        start, end = resolve_run_window(run, profile, profiles, path)
        rate_pct = (run.get("failed_rate") or 0.0) * 100.0
        windows.append((start, end, rate_pct))
        print(
            f"  k6 {kind}/{profile}: {start.isoformat()} -> {end.isoformat()} "
            f"falhas={rate_pct:.2f}%"
        )

    for row in rows:
        minute = row["minute"]
        minute_end = minute + timedelta(minutes=1)
        rate = 0.0
        for start, end, rate_pct in windows:
            # sobreposição do minuto CloudWatch com a janela k6
            if start < minute_end and end > minute:
                rate = rate_pct
                break
        row["k6_failed_rate_pct"] = rate


def load_meta_prices(path: Path) -> float:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("record_type") != "meta":
                continue
            vcpu = parse_float(row.get("task_vcpu")) or 1.0
            mem = parse_float(row.get("task_memory_gb")) or 3.0
            vcpu_price = parse_float(row.get("fargate_vcpu_hour_usd")) or 0.0696
            mem_price = parse_float(row.get("fargate_gb_hour_usd")) or 0.0076
            return vcpu * vcpu_price + mem * mem_price
    return 1.0 * 0.0696 + 3.0 * 0.0076


def attributed_costs(kind: str, rows: list[dict], task_hour_usd: float) -> dict[str, float]:
    fargate = 0.0
    sqs = 0.0
    minute_fraction = 60.0 / 3600.0
    for row in rows:
        if kind == "request":
            tasks = row.get("request_running_tasks") or 0.0
            fargate += tasks * task_hour_usd * minute_fraction
        else:
            tasks = row.get("event_running_tasks") or 0.0
            fargate += tasks * task_hour_usd * minute_fraction
            sqs += row.get("sqs_cost_usd") or 0.0
    return {"fargate": fargate, "sqs": sqs, "total": fargate + sqs}


def pad_ylim(ax, values=None, ymin=0.0):
    """Expande o eixo Y em 20% acima do máximo (evita linha sobre a legenda)."""
    if values is None:
        ymax = None
        for line in ax.get_lines():
            ydata = line.get_ydata()
            if len(ydata) == 0:
                continue
            local = float(np.nanmax(ydata))
            ymax = local if ymax is None else max(ymax, local)
        for container in getattr(ax, "containers", []):
            for patch in container:
                height = getattr(patch, "get_height", lambda: None)()
                if height is None:
                    continue
                local = float(height)
                ymax = local if ymax is None else max(ymax, local)
    else:
        arr = np.asarray(list(values), dtype=float)
        arr = arr[np.isfinite(arr)]
        ymax = float(np.max(arr)) if len(arr) else 0.0

    if ymax is None or not np.isfinite(ymax) or ymax <= 0:
        ax.set_ylim(ymin, 1.0)
        return
    ax.set_ylim(ymin, ymax * Y_PAD)


def style_axes(ax, title: str, ylabel: str, xlabel: str = "Minutos desde o início da janela"):
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xlabel(xlabel)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    pad_ylim(ax)


def profile_title(profile: str) -> str:
    return PROFILE_PT.get(profile, profile)


def plot_profile(
    profile: str,
    series_map: dict[str, list[dict]],
    out_dir: Path,
    origins: dict[str, datetime | None],
) -> Path:
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    fig.suptitle(
        f"Perfil: {profile_title(profile)} — request vs event (média por minuto)",
        fontsize=12,
    )

    ax_tp, ax_lat = axes[0]
    ax_proc, ax_fail = axes[1]
    ax_lat_event = ax_lat.twinx()

    lat_req_vals = []
    event_scale = 1000.0

    for kind, color, label in (
        ("request", REQUEST_COLOR, "request"),
        ("event", EVENT_COLOR, "event"),
    ):
        rows = series_map.get(kind)
        if not rows:
            continue
        origin = origins.get(kind) or rows[0]["minute"]
        x = relative_minutes(rows, origin)

        if kind == "request":
            tp = series(rows, "requests_processed_per_s")
            proc = series(rows, "requests_processing_avg_ms")
            p50 = series(rows, "alb_latency_p50_ms")
            p95 = series(rows, "alb_latency_p95_ms")
            p99 = series(rows, "alb_latency_p99_ms")
            lat_req_vals.extend([p50, p95, p99])
            ax_lat.plot(x, p50, color=color, linewidth=1.2, label=f"{label} ALB p50")
            ax_lat.plot(x, p95, color=color, linewidth=1.2, linestyle="--", label=f"{label} ALB p95")
            ax_lat.plot(x, p99, color=color, linewidth=1.2, linestyle=":", label=f"{label} ALB p99")
        else:
            tp = series(rows, "events_processed_per_s")
            proc = series(rows, "events_processing_avg_ms")
            # Escala visual ×1000 (0.4 → 400), mesmo teto do eixo request
            event_lat = series(rows, "events_processing_avg_ms") * event_scale
            ax_lat_event.plot(
                x,
                event_lat,
                color=color,
                linewidth=1.6,
                label=f"{label} tcc.events.processing.time (×{int(event_scale)})",
            )

        fail = series(rows, "k6_failed_rate_pct")
        ax_tp.plot(x, tp, color=color, linewidth=1.6, label=f"{label} — throughput")
        ax_proc.plot(x, proc, color=color, linewidth=1.6, label=f"{label} — processamento")
        ax_fail.plot(x, fail, color=color, linewidth=1.6, label=f"{label} — falhas k6")

    style_axes(ax_tp, "Throughput", "req/s ou eventos/s")

    ax_lat.set_title("Latência (request: ALB; event: processing.time ×1000)")
    ax_lat.set_ylabel("request ALB (ms)", color=REQUEST_COLOR)
    ax_lat_event.set_ylabel(f"event processing.time ×{int(event_scale)}", color=EVENT_COLOR)
    ax_lat.set_xlabel("Minutos desde o início da 1ª execução k6")
    ax_lat.grid(True, alpha=0.3)

    # Mesmo teto nos dois eixos = máximo do ALB request (+ padding)
    if lat_req_vals:
        req_max = float(np.nanmax(np.concatenate(lat_req_vals)))
        if not np.isfinite(req_max) or req_max <= 0:
            req_max = 1.0
        y_top = req_max * Y_PAD
        ax_lat.set_ylim(0.0, y_top)
        ax_lat_event.set_ylim(0.0, y_top)

    lines_l, labels_l = ax_lat.get_legend_handles_labels()
    lines_r, labels_r = ax_lat_event.get_legend_handles_labels()
    ax_lat.legend(lines_l + lines_r, labels_l + labels_r, loc="upper right", fontsize=7)

    style_axes(ax_proc, "Tempo médio de processamento (aplicação)", "ms")
    style_axes(ax_fail, "Taxa de falhas (k6, timeout 15s incluso)", "%")
    for ax in (ax_tp, ax_proc, ax_fail):
        ax.set_xlabel("Minutos desde o início da 1ª execução k6")

    out_path = out_dir / f"perfil-{profile}-metricas.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def pick_resource(row: dict, kind: str) -> dict:
    """CPU/memória do serviço correspondente; prefere AWS/ECS Utilization quando existir."""
    if kind == "request":
        ecs_cpu = row.get("request_ecs_cpu_util_avg")
        ecs_mem = row.get("request_ecs_memory_util_avg")
        ci_cpu = row.get("request_cpu_percent_avg")
        ci_mem = row.get("request_memory_percent_avg")
        mem_mib = row.get("request_memory_mib_avg")
        tasks = row.get("request_running_tasks")
    else:
        ecs_cpu = row.get("event_ecs_cpu_util_avg")
        ecs_mem = row.get("event_ecs_memory_util_avg")
        ci_cpu = row.get("event_cpu_percent_avg")
        ci_mem = row.get("event_memory_percent_avg")
        mem_mib = row.get("event_memory_mib_avg")
        tasks = row.get("event_running_tasks")

    if ecs_cpu is not None and ecs_cpu > 0:
        cpu, cpu_source = ecs_cpu, "AWS/ECS CPUUtilization"
    else:
        cpu, cpu_source = (ci_cpu or 0.0), "ContainerInsights CpuUtilized"

    if ecs_mem is not None and ecs_mem > 0:
        mem_pct, mem_source = ecs_mem, "AWS/ECS MemoryUtilization"
    else:
        mem_pct, mem_source = (ci_mem or 0.0), "ContainerInsights MemoryUtilized"

    return {
        "cpu_percent_avg": cpu or 0.0,
        "memory_percent_avg": mem_pct or 0.0,
        "memory_mib_avg": mem_mib or 0.0,
        "running_tasks": tasks or 0.0,
        "cpu_source": cpu_source,
        "memory_source": mem_source,
    }


def write_resources_csv(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    input_dir: Path,
) -> Path:
    out_path = input_dir / f"cloudwatch-recursos-{profile}.csv"
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESOURCE_CSV_FIELDS)
        writer.writeheader()
        for kind in ("request", "event"):
            rows = series_map.get(kind) or []
            origin = origins.get(kind) or (rows[0]["minute"] if rows else None)
            for row in rows:
                rel = (row["minute"] - origin).total_seconds() / 60.0 if origin else 0.0
                res = pick_resource(row, kind)
                writer.writerow(
                    {
                        "kind": kind,
                        "minute": row["minute"].isoformat(),
                        "minute_rel": f"{rel:.2f}",
                        "cpu_percent_avg": f"{res['cpu_percent_avg']:.4f}",
                        "memory_percent_avg": f"{res['memory_percent_avg']:.4f}",
                        "memory_mib_avg": f"{res['memory_mib_avg']:.4f}",
                        "running_tasks": f"{res['running_tasks']:.2f}",
                        "cpu_source": res["cpu_source"],
                        "memory_source": res["memory_source"],
                    }
                )
    return out_path


def plot_resources(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    out_dir: Path,
) -> Path:
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True, sharex=True)
    fig.suptitle(
        f"Perfil: {profile_title(profile)} — CPU e memória ECS (média por minuto)",
        fontsize=12,
    )
    ax_cpu, ax_mem = axes
    cpu_vals = []
    mem_vals = []

    for kind, color, label in (
        ("request", REQUEST_COLOR, "request"),
        ("event", EVENT_COLOR, "event"),
    ):
        rows = series_map.get(kind)
        if not rows:
            continue
        origin = origins.get(kind) or rows[0]["minute"]
        x = relative_minutes(rows, origin)
        cpu = np.array([pick_resource(r, kind)["cpu_percent_avg"] for r in rows], dtype=float)
        mem = np.array([pick_resource(r, kind)["memory_percent_avg"] for r in rows], dtype=float)
        cpu_vals.append(cpu)
        mem_vals.append(mem)
        ax_cpu.plot(x, cpu, color=color, linewidth=1.6, label=f"{label} — CPU %")
        ax_mem.plot(x, mem, color=color, linewidth=1.6, label=f"{label} — memória %")

    ax_cpu.set_title("Utilização média de CPU (ECS)")
    ax_cpu.set_ylabel("%")
    ax_cpu.grid(True, alpha=0.3)
    ax_cpu.legend(loc="upper right", fontsize=8)
    if cpu_vals:
        pad_ylim(ax_cpu, np.concatenate(cpu_vals), ymin=0.0)

    ax_mem.set_title("Utilização média de memória (ECS)")
    ax_mem.set_ylabel("%")
    ax_mem.set_xlabel("Minutos desde o início da 1ª execução k6")
    ax_mem.grid(True, alpha=0.3)
    ax_mem.legend(loc="upper right", fontsize=8)
    if mem_vals:
        pad_ylim(ax_mem, np.concatenate(mem_vals), ymin=0.0)

    out_path = out_dir / f"perfil-{profile}-recursos.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_cost_comparison(costs: dict[tuple[str, str], dict[str, float]], out_dir: Path) -> Path:
    labels = []
    fargate_vals = []
    sqs_vals = []
    for kind, profile in sorted(costs, key=lambda k: (k[1], k[0])):
        labels.append(f"{kind}\n{profile_title(profile)}")
        fargate_vals.append(costs[(kind, profile)]["fargate"])
        sqs_vals.append(costs[(kind, profile)]["sqs"])

    x = np.arange(len(labels))
    width = 0.55
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    ax.bar(x, fargate_vals, width, label="Fargate (ECS)", color=COST_COLORS["fargate"])
    ax.bar(x, sqs_vals, width, bottom=fargate_vals, label="SQS", color=COST_COLORS["sqs"])

    totals = [f + s for f, s in zip(fargate_vals, sqs_vals)]
    for i, total in enumerate(totals):
        ax.text(i, total, f"US$ {total:.3f}", ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("US$ (estimativa na janela)")
    ax.set_title("Custo operacional estimado por topologia e perfil de carga")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(loc="upper right")
    pad_ylim(ax, totals)

    out_path = out_dir / "comparacao-custo-aws.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_k6_summary_bars(k6_groups: dict[tuple[str, str], list[dict]], out_dir: Path) -> Path | None:
    if not k6_groups:
        return None

    keys = sorted(k6_groups, key=lambda k: (k[1], k[0]))
    labels = [f"{k}\n{profile_title(p)}" for k, p in keys]
    thr = []
    p95 = []
    fail = []
    for key in keys:
        runs = k6_groups[key]
        thr.append(
            np.mean(
                [
                    r.get("throughput_req_s")
                    if r.get("throughput_req_s") is not None
                    else r.get("throughput_events_s") or 0.0
                    for r in runs
                ]
            )
        )
        p95.append(np.mean([r.get("p95_ms") or 0.0 for r in runs]))
        fail.append(np.mean([(r.get("failed_rate") or 0.0) * 100.0 for r in runs]))

    fig, axes = plt.subplots(1, 3, figsize=(12, 4), constrained_layout=True)
    fig.suptitle("Resumo k6 (média das execuções gold-new)", fontsize=12)
    colors = [REQUEST_COLOR if k == "request" else EVENT_COLOR for k, _ in keys]

    axes[0].bar(labels, thr, color=colors)
    axes[0].set_title("Throughput")
    axes[0].set_ylabel("req/s ou eventos/s")
    axes[0].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[0], thr)

    axes[1].bar(labels, p95, color=colors)
    axes[1].set_title("Latência p95 (cliente k6)")
    axes[1].set_ylabel("ms")
    axes[1].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[1], p95)

    axes[2].bar(labels, fail, color=colors)
    axes[2].set_title("Taxa de falhas (k6)")
    axes[2].set_ylabel("%")
    axes[2].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[2], fail)

    for ax in axes:
        ax.grid(True, axis="y", alpha=0.3)

    out_path = out_dir / "resumo-k6-comparacao.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gráficos gold-new (CloudWatch + k6).")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    plt.rcParams.update({"font.size": 10})

    args = parse_args()
    input_dir = args.input if args.input.is_absolute() else ROOT / args.input
    out_dir = args.output if args.output.is_absolute() else ROOT / args.output
    out_dir.mkdir(parents=True, exist_ok=True)

    csvs = discover_csvs(input_dir)
    if not csvs:
        raise SystemExit(f"ERROR: nenhum cloudwatch-{{request|event}}-{{profile}}.csv em {input_dir}")

    profiles = load_profiles()
    k6_groups = load_k6_summaries(input_dir)
    written: list[Path] = []
    costs: dict[tuple[str, str], dict[str, float]] = {}

    print(
        "Nota: p50/p95/p99 de tcc.events.processing.time não estão disponíveis "
        "no CloudWatch (export OTEL só Average). Latência event usa a média."
    )

    for profile, kind_paths in sorted(csvs.items()):
        series_map: dict[str, list[dict]] = {}
        origins: dict[str, datetime | None] = {}
        for kind, path in kind_paths.items():
            rows = fill_minute_grid(load_minute_rows(path))
            attach_k6_failure_rates(rows, kind, profile, k6_groups, profiles)
            trimmed, origin = trim_rows_to_k6_window(rows, kind, profile, k6_groups, profiles)
            series_map[kind] = trimmed
            origins[kind] = origin
            costs[(kind, profile)] = attributed_costs(kind, rows, load_meta_prices(path))

            proc_key = "events_processing_avg_ms" if kind == "event" else "requests_processing_avg_ms"
            proc_vals = [r[proc_key] for r in trimmed if (r.get(proc_key) or 0) > 0]
            fail_peak = max((r.get("k6_failed_rate_pct") or 0.0) for r in trimmed) if trimmed else 0.0
            print(
                f"Carregado {path.name}: {len(trimmed)} min (recorte k6) | "
                f"processing.time amostras>0={len(proc_vals)} "
                f"média={np.mean(proc_vals) if proc_vals else 0:.3f}ms | "
                f"pico falhas k6={fail_peak:.2f}%"
            )

        written.append(plot_profile(profile, series_map, out_dir, origins))
        resource_csv = write_resources_csv(profile, series_map, origins, input_dir)
        print(f"CSV recursos: {resource_csv}")
        written.append(plot_resources(profile, series_map, origins, out_dir))

    written.append(plot_cost_comparison(costs, out_dir))

    k6_chart = plot_k6_summary_bars(k6_groups, out_dir)
    if k6_chart:
        written.append(k6_chart)

    print("\nCustos atribuídos ao workload:")
    for key in sorted(costs, key=lambda k: (k[1], k[0])):
        c = costs[key]
        print(
            f"  {key[0]}/{key[1]}: total=US$ {c['total']:.4f}  "
            f"fargate=US$ {c['fargate']:.4f}  sqs=US$ {c['sqs']:.4f}"
        )

    print("\nGráficos gerados:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
