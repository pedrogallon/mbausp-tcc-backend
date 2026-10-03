#!/usr/bin/env python3
"""
Scheduled Load Test Runner (PRODUCTION-READY)
Executes load tests in sequence at specified times.
Run this script around 00:50 to start tests at 01:00.

FEATURES:
- Pre-flight checks (all files/deps exist)
- Timeout protection on each test
- Graceful error handling (continues if test fails)
- Detailed logging (success/fail/timing/gaps)
- Summary report at end
"""

import subprocess
import time
import json
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path


class TestScheduler:
    def __init__(self):
        self.tests = [
            {"time": dt_time(18, 40), "type": "request", "profile": "steady", "expected_duration": 600},
            {"time": dt_time(18, 40), "type": "event", "profile": "steady", "expected_duration": 600},
            {"time": dt_time(18, 55), "type": "request", "profile": "spike", "expected_duration": 300},
            {"time": dt_time(18, 55), "type": "event", "profile": "spike", "expected_duration": 300},
            {"time": dt_time(19, 05), "type": "request", "profile": "long", "expected_duration": 1200},
            {"time": dt_time(19, 05), "type": "event", "profile": "long", "expected_duration": 1200},
        ]
        self.script_path = Path(__file__).parent / "load_test.py"
        self.log_file = Path(__file__).parent / "tests_schedule.log"
        self.profiles_path = Path(__file__).parent / "profiles.json"
        self.mock_data_path = Path(__file__).parent / "mock_data.json"

        self.results = []
        self.start_time = None

    def log(self, message):
        """Log message to console and file with error handling"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        print(log_msg, flush=True)

        try:
            with open(self.log_file, "a", encoding='utf-8') as f:
                f.write(log_msg + "\n")
        except Exception as e:
            print(f"[WARNING] Could not write to log file: {str(e)}", flush=True)

    def preflight_check(self):
        """Verify all prerequisites before starting"""
        self.log("=" * 70)
        self.log("PREFLIGHT CHECKS")
        self.log("=" * 70)

        checks = [
            ("load_test.py", self.script_path),
            ("profiles.json", self.profiles_path),
            ("mock_data.json", self.mock_data_path),
        ]

        all_ok = True
        for name, path in checks:
            if path.exists():
                self.log(f"[OK] {name} found")
            else:
                self.log(f"[FAIL] {name} NOT FOUND at {path}")
                all_ok = False

        # Verify profiles.json structure
        if self.profiles_path.exists():
            try:
                with open(self.profiles_path) as f:
                    profiles = json.load(f)
                required_profiles = ["steady", "spike", "long"]
                for prof in required_profiles:
                    if prof in profiles.get("profiles", {}):
                        self.log(f"[OK] Profile '{prof}' defined")
                    else:
                        self.log(f"[FAIL] Profile '{prof}' NOT defined in profiles.json")
                        all_ok = False
            except Exception as e:
                self.log(f"[FAIL] Error reading profiles.json: {str(e)}")
                all_ok = False

        # Check Python dependencies
        try:
            import requests
            import boto3
            self.log("[OK] Python dependencies (requests, boto3) available")
        except ImportError as e:
            self.log(f"[FAIL] Missing Python dependency: {str(e)}")
            all_ok = False

        self.log("")
        if not all_ok:
            raise RuntimeError("Preflight checks failed. Please fix errors above.")

        self.log("[OK] All preflight checks passed\n")

    def wait_until(self, target_time):
        """Wait until the target time with periodic logging"""
        while True:
            now = datetime.now().time()
            if now >= target_time:
                return

            # Calculate seconds until target time
            now_seconds = now.hour * 3600 + now.minute * 60 + now.second
            target_seconds = target_time.hour * 3600 + target_time.minute * 60 + target_time.second

            if target_seconds < now_seconds:
                # Target time is tomorrow
                seconds_until = (86400 - now_seconds) + target_seconds
            else:
                seconds_until = target_seconds - now_seconds

            # Log every 5 minutes or when close to target
            if seconds_until <= 60:
                self.log(f"  [WAIT] {seconds_until}s until start")
                time.sleep(min(10, seconds_until))
            else:
                time.sleep(30)

    def run_test(self, test_type, profile, timeout_sec=None):
        """Run a single test with timeout protection"""
        cmd = [
            "python",
            str(self.script_path),
            "--type", test_type,
            "--profile", profile
        ]

        self.log(f"Running: --type {test_type} --profile {profile}")

        # Calculate timeout: expected duration + 5 min buffer
        if timeout_sec is None:
            timeout_sec = 3600  # 1 hour max per test

        start = time.time()
        test_start_time = datetime.now()

        try:
            result = subprocess.run(
                cmd,
                check=False,
                cwd=str(self.script_path.parent),
                timeout=timeout_sec
            )

            elapsed = time.time() - start

            if result.returncode == 0:
                self.log(f"[PASS] {test_type} {profile} in {elapsed:.1f}s")
                self.results.append({
                    "test": f"{test_type} {profile}",
                    "status": "PASSED",
                    "duration": elapsed,
                    "timestamp": test_start_time.isoformat()
                })
                return True
            else:
                self.log(f"[FAIL] {test_type} {profile} returned code {result.returncode} after {elapsed:.1f}s")
                self.results.append({
                    "test": f"{test_type} {profile}",
                    "status": "FAILED",
                    "duration": elapsed,
                    "timestamp": test_start_time.isoformat(),
                    "error": f"Return code {result.returncode}"
                })
                return False

        except subprocess.TimeoutExpired:
            self.log(f"[TIMEOUT] {test_type} {profile} exceeded {timeout_sec}s limit")
            self.results.append({
                "test": f"{test_type} {profile}",
                "status": "TIMEOUT",
                "duration": timeout_sec,
                "timestamp": test_start_time.isoformat(),
                "error": f"Exceeded {timeout_sec}s timeout"
            })
            return False

        except Exception as e:
            elapsed = time.time() - start
            self.log(f"[ERROR] {test_type} {profile}: {str(e)}")
            self.results.append({
                "test": f"{test_type} {profile}",
                "status": "ERROR",
                "duration": elapsed,
                "timestamp": test_start_time.isoformat(),
                "error": str(e)
            })
            return False

    def run_parallel_batch(self, tests_at_time):
        """Run tests scheduled for the same timestamp in parallel."""
        started = []
        for test in tests_at_time:
            cmd = [
                "python",
                str(self.script_path),
                "--type", test["type"],
                "--profile", test["profile"]
            ]
            timeout_sec = test["expected_duration"] + 600
            started.append({
                "test": test,
                "cmd": cmd,
                "timeout": timeout_sec,
                "process": None
            })

        for item in started:
            self.log(f"Starting parallel test: --type {item['test']['type']} --profile {item['test']['profile']}")
            item["process"] = subprocess.Popen(
                item["cmd"],
                cwd=str(self.script_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        for item in started:
            proc = item["process"]
            test = item["test"]
            try:
                stdout, _ = proc.communicate(timeout=item["timeout"])
                if proc.returncode == 0:
                    self.log(f"[PASS] {test['type']} {test['profile']} in batch")
                    self.results.append({
                        "test": f"{test['type']} {test['profile']}",
                        "status": "PASSED",
                        "duration": item["timeout"],
                        "timestamp": datetime.now().isoformat(),
                    })
                else:
                    self.log(f"[FAIL] {test['type']} {test['profile']} returned code {proc.returncode} in batch")
                    self.results.append({
                        "test": f"{test['type']} {test['profile']}",
                        "status": "FAILED",
                        "duration": item["timeout"],
                        "timestamp": datetime.now().isoformat(),
                        "error": f"Return code {proc.returncode}"
                    })
            except subprocess.TimeoutExpired:
                proc.kill()
                self.log(f"[TIMEOUT] {test['type']} {test['profile']} exceeded {item['timeout']}s limit in batch")
                self.results.append({
                    "test": f"{test['type']} {test['profile']}",
                    "status": "TIMEOUT",
                    "duration": item["timeout"],
                    "timestamp": datetime.now().isoformat(),
                    "error": f"Exceeded {item['timeout']}s timeout"
                })
            except Exception as e:
                self.log(f"[ERROR] {test['type']} {test['profile']}: {str(e)}")
                self.results.append({
                    "test": f"{test['type']} {test['profile']}",
                    "status": "ERROR",
                    "duration": item["timeout"],
                    "timestamp": datetime.now().isoformat(),
                    "error": str(e)
                })

    def print_summary(self):
        """Print summary report"""
        self.log("\n" + "=" * 70)
        self.log("TEST SUMMARY")
        self.log("=" * 70)

        passed = sum(1 for r in self.results if r["status"] == "PASSED")
        failed = sum(1 for r in self.results if r["status"] == "FAILED")
        timeout = sum(1 for r in self.results if r["status"] == "TIMEOUT")
        error = sum(1 for r in self.results if r["status"] == "ERROR")

        total = len(self.results)
        elapsed_total = sum(r["duration"] for r in self.results)

        self.log(f"\nResults: {passed}/{total} PASSED")
        if failed > 0:
            self.log(f"         {failed} FAILED")
        if timeout > 0:
            self.log(f"         {timeout} TIMEOUT")
        if error > 0:
            self.log(f"         {error} ERROR")

        self.log(f"\nTotal duration: {elapsed_total:.1f}s ({elapsed_total//60}m {int(elapsed_total%60)}s)")
        self.log(f"Started: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"Ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        self.log("\nDetailed results:")
        for r in self.results:
            status_symbol = "[PASS]" if r["status"] == "PASSED" else "[FAIL]"
            self.log(f"  {status_symbol} {r['test']:25} {r['status']:7} ({r['duration']:.1f}s)")

        self.log("=" * 70)

    def run_schedule(self):
        """Run all tests according to schedule, launching same-time tests in parallel."""
        self.start_time = datetime.now()
        self.log("=" * 70)
        self.log("LOAD TEST SCHEDULE STARTED")
        self.log("=" * 70)
        self.log(f"Current time: {self.start_time.strftime('%H:%M:%S')}")
        self.log(f"Script location: {self.script_path}")
        self.log(f"Log file: {self.log_file}")
        self.log("")
        self.log("Scheduled tests:")
        for test in self.tests:
            self.log(f"  {test['time'].strftime('%H:%M')} - {test['type']:7} {test['profile']:7} (expect {test['expected_duration']}s)")
        self.log("")

        grouped = {}
        for test in self.tests:
            grouped.setdefault(test["time"], []).append(test)

        for target_time in sorted(grouped.keys()):
            self.log(f"\nWaiting for batch at {target_time.strftime('%H:%M')}...")
            self.wait_until(target_time)

            actual_time = datetime.now().time()
            if actual_time > target_time:
                minutes_late = (datetime.combine(datetime.today(), actual_time) -
                                datetime.combine(datetime.today(), target_time)).total_seconds() / 60
                self.log(f"[WARN] Batch started {minutes_late:.1f}m late")

            self.log(f"Starting batch at {datetime.now().strftime('%H:%M:%S')}")
            self.run_parallel_batch(grouped[target_time])

        self.print_summary()


def main():
    scheduler = TestScheduler()

    try:
        scheduler.preflight_check()
        scheduler.run_schedule()
    except KeyboardInterrupt:
        scheduler.log("\n\n[WARN] Scheduler interrupted by user")
        scheduler.print_summary()
        exit(0)
    except Exception as e:
        scheduler.log(f"\n[ERROR] FATAL ERROR: {str(e)}")
        scheduler.print_summary()
        exit(1)


if __name__ == "__main__":
    main()