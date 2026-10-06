#!/usr/bin/env python3
"""Refetch CloudWatch CSVs for all k6 runs in results/.

For each (kind, profile), computes the padded window from the k6 JSON files and
calls fetch_cloudwatch_metrics.py (start = 1st run − padding, end = last run + pad).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
PROFILES = json.loads((ROOT / "k6_profiles.json").read_text(encoding="utf-8"))
DEFAULT_PADDING_MINUTES = 10
END_PADDING_MINUTES = 5
PROFILE_PADDING_MINUTES = {
    "steady": 20,
}
PROFILE_END_PADDING_MINUTES = {
    "spike": 12,
    "steady": 12,
}
FILENAME_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d+)?)Z\.json$")
LOCAL_TZ = timezone(timedelta(hours=-3))

def parse_duration(value: str) -> timedelta:
    value = value.strip().lower()
    if value.endswith("ms"):
        return timedelta(milliseconds=float(value[:-2]))
    if value.endswith("s"):
        return timedelta(seconds=float(value[:-1]))
    if value.endswith("m"):
        return timedelta(minutes=float(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=float(value[:-1]))
    raise ValueError(value)

def run_length(profile_name: str, data: dict | None = None) -> timedelta:
    ramp = (data or {}).get("ramp_up") or PROFILES[profile_name].get("rampUp", "0s")
    duration = (data or {}).get("duration") or PROFILES[profile_name]["duration"]
    return parse_duration(ramp) + parse_duration(duration)

def parse_filename_end(path: Path) -> datetime | None:
    match = FILENAME_TS_RE.search(path.name)
    if not match:
        return None
    stamp = match.group(1)
    date_part, time_part = stamp.split("T", 1)
    time_part = time_part.replace("-", ":", 2)
    return datetime.fromisoformat(f"{date_part}T{time_part}+00:00").astimezone(LOCAL_TZ)

def resolve_run_window(path: Path, data: dict) -> tuple[datetime, datetime]:
    length = run_length(data["profile"], data)
    start_raw = data.get("start_time")
    end_raw = data.get("end_time")
    start = datetime.fromisoformat(start_raw) if start_raw else None
    end = datetime.fromisoformat(end_raw) if end_raw else None
    file_end = parse_filename_end(path)

    if start and end and start != end:
        return start, end

    finish = file_end or end or start
    if finish is None:
        raise ValueError(f"sem timestamp utilizável em {path}")
    return finish - length, finish

def discover_windows(
    input_dir: Path, default_padding_minutes: int
) -> dict[tuple[str, str], tuple[datetime, datetime, int]]:
    groups: dict[tuple[str, str], list[tuple[datetime, datetime]]] = defaultdict(list)

    for path in sorted(input_dir.glob("k6-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        key = (data["kind"], data["profile"])
        start, end = resolve_run_window(path, data)
        groups[key].append((start, end))

    windows = {}
    for key, runs in groups.items():
        _, profile = key
        pad_minutes = PROFILE_PADDING_MINUTES.get(profile, default_padding_minutes)
        end_pad = PROFILE_END_PADDING_MINUTES.get(profile, END_PADDING_MINUTES)
        window_start = min(start for start, _ in runs) - timedelta(minutes=pad_minutes)
        window_end = max(end for _, end in runs) + timedelta(minutes=end_pad)
        windows[key] = (window_start, window_end, pad_minutes, end_pad)
    return windows

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refetch CloudWatch CSVs for results/ (via fetch_cloudwatch_metrics.py)."
    )
    parser.add_argument("--input", type=Path, default=RESULTS)
    parser.add_argument(
        "--padding-minutes",
        type=int,
        default=DEFAULT_PADDING_MINUTES,
        help=f"Padding padrão em minutos (default: {DEFAULT_PADDING_MINUTES}; steady usa 20).",
    )
    parser.add_argument("--profile", help="Se informado, refetch só este perfil (ex.: steady).")
    args = parser.parse_args()

    input_dir = args.input if args.input.is_absolute() else ROOT / args.input
    windows = discover_windows(input_dir, args.padding_minutes)
    if not windows:
        raise SystemExit(f"ERROR: no k6-*.json in {input_dir}")

    for (kind, profile), (start, end, pad_minutes, end_pad) in sorted(windows.items()):
        if args.profile and profile != args.profile:
            continue
        out = input_dir / f"cloudwatch-{kind}-{profile}.csv"
        cmd = [
            sys.executable,
            str(ROOT / "fetch_cloudwatch_metrics.py"),
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--output",
            str(out),
        ]
        print("=" * 72)
        print(
            f"{kind}/{profile}: {start.isoformat()} -> {end.isoformat()} "
            f"(start -{pad_minutes}m, end +{end_pad}m)"
        )
        print(" ".join(cmd))
        result = subprocess.run(cmd, cwd=str(ROOT))
        if result.returncode != 0:
            return result.returncode
    print("Concluído.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
