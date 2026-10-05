#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
K6_SCRIPT = ROOT / "k6_load_test.js"
PROFILES_FILE = ROOT / "k6_profiles.json"
LOG_FILE = ROOT / "k6_schedule.log"
AWS_CREDS_FILE = ROOT / ".k6-aws-creds.json"

TYPES = ["event", "request"]
WARMUP_PROFILE = "warmup"
PROFILES = ["steady", "spike", "long"]
RUNS_PER_PROFILE = 5
GAP_SAME_PROFILE_SEC = 5 * 60
GAP_DIFFERENT_PROFILE_SEC = 5 * 60
AWS_REGION = "sa-east-1"

_log_lock = threading.Lock()
_aws_creds_lock = threading.Lock()


def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def parse_duration_seconds(value: str) -> int:
    value = value.strip().lower()
    if value.endswith("ms"):
        return max(1, int(float(value[:-2]) / 1000))
    if value.endswith("s"):
        return int(float(value[:-1]))
    if value.endswith("m"):
        return int(float(value[:-1]) * 60)
    if value.endswith("h"):
        return int(float(value[:-1]) * 3600)
    return int(float(value))


def profile_run_seconds(profiles_cfg: dict, profile_name: str) -> int:
    duration = parse_duration_seconds(profiles_cfg[profile_name]["duration"])
    ramp_up = parse_duration_seconds(profiles_cfg[profile_name].get("rampUp", "30s"))
    graceful = parse_duration_seconds(profiles_cfg[profile_name].get("gracefulStop", "0s"))
    return ramp_up + duration + graceful


def estimate_total_seconds(profiles_cfg: dict) -> int:
    total = profile_run_seconds(profiles_cfg, WARMUP_PROFILE)
    total += GAP_DIFFERENT_PROFILE_SEC
    for index, profile_name in enumerate(PROFILES):
        total += RUNS_PER_PROFILE * profile_run_seconds(profiles_cfg, profile_name)
        total += (RUNS_PER_PROFILE - 1) * GAP_SAME_PROFILE_SEC
        if index < len(PROFILES) - 1:
            total += GAP_DIFFERENT_PROFILE_SEC
    return total


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
    with _aws_creds_lock:
        AWS_CREDS_FILE.write_text(json.dumps(payload), encoding="utf-8")


def sleep_seconds(seconds: int, reason: str) -> None:
    if seconds <= 0:
        return
    until = datetime.now() + timedelta(seconds=seconds)
    log(f"Sleeping {seconds // 60}m ({reason}) until {until.strftime('%H:%M:%S')}")
    time.sleep(seconds)


def run_k6(type_name: str, profile_name: str, run_index: int, env: dict) -> int:
    run_env = strip_aws_keys(env)
    if type_name == "event":
        try:
            write_aws_creds_file(run_env)
            log(f"[{type_name}] AWS credentials exported for k6")
        except Exception as exc:
            log(f"[{type_name}] ERROR exporting AWS credentials: {exc}")
            log("Hint: run `aws login` again, and clear stale AWS_* env vars in this shell.")
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
    log(f"START type={type_name} profile={profile_name} run={run_index}/{RUNS_PER_PROFILE}")
    log(f"[{type_name}] CMD: {' '.join(cmd)}")
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=run_env)
        elapsed = int(time.monotonic() - started)
        status = "OK" if result.returncode == 0 else f"FAIL(code={result.returncode})"
        log(
            f"END   type={type_name} profile={profile_name} "
            f"run={run_index}/{RUNS_PER_PROFILE} {status} elapsed={elapsed}s"
        )
        return result.returncode
    except Exception as exc:
        log(
            f"END   type={type_name} profile={profile_name} "
            f"run={run_index}/{RUNS_PER_PROFILE} ERROR: {exc}"
        )
        return 1
    finally:
        if type_name == "event":
            with _aws_creds_lock:
                AWS_CREDS_FILE.unlink(missing_ok=True)


def run_type_schedule(type_name: str, env: dict) -> list[dict]:
    results = []
    log(f"[{type_name}] worker started")

    code = run_k6(type_name, WARMUP_PROFILE, 1, env)
    results.append(
        {
            "type": type_name,
            "profile": WARMUP_PROFILE,
            "run": 1,
            "exit_code": code,
        }
    )
    if type_name == "event" and code != 0:
        log(f"[{type_name}] Aborting this worker after warmup failure.")
        return results

    sleep_seconds(GAP_DIFFERENT_PROFILE_SEC, f"{type_name} after warmup")

    for profile_index, profile_name in enumerate(PROFILES):
        for run_index in range(1, RUNS_PER_PROFILE + 1):
            code = run_k6(type_name, profile_name, run_index, env)
            results.append(
                {
                    "type": type_name,
                    "profile": profile_name,
                    "run": run_index,
                    "exit_code": code,
                }
            )
            if type_name == "event" and code != 0:
                log(f"[{type_name}] Aborting this worker after failure.")
                return results
            if run_index < RUNS_PER_PROFILE:
                sleep_seconds(GAP_SAME_PROFILE_SEC, f"{type_name} same profile")

        if profile_index < len(PROFILES) - 1:
            sleep_seconds(GAP_DIFFERENT_PROFILE_SEC, f"{type_name} different profile")

    log(f"[{type_name}] worker finished")
    return results


def main() -> int:
    if shutil.which("k6") is None:
        print("ERROR: k6 not found in PATH", file=sys.stderr)
        return 1
    if not K6_SCRIPT.exists():
        print(f"ERROR: missing {K6_SCRIPT}", file=sys.stderr)
        return 1
    if not PROFILES_FILE.exists():
        print(f"ERROR: missing {PROFILES_FILE}", file=sys.stderr)
        return 1

    profiles_cfg = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    required_profiles = [WARMUP_PROFILE, *PROFILES]
    for profile_name in required_profiles:
        if profile_name not in profiles_cfg:
            print(f"ERROR: profile '{profile_name}' not in {PROFILES_FILE}", file=sys.stderr)
            return 1

    env = strip_aws_keys(os.environ.copy())

    estimated = estimate_total_seconds(profiles_cfg)
    eta = datetime.now() + timedelta(seconds=estimated)

    log("=" * 72)
    log("K6 OVERNIGHT SCHEDULER")
    log(f"Mode: parallel types={TYPES}")
    log(f"Warmup: {WARMUP_PROFILE} x1, then gap {GAP_DIFFERENT_PROFILE_SEC // 60}m")
    log(f"Profiles: {PROFILES}")
    log(f"Runs per profile: {RUNS_PER_PROFILE}")
    log(f"Gap same profile: {GAP_SAME_PROFILE_SEC // 60}m")
    log(f"Gap different profile: {GAP_DIFFERENT_PROFILE_SEC // 60}m")
    log(
        f"Estimated wall time: ~{estimated // 3600}h{(estimated % 3600) // 60}m "
        f"(ETA {eta.strftime('%Y-%m-%d %H:%M')})"
    )
    log("=" * 72)

    if "event" in TYPES:
        try:
            ensure_aws_login(env)
            log("AWS login OK (sts get-caller-identity)")
        except Exception as exc:
            log(f"ERROR: AWS login not usable: {exc}")
            log("Stop the scheduler, clear AWS_* env vars, run `aws login`, then start again.")
            return 1

    results = []
    try:
        with ThreadPoolExecutor(max_workers=len(TYPES)) as executor:
            futures = {
                executor.submit(run_type_schedule, type_name, env): type_name
                for type_name in TYPES
            }
            for future in as_completed(futures):
                type_name = futures[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    log(f"[{type_name}] worker crashed: {exc}")
                    results.append(
                        {
                            "type": type_name,
                            "profile": "?",
                            "run": 0,
                            "exit_code": 1,
                        }
                    )
    finally:
        with _aws_creds_lock:
            AWS_CREDS_FILE.unlink(missing_ok=True)

    profile_order = [WARMUP_PROFILE, *PROFILES]

    def sort_key(r):
        profile_rank = profile_order.index(r["profile"]) if r["profile"] in profile_order else 99
        return (r["type"], profile_rank, r["run"])

    results.sort(key=sort_key)
    ok = sum(1 for r in results if r["exit_code"] == 0)
    fail = len(results) - ok
    log("=" * 72)
    log(f"SUMMARY total={len(results)} ok={ok} fail={fail}")
    for r in results:
        mark = "OK" if r["exit_code"] == 0 else "FAIL"
        log(f"  {mark} type={r['type']} profile={r['profile']} run={r['run']} code={r['exit_code']}")
    log("=" * 72)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
