#!/usr/bin/env python3
import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError, NoCredentialsError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT_DIR = Path(__file__).resolve().parent
PROFILES_FILE = ROOT_DIR / "profiles.json"
JSON_FILE = ROOT_DIR / "mock_data.json"


def ensure_aws_credentials_ready():
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "AWS credentials not found. Run 'aws login' or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY before starting the load test."
        )
    return credentials


class LoadTester:
    def __init__(self, kind: str, profile: dict):
        self.kind = kind
        self.profile = profile
        self.payload = json.loads(JSON_FILE.read_text(encoding="utf-8"))
        self.duration_seconds = int(profile["duration_seconds"])
        self.target_rps = int(profile["target_rps"])
        self.concurrency = self.target_rps
        self.timeout = int(profile["timeout"])
        self.lock = threading.Lock()
        self.stop_flag = False

        self.results = {
            "total_items": 0,
            "successful_items": 0,
            "failed_items": 0,
            "total_time": 0.0,
            "latencies": [],
            "status_codes": {},
            "error_messages": [],
        }

        ensure_aws_credentials_ready()

        if kind == "request":
            self.endpoint_url = profile["http"]["endpoint"]
            self.session = requests.Session()
            retry_strategy = Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        else:
            self.queue_url = profile["sqs"]["queue_url"]
            self.sqs_client = boto3.client("sqs", region_name="sa-east-1")

    def _send_request(self, index: int):
        start = time.time()
        try:
            response = self.session.post(
                self.endpoint_url,
                json=self.payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "loadtest/1.0",
                },
                timeout=self.timeout,
            )
            elapsed = time.time() - start
            with self.lock:
                self.results["total_items"] += 1
                self.results["latencies"].append(elapsed)
                self.results["total_time"] += elapsed
                if response.status_code not in self.results["status_codes"]:
                    self.results["status_codes"][response.status_code] = 0
                self.results["status_codes"][response.status_code] += 1
                if 200 <= response.status_code < 300:
                    self.results["successful_items"] += 1
                else:
                    self.results["failed_items"] += 1
                    self.results["error_messages"].append(
                        f"Request {index}: HTTP {response.status_code}"
                    )
        except requests.exceptions.Timeout:
            with self.lock:
                self.results["total_items"] += 1
                self.results["failed_items"] += 1
                self.results["error_messages"].append(f"Request {index}: TIMEOUT")
        except requests.exceptions.RequestException as exc:
            with self.lock:
                self.results["total_items"] += 1
                self.results["failed_items"] += 1
                self.results["error_messages"].append(f"Request {index}: {exc}")
        except Exception as exc:  # pragma: no cover
            with self.lock:
                self.results["total_items"] += 1
                self.results["failed_items"] += 1
                self.results["error_messages"].append(f"Request {index}: {exc}")

    def _send_event(self, index: int):
        start = time.time()
        try:
            message_body = json.dumps(self.payload)
            response = self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body,
                MessageAttributes={
                    "Source": {"StringValue": "LoadTester", "DataType": "String"},
                    "MessageNum": {"StringValue": str(index), "DataType": "String"},
                    "Timestamp": {
                        "StringValue": datetime.utcnow().isoformat(),
                        "DataType": "String",
                    },
                },
            )
            elapsed = time.time() - start
            with self.lock:
                self.results["total_items"] += 1
                self.results["latencies"].append(elapsed)
                self.results["total_time"] += elapsed
                self.results["successful_items"] += 1
                self.results["status_codes"]["SQS_OK"] = (
                    self.results["status_codes"].get("SQS_OK", 0) + 1
                )
                if response.get("MessageId"):
                    pass
        except ClientError as exc:
            with self.lock:
                self.results["total_items"] += 1
                self.results["failed_items"] += 1
                self.results["error_messages"].append(
                    f"Event {index}: {exc.response['Error']['Code']} - {exc.response['Error']['Message']}"
                )
        except Exception as exc:  # pragma: no cover
            with self.lock:
                self.results["total_items"] += 1
                self.results["failed_items"] += 1
                self.results["error_messages"].append(f"Event {index}: {exc}")

    def _dispatch_item(self, index: int):
        if self.kind == "request":
            self._send_request(index)
        else:
            self._send_event(index)

    def _snapshot_metrics(self):
        with self.lock:
            total = self.results["total_items"]
            successful = self.results["successful_items"]
            failed = self.results["failed_items"]
            latencies = list(self.results["latencies"])
            status_codes = dict(self.results["status_codes"])
            return total, successful, failed, latencies, status_codes

    def _log_progress(self, elapsed_seconds: float, next_log_seconds: float):
        total, successful, failed, latencies, status_codes = self._snapshot_metrics()
        throughput = (total / elapsed_seconds) if elapsed_seconds > 0 else 0.0
        if latencies:
            p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        else:
            p95 = 0.0

        if self.kind == "request":
            kind_label = "request"
        else:
            kind_label = "event"

        print(
            f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}Z] "
            f"progress {kind_label}: elapsed={elapsed_seconds:.0f}s / {next_log_seconds:.0f}s, "
            f"total={total}, success={successful}, failed={failed}, "
            f"throughput={throughput:.2f} items/s, p95={p95:.3f}s, status={status_codes}"
        )

    def run(self):
        label = "HTTP" if self.kind == "request" else "SQS"
        print(f"\n{'=' * 72}")
        print(f"{label} SUSTAINED LOAD TEST")
        print(f"{'=' * 72}")
        print(f"Kind: {self.kind}")
        if self.kind == "request":
            print(f"Endpoint: {self.endpoint_url}")
        else:
            print(f"Queue URL: {self.queue_url}")
        print(f"Profile: {self.profile['description']}")
        print(f"Duration: {self.duration_seconds}s ({self.duration_seconds // 60}m)")
        print(f"Target RPS: {self.target_rps}")
        print(f"Concurrency: {self.concurrency}")
        print(f"Payload size: {len(json.dumps(self.payload)) / 1024:.2f} KB")
        print(f"Expected total: ~{self.target_rps * self.duration_seconds}")
        print("Progress logs will be emitted every 5 minutes.")
        print(f"{'=' * 72}\n")

        start_time = time.time()
        end_time = start_time + self.duration_seconds
        index = 0
        threads = []
        next_log_checkpoint = 300

        while time.time() < end_time and not self.stop_flag:
            batch_start = time.time()
            for _ in range(self.target_rps):
                if time.time() >= end_time:
                    self.stop_flag = True
                    break
                index += 1
                while len([t for t in threads if t.is_alive()]) >= self.concurrency:
                    time.sleep(0.001)
                thread = threading.Thread(target=self._dispatch_item, args=(index,))
                thread.daemon = True
                thread.start()
                threads.append(thread)

            batch_elapsed = time.time() - batch_start
            if batch_elapsed < 1.0:
                time.sleep(1.0 - batch_elapsed)

            elapsed_seconds = time.time() - start_time
            if elapsed_seconds >= next_log_checkpoint:
                self._log_progress(elapsed_seconds, next_log_checkpoint)
                next_log_checkpoint += 300

        for thread in threads:
            thread.join()

        total_elapsed = time.time() - start_time
        self._log_progress(total_elapsed, self.duration_seconds)

        latencies = self.results["latencies"]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            min_latency = min(latencies)
            max_latency = max(latencies)
            p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        else:
            avg_latency = min_latency = max_latency = p95 = 0.0

        print(f"\n{'=' * 72}")
        print("RESULTS")
        print(f"{'=' * 72}")
        print(f"Total time: {total_elapsed:.2f}s")
        print(f"Actual throughput: {self.results['total_items'] / total_elapsed:.2f} items/s")
        print(f"Total item count: {self.results['total_items']}")
        print(f"Successful: {self.results['successful_items']}")
        print(f"Failed: {self.results['failed_items']}")
        if self.results["total_items"]:
            print(
                f"Success rate: {(self.results['successful_items'] / self.results['total_items']) * 100:.2f}%"
            )
        print("\nLatency (seconds):")
        print(f"  Average: {avg_latency:.3f}s")
        print(f"  Min: {min_latency:.3f}s")
        print(f"  Max: {max_latency:.3f}s")
        print(f"  P95: {p95:.3f}s")
        if self.results["status_codes"]:
            print("\nStatus codes:")
            for key, value in sorted(self.results["status_codes"].items()):
                print(f"  {key}: {value}")
        if self.results["error_messages"]:
            print("\nFirst errors:")
            for error in self.results["error_messages"][:10]:
                print(f"  - {error}")
        print(f"{'=' * 72}\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Unified sustained load test for request or event workloads"
    )
    parser.add_argument(
        "--type",
        dest="kind",
        choices=["request", "event"],
        required=True,
        help="Workload type: request or event",
    )
    parser.add_argument(
        "--profile",
        choices=["steady", "spike", "long", "test"],
        required=True,
        help="Load profile: steady, spike, or long",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        if not PROFILES_FILE.exists():
            raise FileNotFoundError(f"Profiles file not found: {PROFILES_FILE}")
        if not JSON_FILE.exists():
            raise FileNotFoundError(f"JSON payload file not found: {JSON_FILE}")

        with PROFILES_FILE.open("r", encoding="utf-8") as fh:
            profiles = json.load(fh)

        profile_name = args.profile
        if profile_name not in profiles["profiles"]:
            raise KeyError(f"Profile '{profile_name}' not found in {PROFILES_FILE}")

        profile = profiles["profiles"][profile_name]
        tester = LoadTester(kind=args.kind, profile=profile)
        tester.run()
    except (RuntimeError, FileNotFoundError, KeyError) as exc:
        raise SystemExit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()