#!/usr/bin/env python3
"""Gráficos por perfil a partir dos CSV CloudWatch em results/ (+ resumo k6)."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Patch, Polygon
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "results"
DEFAULT_OUTPUT = DEFAULT_INPUT / "charts"
PROFILES_FILE = ROOT / "k6_profiles.json"
CSV_RE = re.compile(r"^cloudwatch-(request|event)-(.+)\.csv$")
K6_RE = re.compile(r"^k6-(request|event)-(.+)-(\d{4}-\d{2}-\d{2}T.+)\.json$")

PROFILE_PT = {
    "steady": "carga constante",
    "spike": "pico",
    "long": "longa duração",
    "warmup": "aquecimento",
    "test": "teste",
}
KIND_PT = {
    "request": "Requisição",
    "event": "Evento",
}

REQUEST_COLOR = "#1f77b4"
EVENT_COLOR = "#ff7f0e"
COST_COLORS = {"fargate": "#4c78a8", "sqs": "#f58518"}
Y_PAD = 1.2
X_PAD_MINUTES = 2.0
SUBPLOT_HSPACE = 0.14
SUBPLOT_WSPACE = 0.09

def format_br(value: float, decimals: int | None = None) -> str:
    """Padrão BR: ponto como milhar e vírgula como decimal (ex.: 1.234,56)."""
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(v):
        return ""
    if decimals is None:
        decimals = 0 if abs(v - round(v)) < 1e-9 * max(1.0, abs(v)) else 2
    if decimals <= 0:
        s = f"{v:,.0f}"
    else:
        s = f"{v:,.{int(decimals)}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

def format_tick_br(value, _pos=None) -> str:
    """Rótulo de eixo no padrão BR, sem zeros decimais desnecessários."""
    if value is None or not np.isfinite(value):
        return ""
    v = float(value)
    if abs(v) < 1e-12:
        return "0"
    if abs(v - round(v)) < 1e-6 * max(1.0, abs(v)):
        return format_br(round(v), 0)
    abs_v = abs(v)
    if abs_v >= 100:
        decimals = 1
    elif abs_v >= 1:
        decimals = 2
    else:
        decimals = 3
    return format_br(v, decimals).rstrip("0").rstrip(",")

def apply_br_number_format(fig, *, y_only: bool = False) -> None:
    """Aplica separadores BR nos eixos. y_only=True preserva rótulos categóricos no X."""
    formatter = FuncFormatter(format_tick_br)
    for ax in fig.get_axes():
        ax.yaxis.set_major_formatter(formatter)
        if not y_only:
            ax.xaxis.set_major_formatter(formatter)

def space_subplots(fig, hspace: float = SUBPLOT_HSPACE, wspace: float = SUBPLOT_WSPACE) -> None:
    """Aumenta a margem entre gráficos em figuras com múltiplos painéis."""
    if getattr(fig, "get_constrained_layout", lambda: False)():
        fig.set_constrained_layout_pads(w_pad=0.03, h_pad=0.05, hspace=hspace, wspace=wspace)
    else:
        fig.subplots_adjust(hspace=hspace, wspace=wspace)

ZERO_FILL_KEYS = {
    "requests_processed_per_s",
    "events_processed_per_s",
    "alb_requests_per_s",
    "sqs_sent_per_s",
    "sqs_deleted_per_s",
    "sqs_visible_avg",
    "sqs_oldest_age_s",
    "alb_latency_p50_ms",
    "alb_latency_p95_ms",
    "alb_latency_p99_ms",
    "requests_processing_avg_ms",
    "requests_processing_p50_ms",
    "requests_processing_p95_ms",
    "requests_processing_p99_ms",
    "events_processing_avg_ms",
    "events_processing_p50_ms",
    "events_processing_p95_ms",
    "events_processing_p99_ms",
    "requests_error_rate",
    "events_app_error_rate",
    "requests_app_error_rate",
    "requests_slow_15s_rate",
    "fargate_cost_usd",
    "sqs_cost_usd",
    "total_cost_usd",
    "k6_failed_rate_pct",
    "k6_client_throughput_ops_s",
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
    "topologia",
    "minuto_utc",
    "minuto_desde_inicio",
    "cpu_utilizacao_percentual",
    "memoria_utilizacao_percentual",
    "memoria_mib",
    "tarefas_em_execucao",
    "fonte_cpu",
    "fonte_memoria",
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
    """Origem do eixo X = início real da 1ª execução k6 (ignora padding do CloudWatch)."""
    runs = k6_groups.get((kind, profile), [])
    starts = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        start, _ = resolve_run_window(run, profile, profiles, path)
        starts.append(start)
    return min(starts) if starts else None

def throughput_series_for_kind(rows: list[dict], kind: str) -> np.ndarray:
    if kind == "request":
        return series(rows, "requests_processed_per_s")
    processed = series(rows, "events_processed_per_s")
    sent = series(rows, "sqs_sent_per_s")
    out = processed.copy()
    missing = out <= 0
    out[missing] = sent[missing]
    return out

def busy_segments(
    rows: list[dict],
    kind: str,
    x: np.ndarray,
    tp_threshold: float = 20.0,
    min_quiet: int = 2,
) -> list[tuple[float, float]]:
    """Segmentos contíguos de carga (início/fim em minutos relativos ao origin)."""
    if not rows or x.size == 0:
        return []
    tp = throughput_series_for_kind(rows, kind)
    if float(np.nanmax(tp)) < tp_threshold:
        cpu = np.array([pick_resource(r, kind)["cpu_percent_avg"] for r in rows], dtype=float)
        active = cpu >= 20.0
    else:
        active = tp >= tp_threshold

    segments: list[tuple[float, float]] = []
    start = None
    quiet = 0
    for i, is_on in enumerate(active.tolist()):
        if is_on:
            if start is None:
                start = float(x[i])
            quiet = 0
            end = float(x[i])
        elif start is not None:
            quiet += 1
            if quiet >= min_quiet:
                segments.append((start, end))
                start = None
                quiet = 0
    if start is not None:
        segments.append((start, end))
    return segments

def overlay_relative_minutes(
    rows: list[dict],
    origin: datetime | None,
    segments: list[tuple[float, float]],
    template: list[tuple[float, float]],
) -> np.ndarray:
    """Mapeia cada pico de carga ao mesmo template temporal (overlays sem drift)."""
    x = relative_minutes(rows, origin)
    if not segments or not template:
        return x

    n = min(len(segments), len(template))
    aligned = x.copy()
    s0, _ = segments[0]
    t0, _ = template[0]
    aligned = np.where(x < s0, x - s0 + t0, aligned)

    for i in range(n):
        s_a, s_b = segments[i]
        t_a, t_b = template[i]
        span_s = s_b - s_a
        span_t = t_b - t_a
        if span_s <= 0:
            continue
        scale = span_t / span_s if span_s > 0 else 1.0
        in_seg = (x >= s_a) & (x <= s_b)
        aligned = np.where(in_seg, t_a + (x - s_a) * scale, aligned)

        next_s = segments[i + 1][0] if i + 1 < n else None
        next_t = template[i + 1][0] if i + 1 < n else None
        if next_s is not None and next_t is not None:
            gap_s = next_s - s_b
            gap_t = next_t - t_b
            in_gap = (x > s_b) & (x < next_s)
            if gap_s > 0:
                aligned = np.where(in_gap, t_b + (x - s_b) * (gap_t / gap_s), aligned)
            else:
                aligned = np.where(in_gap, t_b, aligned)
        else:
            in_trail = x > s_b
            aligned = np.where(in_trail, t_b + (x - s_b), aligned)

    return aligned

def overlay_alignment(
    series_map: dict[str, list[dict]],
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
    origins: dict[str, datetime | None],
) -> dict[str, tuple[datetime | None, list[tuple[float, float]], list[tuple[float, float]]]]:
    """Por kind: (origin, segmentos detectados, template compartilhado de picos)."""
    meta: dict[str, tuple[datetime | None, list[tuple[float, float]]]] = {}
    for kind in ("request", "event"):
        rows = series_map.get(kind)
        if not rows:
            continue
        origin = origins.get(kind) or first_k6_origin(kind, profile, k6_groups, profiles)
        x = relative_minutes(rows, origin)
        meta[kind] = (origin, busy_segments(rows, kind, x))

    counts = [len(segs) for _, segs in meta.values() if segs]
    n = min(counts) if counts else 0
    if n == 0:
        return {kind: (origin, segs, []) for kind, (origin, segs) in meta.items()}

    dur = []
    gap = []
    for _, segs in meta.values():
        segs = segs[:n]
        dur.append([b - a for a, b in segs])
        gap.append([segs[i + 1][0] - segs[i][1] for i in range(n - 1)])
    mean_dur = [float(np.mean([d[i] for d in dur])) for i in range(n)]
    mean_gap = [float(np.mean([g[i] for g in gap])) for i in range(n - 1)] if n > 1 else []

    template: list[tuple[float, float]] = []
    cursor = 0.0
    for i in range(n):
        template.append((cursor, cursor + mean_dur[i]))
        cursor += mean_dur[i]
        if i < n - 1:
            cursor += mean_gap[i]

    return {kind: (origin, segs[:n], template) for kind, (origin, segs) in meta.items()}

def trim_rows_to_k6_window(
    rows: list[dict],
    kind: str,
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
    lead_minutes: int = 2,
    trail_minutes: int | None = None,
) -> tuple[list[dict], datetime | None]:
    """Recorta a série para [1ª k6 − lead, última k6 + trail], alinhando falhas e AWS."""
    runs = k6_groups.get((kind, profile), [])
    if not runs or not rows:
        return rows, (rows[0]["minute"] if rows else None)

    if trail_minutes is None:
        trail_minutes = 12

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

def attach_k6_client_metrics(
    rows: list[dict],
    kind: str,
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> None:
    """Preenche métricas do cliente k6 por minuto (falhas % e vazão ops/s)."""
    runs = k6_groups.get((kind, profile), [])
    windows = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        start, end = resolve_run_window(run, profile, profiles, path)
        rate_pct = (run.get("failed_rate") or 0.0) * 100.0
        thr = run.get("throughput_req_s")
        if thr is None:
            thr = run.get("throughput_events_s") or 0.0
        windows.append((start, end, rate_pct, float(thr)))
        print(
            f"  k6 {kind}/{profile}: {start.isoformat()} -> {end.isoformat()} "
            f"falhas={rate_pct:.2f}% vazão={float(thr):.2f} ops/s"
        )

    for row in rows:
        minute = row["minute"]
        minute_end = minute + timedelta(minutes=1)
        rate = 0.0
        thr = 0.0
        for start, end, rate_pct, thr_ops in windows:
            if start < minute_end and end > minute:
                rate = rate_pct
                thr = thr_ops
                break
        row["k6_failed_rate_pct"] = rate
        row["k6_client_throughput_ops_s"] = thr


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

def pad_ylim(ax, values=None, ymin=0.0, factor: float | None = None):
    """Expande o eixo Y acima do máximo (evita linha sobre a legenda)."""
    pad = Y_PAD if factor is None else factor
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
        ax.set_ylim(ymin, pad)
        return
    ax.set_ylim(ymin, ymax * pad)

def style_axes(ax, title: str, ylabel: str, xlabel: str = "minutos"):
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xlabel(xlabel)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    pad_ylim(ax)

def paint_under(ax, x, y, color: str, alpha_top: float = 0.40, zorder: float = 1.5) -> None:
    """Preenche sob a curva com degradê vertical (opaco na linha → transparente no zero)."""
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.nan_to_num(np.asarray(y, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    if x_arr.size < 2 or float(np.nanmax(y_arr)) <= 0:
        return

    rgb = mcolors.to_rgb(color)
    cmap = LinearSegmentedColormap.from_list(
        "paint_under",
        [(rgb[0], rgb[1], rgb[2], 0.0), (rgb[0], rgb[1], rgb[2], alpha_top)],
    )
    ymin = 0.0
    ymax = float(np.nanmax(y_arr))
    im = ax.imshow(
        np.linspace(0.0, 1.0, 256).reshape(-1, 1),
        extent=[float(x_arr.min()), float(x_arr.max()), ymin, ymax],
        aspect="auto",
        origin="lower",
        cmap=cmap,
        interpolation="bicubic",
        zorder=zorder,
        clip_on=True,
    )
    verts = list(zip(x_arr.tolist(), y_arr.tolist()))
    verts += [(float(x_arr[-1]), ymin), (float(x_arr[0]), ymin)]
    clip = Polygon(verts, closed=True, transform=ax.transData)
    im.set_clip_path(clip)

def apply_shared_xlim(axes, x_arrays, pad_minutes: float = X_PAD_MINUTES) -> None:
    """Mesmo eixo X em todos os painéis, com folga simétrica (padrão: 2 min)."""
    finite = [np.asarray(x, dtype=float) for x in x_arrays if len(x)]
    if not finite:
        return
    xmin = min(float(np.nanmin(x)) for x in finite)
    xmax = max(float(np.nanmax(x)) for x in finite)
    left = xmin - pad_minutes
    right = xmax + pad_minutes
    for ax in axes:
        ax.set_xlim(left, right)

def profile_title(profile: str) -> str:
    return PROFILE_PT.get(profile, profile)

def plot_profile(
    profile: str,
    series_map: dict[str, list[dict]],
    out_dir: Path,
    origins: dict[str, datetime | None],
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> Path:
    fig = plt.figure(figsize=(12, 10.2), constrained_layout=True)
    gs = fig.add_gridspec(3, 2, height_ratios=[1, 1, 2])
    ax_tp = fig.add_subplot(gs[0:2, 0])
    ax_lat = fig.add_subplot(gs[0, 1])
    ax_lat_event = fig.add_subplot(gs[1, 1], sharex=ax_lat)
    ax_fail = fig.add_subplot(gs[2, 0])
    ax_proc = fig.add_subplot(gs[2, 1])
    space_subplots(fig)

    lat_req_vals = []
    lat_event_vals = []
    x_arrays = []
    align = overlay_alignment(series_map, profile, k6_groups, profiles, origins)

    for kind, color in (
        ("request", REQUEST_COLOR),
        ("event", EVENT_COLOR),
    ):
        label = KIND_PT[kind]
        rows = series_map.get(kind)
        if not rows:
            continue
        origin, segments, template = align.get(
            kind, (origins.get(kind) or rows[0]["minute"], [], [])
        )
        x = overlay_relative_minutes(rows, origin, segments, template)
        x_arrays.append(x)

        if kind == "request":
            tp = series(rows, "requests_processed_per_s")
            proc = series(rows, "requests_processing_avg_ms")
            p50 = series(rows, "alb_latency_p50_ms")
            p95 = series(rows, "alb_latency_p95_ms")
            p99 = series(rows, "alb_latency_p99_ms")
            lat_req_vals.extend([p50, p95, p99])
            ax_lat.plot(x, p50, color=color, linewidth=1.8, label=f"{label}, p50", zorder=3)
            ax_lat.plot(x, p95, color=color, linewidth=1.2, linestyle="--", label=f"{label}, p95", zorder=3)
            ax_lat.plot(x, p99, color=color, linewidth=1.2, linestyle=":", label=f"{label}, p99", zorder=3)
            svc_err = series(rows, "requests_app_error_rate") * 100.0
        else:
            tp = series(rows, "events_processed_per_s")
            proc = series(rows, "events_processing_avg_ms")
            p50 = series(rows, "events_processing_p50_ms")
            p95 = series(rows, "events_processing_p95_ms")
            p99 = series(rows, "events_processing_p99_ms")
            has_pct = any(
                arr.size > 0 and float(np.nanmax(np.nan_to_num(arr, nan=0.0))) > 0
                for arr in (p50, p95, p99)
            )
            if has_pct:
                ax_lat_event.plot(
                    x, p50, color=color, linewidth=1.8, label=f"{label}, p50", zorder=3
                )
                ax_lat_event.plot(
                    x,
                    p95,
                    color=color,
                    linewidth=1.2,
                    linestyle="--",
                    label=f"{label}, p95",
                    zorder=3,
                )
                ax_lat_event.plot(
                    x,
                    p99,
                    color=color,
                    linewidth=1.2,
                    linestyle=":",
                    label=f"{label}, p99",
                    zorder=3,
                )
                lat_event_vals.extend([p50, p95, p99])
            else:
                event_lat = series(rows, "events_processing_avg_ms")
                ax_lat_event.plot(
                    x,
                    event_lat,
                    color=color,
                    linewidth=1.8,
                    label=f"{label}, média",
                    zorder=3,
                )
                lat_event_vals.append(event_lat)
            svc_err = series(rows, "events_app_error_rate") * 100.0

        paint_under(ax_tp, x, tp, color)
        paint_under(ax_proc, x, proc, color)
        paint_under(ax_fail, x, svc_err, color, alpha_top=0.30)
        ax_tp.plot(x, tp, color=color, linewidth=1.8, label=label, zorder=3)
        ax_proc.plot(x, proc, color=color, linewidth=1.8, label=label, zorder=3)
        ax_fail.plot(x, svc_err, color=color, linewidth=1.8, label=label, zorder=3)

    style_axes(ax_tp, "Unidades processadas pela aplicação", "unidades/s")

    ax_lat.set_title("Percentis de tempo do ALB na requisição")
    ax_lat.set_ylabel("ms")
    ax_lat.grid(True, alpha=0.3)
    ax_lat.tick_params(axis="x", labelbottom=False)
    if lat_req_vals:
        pad_ylim(ax_lat, np.concatenate(lat_req_vals), ymin=0.0)
    if ax_lat.get_legend_handles_labels()[0]:
        ax_lat.legend(loc="upper right", fontsize=7)

    ax_lat_event.set_title("Percentis de processamento no evento")
    ax_lat_event.set_ylabel("ms")
    ax_lat_event.set_xlabel("minutos")
    ax_lat_event.grid(True, alpha=0.3)
    if lat_event_vals:
        pad_ylim(ax_lat_event, np.concatenate(lat_event_vals), ymin=0.0)
    if ax_lat_event.get_legend_handles_labels()[0]:
        ax_lat_event.legend(loc="upper right", fontsize=7)

    style_axes(ax_fail, "Percentual de exceções da aplicação", "%")
    style_axes(ax_proc, "Tempo médio de processamento da aplicação", "ms")
    for ax in (ax_tp, ax_fail, ax_proc):
        ax.set_xlabel("minutos")

    apply_shared_xlim((ax_tp, ax_lat, ax_lat_event, ax_proc, ax_fail), x_arrays, pad_minutes=X_PAD_MINUTES)
    space_subplots(fig)
    apply_br_number_format(fig)

    out_path = out_dir / f"perfil-{profile}-metricas.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path

def estimate_event_drain_windows(
    rows: list[dict],
    profile: str,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> list[dict]:
    """Para cada execução k6 event: duração de envio vs até drenar a fila SQS."""
    runs = k6_groups.get(("event", profile), [])
    if not runs or not rows:
        return []

    resolved = []
    for run in runs:
        path = Path(run["_path"]) if run.get("_path") else None
        send_start, send_end = resolve_run_window(run, profile, profiles, path)
        resolved.append((send_start, send_end))
    resolved.sort(key=lambda item: item[0])

    results = []
    for idx, (send_start, send_end) in enumerate(resolved):
        send_minutes = (send_end - send_start).total_seconds() / 60.0
        next_start = resolved[idx + 1][0] if idx + 1 < len(resolved) else None
        search_end = send_end + timedelta(minutes=15)
        if next_start is not None:
            search_end = min(search_end, next_start)

        zero_streak = 0
        drain_end = send_end
        saw_backlog = False
        for row in rows:
            minute = row["minute"]
            if minute < send_start:
                continue
            if minute >= search_end:
                break
            visible = row.get("sqs_visible_avg") or 0.0
            sent = row.get("sqs_sent_per_s") or 0.0
            if minute >= send_end and sent > 10.0 and next_start and minute >= next_start - timedelta(minutes=1):
                break
            if visible > 1.0:
                saw_backlog = True
                zero_streak = 0
                drain_end = minute + timedelta(minutes=1)
            elif minute >= send_end and saw_backlog:
                zero_streak += 1
                if zero_streak >= 2:
                    drain_end = minute - timedelta(minutes=1)
                    break

        drain_end = max(drain_end, send_end)
        if drain_end > search_end:
            drain_end = search_end
        process_minutes = (drain_end - send_start).total_seconds() / 60.0
        results.append(
            {
                "send_start": send_start,
                "send_end": send_end,
                "drain_end": drain_end,
                "send_minutes": send_minutes,
                "process_minutes": process_minutes,
                "extension_minutes": max(0.0, process_minutes - send_minutes),
            }
        )
    return results

def plot_event_sqs_extension(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    out_dir: Path,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> Path | None:
    """Mostra que o processamento event se estende além da janela de publicação k6."""
    rows = series_map.get("event")
    if not rows:
        return None

    origin = origins.get("event") or rows[0]["minute"]
    x = relative_minutes(rows, origin)
    sent = series(rows, "sqs_sent_per_s")
    deleted = series(rows, "sqs_deleted_per_s")
    visible = series(rows, "sqs_visible_avg")
    age = series(rows, "sqs_oldest_age_s")

    windows = estimate_event_drain_windows(rows, profile, k6_groups, profiles)
    avg_send = float(np.mean([w["send_minutes"] for w in windows])) if windows else 0.0
    avg_proc = float(np.mean([w["process_minutes"] for w in windows])) if windows else 0.0
    avg_ext = float(np.mean([w["extension_minutes"] for w in windows])) if windows else 0.0

    fig, axes = plt.subplots(2, 1, figsize=(11, 9.6), sharex=False)
    x_label = "minutos"

    ax_rate, ax_backlog = axes
    ax_rate.plot(x, sent, color=EVENT_COLOR, linewidth=1.6, label="Enviadas à fila")
    ax_rate.plot(x, deleted, color="#2ca02c", linewidth=1.6, label="Excluídas pelo consumidor")
    for w in windows:
        x0 = (w["send_start"] - origin).total_seconds() / 60.0
        x1 = (w["send_end"] - origin).total_seconds() / 60.0
        x2 = (w["drain_end"] - origin).total_seconds() / 60.0
        ax_rate.axvspan(x0, x1, color=EVENT_COLOR, alpha=0.08)
        ax_rate.axvspan(x1, x2, color="#2ca02c", alpha=0.12)
    ax_rate.set_title(
        "Publicação vs consumo de mensagens SQS"
    )
    ax_rate.set_ylabel("mensagens/s")
    ax_rate.grid(True, alpha=0.3)
    pad_ylim(ax_rate, np.concatenate([sent, deleted]), ymin=0.0, factor=1.1)
    leg_rate = ax_rate.legend(loc="upper right", fontsize=8, framealpha=0.92)

    ax_backlog.plot(x, visible, color="#d62728", linewidth=1.6, label="Mensagens visíveis na fila")
    ax_age = ax_backlog.twinx()
    ax_age.plot(x, age, color="#9467bd", linewidth=1.4, linestyle="--", label="Idade da mensagem mais antiga (s)")
    for w in windows:
        x0 = (w["send_start"] - origin).total_seconds() / 60.0
        x1 = (w["send_end"] - origin).total_seconds() / 60.0
        x2 = (w["drain_end"] - origin).total_seconds() / 60.0
        ax_backlog.axvspan(x0, x1, color=EVENT_COLOR, alpha=0.08)
        ax_backlog.axvspan(x1, x2, color="#2ca02c", alpha=0.12)
    ax_backlog.set_title("Mensagens acumuladas na fila durante e após cada publicação", pad=8)
    ax_backlog.set_ylabel("mensagens visíveis")
    ax_age.set_ylabel("idade máxima (s)")
    ax_backlog.grid(True, alpha=0.3)
    pad_ylim(ax_backlog, visible, ymin=0.0, factor=1.1)
    pad_ylim(ax_age, age, ymin=0.0, factor=1.1)
    lines_a, labels_a = ax_backlog.get_legend_handles_labels()
    lines_b, labels_b = ax_age.get_legend_handles_labels()
    ax_backlog.legend(lines_a + lines_b, labels_a + labels_b, loc="upper right", fontsize=8)

    for ax in (ax_rate, ax_backlog):
        ax.tick_params(axis="x", labelbottom=True, bottom=True)
        ax.set_xlabel(x_label, labelpad=4)

    band_handles = [
        Patch(facecolor=EVENT_COLOR, alpha=0.35, edgecolor=EVENT_COLOR, label="Intervalo de publicação"),
        Patch(facecolor="#2ca02c", alpha=0.35, edgecolor="#2ca02c", label="Consumo após a publicação"),
    ]
    band_legend_kw = dict(
        handles=band_handles,
        ncol=2,
        fontsize=9,
        frameon=True,
        columnspacing=1.0,
        handletextpad=0.5,
        borderpad=0.35,
    )
    fig.subplots_adjust(left=0.10, right=0.92, top=0.93, bottom=0.10, hspace=0.34)
    leg_bands_top = fig.legend(
        **band_legend_kw,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.528),
    )
    fig.add_artist(leg_bands_top)
    ax_rate.add_artist(leg_rate)
    fig.legend(
        **band_legend_kw,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.012),
    )

    apply_br_number_format(fig)
    out_path = out_dir / f"perfil-{profile}-event-extensao-sqs.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    print(
        f"  SQS extensão event/{profile}: envio médio={avg_send:.2f} min | "
        f"processamento até drenar={avg_proc:.2f} min | extensão=+{avg_ext:.2f} min"
    )
    return out_path

def plot_timeout_15s(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    out_dir: Path,
) -> Path:
    """Chamadas que o cliente não concluiu. Não é timeout de 15 s nem exceção da aplicação."""
    fig, ax = plt.subplots(figsize=(11, 4.5), constrained_layout=True)

    rows = series_map.get("request")
    vals = []
    if rows:
        origin = origins.get("request") or rows[0]["minute"]
        x = relative_minutes(rows, origin)
        k6_timeout = series(rows, "k6_failed_rate_pct")
        vals.append(k6_timeout)
        paint_under(ax, x, k6_timeout, REQUEST_COLOR)
        ax.plot(
            x,
            k6_timeout,
            color=REQUEST_COLOR,
            linewidth=1.8,
            label="Requisição — não concluída pelo cliente",
            zorder=3,
        )

    ax.set_title("Porcentagem de requisições não concluídas pelo cliente (timeout de 15s)")
    ax.set_ylabel("%")
    ax.set_xlabel("minutos")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    if vals:
        pad_ylim(ax, np.concatenate(vals), ymin=0.0)

    apply_br_number_format(fig)
    out_path = out_dir / f"perfil-{profile}-timeout-15s.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path

def plot_k6_client_throughput(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    out_dir: Path,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> Path:
    """Vazão reportada pelo cliente k6 (throughput_req_s / throughput_events_s), sem CloudWatch."""
    fig, ax = plt.subplots(figsize=(11, 4.5), constrained_layout=True)
    align = overlay_alignment(series_map, profile, k6_groups, profiles, origins)
    vals: list[np.ndarray] = []
    x_arrays: list[np.ndarray] = []
    prepared: list[tuple[str, str, np.ndarray, np.ndarray]] = []

    for kind, color in (("request", REQUEST_COLOR), ("event", EVENT_COLOR)):
        rows = series_map.get(kind)
        if not rows:
            continue
        origin, segments, template = align.get(
            kind, (origins.get(kind) or rows[0]["minute"], [], [])
        )
        x = overlay_relative_minutes(rows, origin, segments, template)
        y = series(rows, "k6_client_throughput_ops_s")
        prepared.append((color, KIND_PT[kind], x, y))

    if prepared:
        common_xmin = max(float(np.nanmin(item[2])) for item in prepared)
        common_xmax = min(float(np.nanmax(item[2])) for item in prepared)
        for color, label, x, y in prepared:
            mask = (x >= common_xmin - 1e-9) & (x <= common_xmax + 1e-9)
            x_p, y_p = x[mask], y[mask]
            x_arrays.append(x_p)
            vals.append(y_p)
            paint_under(ax, x_p, y_p, color)
            ax.plot(
                x_p,
                y_p,
                color=color,
                linewidth=1.8,
                label=f"{label} — vazão no cliente k6",
                zorder=3,
            )

    ax.set_title("Vazão medida pelo cliente k6")
    ax.set_ylabel("operações/s")
    ax.set_xlabel("minutos")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    if vals:
        pad_ylim(ax, np.concatenate(vals), ymin=0.0)
    apply_shared_xlim((ax,), x_arrays, pad_minutes=X_PAD_MINUTES)

    apply_br_number_format(fig)
    out_path = out_dir / f"perfil-{profile}-vazao-k6.png"
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
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
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
                        "topologia": KIND_PT[kind],
                        "minuto_utc": row["minute"].isoformat(),
                        "minuto_desde_inicio": f"{rel:.2f}",
                        "cpu_utilizacao_percentual": f"{res['cpu_percent_avg']:.4f}",
                        "memoria_utilizacao_percentual": f"{res['memory_percent_avg']:.4f}",
                        "memoria_mib": f"{res['memory_mib_avg']:.4f}",
                        "tarefas_em_execucao": f"{res['running_tasks']:.2f}",
                        "fonte_cpu": res["cpu_source"],
                        "fonte_memoria": res["memory_source"],
                    }
                )
    return out_path

def plot_resources(
    profile: str,
    series_map: dict[str, list[dict]],
    origins: dict[str, datetime | None],
    out_dir: Path,
    k6_groups: dict[tuple[str, str], list[dict]],
    profiles: dict,
) -> Path:
    fig, axes = plt.subplots(2, 1, figsize=(11, 7.9), constrained_layout=True, sharex=False)
    space_subplots(fig, hspace=0.16)
    ax_cpu, ax_mem = axes
    cpu_vals = []
    mem_vals = []
    x_arrays = []
    prepared: list[tuple[str, str, np.ndarray, np.ndarray, np.ndarray]] = []
    align = overlay_alignment(series_map, profile, k6_groups, profiles, origins)

    for kind, color in (
        ("request", REQUEST_COLOR),
        ("event", EVENT_COLOR),
    ):
        rows = series_map.get(kind)
        if not rows:
            continue
        origin, segments, template = align.get(
            kind, (origins.get(kind) or rows[0]["minute"], [], [])
        )
        x = overlay_relative_minutes(rows, origin, segments, template)
        cpu = np.array([pick_resource(r, kind)["cpu_percent_avg"] for r in rows], dtype=float)
        mem = np.array([pick_resource(r, kind)["memory_percent_avg"] for r in rows], dtype=float)
        prepared.append((color, KIND_PT[kind], x, cpu, mem))

    if prepared:
        common_xmin = max(float(np.nanmin(item[2])) for item in prepared)
        common_xmax = min(float(np.nanmax(item[2])) for item in prepared)
        for color, label, x, cpu, mem in prepared:
            mask = (x >= common_xmin - 1e-9) & (x <= common_xmax + 1e-9)
            x_p, cpu_p, mem_p = x[mask], cpu[mask], mem[mask]
            x_arrays.append(x_p)
            cpu_vals.append(cpu_p)
            mem_vals.append(mem_p)
            paint_under(ax_cpu, x_p, cpu_p, color)
            paint_under(ax_mem, x_p, mem_p, color)
            ax_cpu.plot(x_p, cpu_p, color=color, linewidth=1.8, label=label, zorder=3)
            ax_mem.plot(x_p, mem_p, color=color, linewidth=1.8, label=label, zorder=3)

    x_label = "minutos"
    ax_cpu.set_title("Utilização média da CPU alocada")
    ax_cpu.set_ylabel("%")
    ax_cpu.set_xlabel(x_label)
    ax_cpu.grid(True, alpha=0.3)
    ax_cpu.legend(loc="upper right", fontsize=8)
    if cpu_vals:
        pad_ylim(ax_cpu, np.concatenate(cpu_vals), ymin=0.0)

    ax_mem.set_title("Utilização média da memória alocada")
    ax_mem.set_ylabel("%")
    ax_mem.set_xlabel(x_label)
    ax_mem.grid(True, alpha=0.3)
    ax_mem.legend(loc="upper right", fontsize=8)
    if mem_vals:
        pad_ylim(ax_mem, np.concatenate(mem_vals), ymin=0.0)
    apply_shared_xlim((ax_cpu, ax_mem), x_arrays, pad_minutes=X_PAD_MINUTES)
    for ax in (ax_cpu, ax_mem):
        ax.tick_params(axis="x", labelbottom=True, bottom=True)
        ax.xaxis.set_tick_params(which="both", labelbottom=True)
        ax.set_xlabel(x_label)
    space_subplots(fig, hspace=0.16)

    apply_br_number_format(fig)
    out_path = out_dir / f"perfil-{profile}-recursos.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path

def plot_cost_comparison(costs: dict[tuple[str, str], dict[str, float]], out_dir: Path) -> Path:
    labels = []
    fargate_vals = []
    sqs_vals = []
    for kind, profile in sorted(costs, key=lambda k: (k[1], k[0])):
        labels.append(f"{KIND_PT.get(kind, kind)}\n{profile_title(profile)}")
        fargate_vals.append(costs[(kind, profile)]["fargate"])
        sqs_vals.append(costs[(kind, profile)]["sqs"])

    x = np.arange(len(labels))
    width = 0.55
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    ax.bar(x, fargate_vals, width, label="ECS Fargate", color=COST_COLORS["fargate"])
    ax.bar(x, sqs_vals, width, bottom=fargate_vals, label="SQS", color=COST_COLORS["sqs"])

    totals = [f + s for f, s in zip(fargate_vals, sqs_vals)]
    for i, total in enumerate(totals):
        ax.text(i, total, f"US$ {format_br(total, 3)}", ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Custo estimado na janela (US$)")
    ax.set_title("Custo estimado por topologia e perfil de carga")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(loc="upper right")
    pad_ylim(ax, totals)

    apply_br_number_format(fig, y_only=True)
    out_path = out_dir / "comparacao-custo-aws.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path

def plot_k6_summary_bars(k6_groups: dict[tuple[str, str], list[dict]], out_dir: Path) -> Path | None:
    if not k6_groups:
        return None

    keys = sorted(k6_groups, key=lambda k: (k[1], k[0]))
    labels = [f"{KIND_PT.get(k, k)}\n{profile_title(p)}" for k, p in keys]
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

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.2), constrained_layout=True)
    space_subplots(fig, wspace=0.14)
    colors = [REQUEST_COLOR if k == "request" else EVENT_COLOR for k, _ in keys]

    axes[0].bar(labels, thr, color=colors)
    axes[0].set_title("Operações concluídas pelo cliente")
    axes[0].set_ylabel("operações/s")
    axes[0].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[0], thr)

    axes[1].bar(labels, p95, color=colors)
    axes[1].set_title("Tempo da operação de entrada no cliente, p95")
    axes[1].set_ylabel("ms")
    axes[1].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[1], p95)

    axes[2].bar(labels, fail, color=colors)
    axes[2].set_title("Chamadas não concluídas pelo cliente")
    axes[2].set_ylabel("%")
    axes[2].tick_params(axis="x", labelrotation=30)
    pad_ylim(axes[2], fail)

    for ax in axes:
        ax.grid(True, axis="y", alpha=0.3)
    space_subplots(fig, wspace=0.14)
    apply_br_number_format(fig, y_only=True)

    out_path = out_dir / "resumo-k6-comparacao.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gráficos a partir de results/ (CloudWatch + k6).")
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
        "Nota: latência event usa percentis EMF "
        "(TccMba process.time / Source=event, p50/p95/p99) quando presentes no CSV; "
        "senão cai na média OTEL de process.time."
    )

    for profile, kind_paths in sorted(csvs.items()):
        series_map: dict[str, list[dict]] = {}
        origins: dict[str, datetime | None] = {}
        for kind, path in kind_paths.items():
            rows = fill_minute_grid(load_minute_rows(path))
            attach_k6_client_metrics(rows, kind, profile, k6_groups, profiles)
            trimmed, _window_start = trim_rows_to_k6_window(
                rows, kind, profile, k6_groups, profiles, lead_minutes=2
            )
            series_map[kind] = trimmed
            origins[kind] = first_k6_origin(kind, profile, k6_groups, profiles) or _window_start
            costs[(kind, profile)] = attributed_costs(kind, rows, load_meta_prices(path))

            proc_key = "events_processing_avg_ms" if kind == "event" else "requests_processing_avg_ms"
            proc_vals = [r[proc_key] for r in trimmed if (r.get(proc_key) or 0) > 0]
            fail_peak = max((r.get("k6_failed_rate_pct") or 0.0) for r in trimmed) if trimmed else 0.0
            pct_key = "events_processing_p95_ms" if kind == "event" else "requests_processing_p95_ms"
            pct_vals = [r[pct_key] for r in trimmed if (r.get(pct_key) or 0) > 0]
            print(
                f"Carregado {path.name}: {len(trimmed)} min (recorte k6) | "
                f"process.time amostras>0={len(proc_vals)} "
                f"média={np.mean(proc_vals) if proc_vals else 0:.3f}ms | "
                f"EMF p95 amostras>0={len(pct_vals)} | "
                f"pico de chamadas não concluídas pelo cliente={fail_peak:.2f}%"
            )

        written.append(plot_profile(profile, series_map, out_dir, origins, k6_groups, profiles))
        written.append(plot_timeout_15s(profile, series_map, origins, out_dir))
        written.append(
            plot_k6_client_throughput(profile, series_map, origins, out_dir, k6_groups, profiles)
        )
        sqs_chart = plot_event_sqs_extension(
            profile, series_map, origins, out_dir, k6_groups, profiles
        )
        if sqs_chart:
            written.append(sqs_chart)
        resource_csv = write_resources_csv(profile, series_map, origins, input_dir)
        print(f"CSV recursos: {resource_csv}")
        written.append(plot_resources(profile, series_map, origins, out_dir, k6_groups, profiles))

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
