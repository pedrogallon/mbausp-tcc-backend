#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
K6_SCRIPT = ROOT / "k6_load_test.js"
PROFILES_FILE = ROOT / "k6_profiles.json"
LOG_FILE = ROOT / "k6_repeat.log"
AWS_CREDS_FILE = ROOT / ".k6-aws-creds.json"
AWS_REGION = "sa-east-1"
DEFAULT_RUNS = 5
GAP_BETWEEN_RUNS_SEC = 5 * 60

def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

def strip_aws_keys(env: dict) -> dict:
    cleaned = dict(env)
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
    ):
        cleaned.pop(key, None)
    cleaned["AWS_REGION"] = AWS_REGION
    return cleaned

def ensure_aws_login(env: dict) -> None:
    cli_env = strip_aws_keys(env)
    subprocess.check_output(
        ["aws", "sts", "get-caller-identity", "--region", AWS_REGION],
        text=True,
        env=cli_env,
    )

def write_aws_creds_file(env: dict) -> None:
    cli_env = strip_aws_keys(env)
    raw = subprocess.check_output(
        ["aws", "configure", "export-credentials", "--format", "process"],
        text=True,
        env=cli_env,
    )
    creds = json.loads(raw)
    expiration = creds.get("Expiration")
    if expiration:
        exp = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        now = datetime.now(exp.tzinfo)
        if exp <= now:
            raise RuntimeError(f"Exported AWS credentials already expired at {expiration}")

    payload = {
        "AccessKeyId": creds["AccessKeyId"],
        "SecretAccessKey": creds["SecretAccessKey"],
        "SessionToken": creds.get("SessionToken", ""),
        "Region": AWS_REGION,
        "Expiration": expiration,
    }
    AWS_CREDS_FILE.write_text(json.dumps(payload), encoding="utf-8")

def sleep_seconds(seconds: int, reason: str) -> None:
    if seconds <= 0:
        return
    until = datetime.now() + timedelta(seconds=seconds)
    log(f"Sleeping {seconds // 60}m ({reason}) until {until.strftime('%H:%M:%S')}")
    time.sleep(seconds)

def run_k6(type_name: str, profile_name: str, run_index: int, runs: int, env: dict) -> int:
    run_env = strip_aws_keys(env)
    if type_name == "event":
        try:
            write_aws_creds_file(run_env)
            log("AWS credentials exported for k6")
        except Exception as exc:
            log(f"ERROR exporting AWS credentials: {exc}")
            return 1

    cmd = [
        "k6",
        "run",
        "-e",
        f"type={type_name}",
        "-e",
        f"K6_PROFILE={profile_name}",
        str(K6_SCRIPT),
    ]
    log(f"START type={type_name} profile={profile_name} run={run_index}/{runs}")
    log("CMD: " + " ".join(cmd))
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=run_env)
        elapsed = int(time.monotonic() - started)
        status = "OK" if result.returncode == 0 else f"FAIL(code={result.returncode})"
        log(
            f"END   type={type_name} profile={profile_name} "
            f"run={run_index}/{runs} {status} elapsed={elapsed}s"
        )
        return result.returncode
    except Exception as exc:
        log(
            f"END   type={type_name} profile={profile_name} "
            f"run={run_index}/{runs} ERROR: {exc}"
        )
        return 1
    finally:
        if type_name == "event":
            AWS_CREDS_FILE.unlink(missing_ok=True)

def parse_args() -> argparse.Namespace:
    profiles = []
    if PROFILES_FILE.exists():
        profiles = sorted(json.loads(PROFILES_FILE.read_text(encoding="utf-8")).keys())

    parser = argparse.ArgumentParser(
        description="Run the same k6 type+profile N times with a fixed gap between runs"
    )
    parser.add_argument(
        "--type",
        dest="kind",
        choices=["request", "event"],
        required=True,
        help="Workload type",
    )
    parser.add_argument(
        "--profile",
        choices=profiles or None,
        required=True,
        help="Profile from k6_profiles.json",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=f"How many times to run (default: {DEFAULT_RUNS})",
    )
    parser.add_argument(
        "--gap-minutes",
        type=int,
        default=GAP_BETWEEN_RUNS_SEC // 60,
        help="Minutes to wait between runs (default: 5)",
    )
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    if args.runs < 1:
        print("ERROR: --runs must be >= 1", file=sys.stderr)
        return 1
    if shutil.which("k6") is None:
        print("ERROR: k6 not found in PATH", file=sys.stderr)
        return 1
    if not K6_SCRIPT.exists():
        print(f"ERROR: missing {K6_SCRIPT}", file=sys.stderr)
        return 1
    if not PROFILES_FILE.exists():
        print(f"ERROR: missing {PROFILES_FILE}", file=sys.stderr)
        return 1

    env = strip_aws_keys(os.environ.copy())
    gap_sec = args.gap_minutes * 60

    log("=" * 72)
    log("K6 REPEAT RUNNER")
    log(f"type={args.kind} profile={args.profile} runs={args.runs} gap={args.gap_minutes}m")
    log("=" * 72)

    if args.kind == "event":
        try:
            ensure_aws_login(env)
            log("AWS login OK (sts get-caller-identity)")
        except Exception as exc:
            log(f"ERROR: AWS login not usable: {exc}")
            log("Clear AWS_* env vars, run aws login + export-credentials, then retry.")
            return 1

    results = []
    try:
        for run_index in range(1, args.runs + 1):
            code = run_k6(args.kind, args.profile, run_index, args.runs, env)
            results.append({"run": run_index, "exit_code": code})
            if args.kind == "event" and code != 0:
                log("Aborting after event-run failure.")
                break
            if run_index < args.runs:
                sleep_seconds(gap_sec, "between runs")
    finally:
        AWS_CREDS_FILE.unlink(missing_ok=True)

    ok = sum(1 for r in results if r["exit_code"] == 0)
    fail = len(results) - ok
    log("=" * 72)
    log(f"SUMMARY type={args.kind} profile={args.profile} total={len(results)} ok={ok} fail={fail}")
    for r in results:
        mark = "OK" if r["exit_code"] == 0 else "FAIL"
        log(f"  {mark} run={r['run']} code={r['exit_code']}")
    log("=" * 72)
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
