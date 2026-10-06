"""Exporta a série já alinhada de cada figura e monta um xlsx com uma figura por aba.

O CSV é a fonte do workbook. Cada aba repete o tipo do gráfico original
(linha, barras ou barras empilhadas), com as mesmas séries.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.series import DataPoint
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.label import DataLabelList
from openpyxl.drawing.line import LineProperties
from openpyxl.styles import Font
from openpyxl.worksheet.page import PageMargins

from plot_results import (
    COST_COLORS,
    EVENT_COLOR,
    KIND_PT,
    REQUEST_COLOR,
    overlay_alignment,
    overlay_relative_minutes,
    pick_resource,
    profile_title,
    relative_minutes,
    series,
)

CSV_FIELDS = ["figura", "painel", "tipo", "serie", "eixo", "eixo_x", "valor"]


def _hex(color: str) -> str:
    return color.lstrip("#").upper()


def _xy(series_map, origins, profile, k6_groups, profiles, kind: str, aligned: bool):
    rows = series_map.get(kind)
    if not rows:
        return None
    origin = origins.get(kind) or rows[0]["minute"]
    if not aligned:
        return rows, relative_minutes(rows, origin)
    align = overlay_alignment(series_map, profile, k6_groups, profiles, origins)
    origin, segments, template = align.get(kind, (origin, [], []))
    return rows, overlay_relative_minutes(rows, origin, segments, template)


def _finite_xy(x, y) -> tuple[np.ndarray, np.ndarray]:
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    mask = np.isfinite(x_arr) & np.isfinite(y_arr)
    x_arr, y_arr = x_arr[mask], y_arr[mask]
    if x_arr.size == 0:
        return x_arr, y_arr
    order = np.argsort(x_arr)
    x_arr, y_arr = x_arr[order], y_arr[order]
    uniq, idx = np.unique(x_arr, return_index=True)
    return uniq, y_arr[idx]


def _on_minutes(x, y, grid: np.ndarray) -> np.ndarray:
    x_arr, y_arr = _finite_xy(x, y)
    out = np.full(grid.shape, np.nan)
    if x_arr.size == 0:
        return out
    inside = (grid >= x_arr[0]) & (grid <= x_arr[-1])
    if x_arr.size == 1:
        out[inside] = y_arr[0]
        return out
    out[inside] = np.interp(grid[inside], x_arr, y_arr)
    return out


def _grid_for(pairs: list[tuple[np.ndarray, np.ndarray]], intersection: bool) -> np.ndarray:
    spans = []
    for x, y in pairs:
        x_arr, _ = _finite_xy(x, y)
        if x_arr.size:
            spans.append((float(x_arr[0]), float(x_arr[-1])))
    if not spans:
        return np.array([])
    if intersection and len(spans) > 1:
        left = max(a for a, _ in spans)
        right = min(b for _, b in spans)
    else:
        left = min(a for a, _ in spans)
        right = max(b for _, b in spans)
    if right < left:
        return np.array([])
    start = int(np.floor(left))
    stop = int(np.ceil(right))
    return np.arange(start, stop + 1, dtype=float)


def _append_line(rows: list[dict], figura: str, painel: str, named: list[tuple[str, np.ndarray, np.ndarray]], intersection: bool, secondary: set[str] | None = None) -> None:
    secondary = secondary or set()
    grid = _grid_for([(x, y) for _, x, y in named], intersection)
    if grid.size == 0:
        return
    sampled = [(name, _on_minutes(x, y, grid)) for name, x, y in named]
    for i, minute in enumerate(grid.tolist()):
        if all(not np.isfinite(values[i]) for _, values in sampled):
            continue
        for name, values in sampled:
            if not np.isfinite(values[i]):
                continue
            rows.append(
                {
                    "figura": figura,
                    "painel": painel,
                    "tipo": "linha",
                    "serie": name,
                    "eixo": "y2" if name in secondary else "y",
                    "eixo_x": f"{minute:.0f}",
                    "valor": f"{float(values[i]):.6f}",
                }
            )


def _append_bar(rows: list[dict], figura: str, painel: str, tipo: str, categories: list[str], named: list[tuple[str, list[float]]]) -> None:
    for i, category in enumerate(categories):
        for name, values in named:
            rows.append(
                {
                    "figura": figura,
                    "painel": painel,
                    "tipo": tipo,
                    "serie": name,
                    "eixo": "y",
                    "eixo_x": category,
                    "valor": f"{float(values[i]):.6f}",
                }
            )


def collect_rows(payloads: list[dict], costs: dict, k6_groups: dict) -> list[dict]:
    rows: list[dict] = []
    for item in payloads:
        profile = item["profile"]
        series_map = item["series_map"]
        origins = item["origins"]
        groups = item["k6_groups"]
        profiles = item["profiles"]
        figura = f"{profile}-metricas"
        prepared = {}
        for kind in ("request", "event"):
            got = _xy(series_map, origins, profile, groups, profiles, kind, aligned=True)
            if got:
                prepared[kind] = got

        vazao = []
        falhas = []
        proc = []
        if "request" in prepared:
            data, x = prepared["request"]
            vazao.append(("Requisição", x, series(data, "requests_processed_per_s")))
            falhas.append(("Requisição", x, series(data, "requests_app_error_rate") * 100.0))
            proc.append(("Requisição", x, series(data, "requests_processing_avg_ms")))
            _append_line(
                rows,
                figura,
                "alb",
                [
                    ("p50", x, series(data, "alb_latency_p50_ms")),
                    ("p95", x, series(data, "alb_latency_p95_ms")),
                    ("p99", x, series(data, "alb_latency_p99_ms")),
                ],
                intersection=False,
            )
        if "event" in prepared:
            data, x = prepared["event"]
            vazao.append(("Evento", x, series(data, "events_processed_per_s")))
            falhas.append(("Evento", x, series(data, "events_app_error_rate") * 100.0))
            proc.append(("Evento", x, series(data, "events_processing_avg_ms")))
            p50 = series(data, "events_processing_p50_ms")
            p95 = series(data, "events_processing_p95_ms")
            p99 = series(data, "events_processing_p99_ms")
            if float(np.nanmax(np.nan_to_num(p95, nan=0.0))) > 0:
                event_lat = [("p50", x, p50), ("p95", x, p95), ("p99", x, p99)]
            else:
                event_lat = [("média", x, series(data, "events_processing_avg_ms"))]
            _append_line(rows, figura, "evento", event_lat, intersection=False)
        _append_line(rows, figura, "vazao", vazao, intersection=False)
        _append_line(rows, figura, "excecoes", falhas, intersection=False)
        _append_line(rows, figura, "processamento", proc, intersection=False)

        got = _xy(series_map, origins, profile, groups, profiles, "request", aligned=False)
        if got:
            data, x = got
            _append_line(
                rows,
                f"{profile}-timeout",
                "nao-concluidas",
                [("Requisição", x, series(data, "k6_failed_rate_pct"))],
                intersection=False,
            )

        k6_lines = []
        for kind, label in (("request", "Requisição"), ("event", "Evento")):
            got = _xy(series_map, origins, profile, groups, profiles, kind, aligned=True)
            if not got:
                continue
            data, x = got
            k6_lines.append((label, x, series(data, "k6_client_throughput_ops_s")))
        _append_line(rows, f"{profile}-vazao-k6", "vazao-k6", k6_lines, intersection=True)

        got = _xy(series_map, origins, profile, groups, profiles, "event", aligned=False)
        if got:
            data, x = got
            _append_line(
                rows,
                f"{profile}-sqs",
                "taxa",
                [
                    ("Enviadas à fila", x, series(data, "sqs_sent_per_s")),
                    ("Excluídas pelo consumidor", x, series(data, "sqs_deleted_per_s")),
                ],
                intersection=False,
            )
            _append_line(
                rows,
                f"{profile}-sqs",
                "fila",
                [
                    ("Mensagens visíveis", x, series(data, "sqs_visible_avg")),
                    ("Idade da mensagem mais antiga", x, series(data, "sqs_oldest_age_s")),
                ],
                intersection=False,
                secondary={"Idade da mensagem mais antiga"},
            )

        cpu = []
        mem = []
        for kind, label in (("request", "Requisição"), ("event", "Evento")):
            got = _xy(series_map, origins, profile, groups, profiles, kind, aligned=True)
            if not got:
                continue
            data, x = got
            cpu.append((label, x, np.array([pick_resource(r, kind)["cpu_percent_avg"] for r in data], dtype=float)))
            mem.append((label, x, np.array([pick_resource(r, kind)["memory_percent_avg"] for r in data], dtype=float)))
        _append_line(rows, f"{profile}-recursos", "cpu", cpu, intersection=True)
        _append_line(rows, f"{profile}-recursos", "memoria", mem, intersection=True)

    if costs:
        categories = []
        fargate = []
        sqs = []
        for kind, profile in sorted(costs, key=lambda k: (k[1], k[0])):
            categories.append(f"{KIND_PT.get(kind, kind)} {profile_title(profile)}")
            fargate.append(costs[(kind, profile)]["fargate"])
            sqs.append(costs[(kind, profile)]["sqs"])
        _append_bar(rows, "custo", "custo", "barras_empilhadas", categories, [("ECS Fargate", fargate), ("Amazon SQS", sqs)])

    if k6_groups:
        keys = sorted(k6_groups, key=lambda k: (k[1], k[0]))
        categories = [f"{KIND_PT.get(k, k)} {profile_title(p)}" for k, p in keys]
        thr = []
        for key in keys:
            runs = k6_groups[key]
            thr.append(
                float(
                    np.mean(
                        [
                            r.get("throughput_req_s")
                            if r.get("throughput_req_s") is not None
                            else r.get("throughput_events_s") or 0.0
                            for r in runs
                        ]
                    )
                )
            )
        _append_bar(rows, "k6-resumo", "vazao", "barras", categories, [("operações/s", thr)])
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _light_grid() -> ChartLines:
    lines = ChartLines()
    lines.spPr = GraphicalProperties(ln=LineProperties(solidFill="D0D0D0", w=4000))
    return lines


def _style(chart, y_title: str, x_title: str | None) -> None:
    chart.y_axis.title = y_title
    if x_title:
        chart.x_axis.title = x_title
    chart.y_axis.majorGridlines = _light_grid()
    chart.x_axis.majorGridlines = None
    chart.y_axis.scaling.min = 0
    chart.displayBlanksAs = "gap"
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showVal = False
    if chart.legend is not None:
        chart.legend.position = "b"


SERIES_STYLE = {
    "Requisição": (_hex(REQUEST_COLOR), "solid"),
    "Evento": (_hex(EVENT_COLOR), "solid"),
    "p50": (_hex(REQUEST_COLOR), "solid"),
    "p95": (_hex(REQUEST_COLOR), "dash"),
    "p99": (_hex(REQUEST_COLOR), "sysDot"),
    "média": (_hex(EVENT_COLOR), "solid"),
    "Enviadas à fila": (_hex(EVENT_COLOR), "solid"),
    "Excluídas pelo consumidor": ("2CA02C", "solid"),
    "Mensagens visíveis": ("D62728", "solid"),
    "Idade da mensagem mais antiga": ("9467BD", "dash"),
    "ECS Fargate": (_hex(COST_COLORS["fargate"]), "solid"),
    "Amazon SQS": (_hex(COST_COLORS["sqs"]), "solid"),
}

EVENT_PERCENTILE = {
    "p50": (_hex(EVENT_COLOR), "solid"),
    "p95": (_hex(EVENT_COLOR), "dash"),
    "p99": (_hex(EVENT_COLOR), "sysDot"),
    "média": (_hex(EVENT_COLOR), "solid"),
}

PANEL_META = {
    "vazao": ("Unidades processadas pela aplicação", "unidades/s", "minutos"),
    "alb": ("Percentis de tempo do ALB na requisição", "ms", "minutos"),
    "evento": ("Percentis de processamento no evento", "ms", "minutos"),
    "excecoes": ("Percentual de exceções da aplicação", "%", "minutos"),
    "processamento": ("Tempo médio de processamento da aplicação", "ms", "minutos"),
    "nao-concluidas": ("Requisições não concluídas pelo cliente", "%", "minutos"),
    "vazao-k6": ("Vazão medida pelo cliente k6", "operações/s", "minutos"),
    "taxa": ("Publicação vs consumo de mensagens SQS", "mensagens/s", "minutos"),
    "fila": ("Mensagens acumuladas na fila", "mensagens", "minutos"),
    "cpu": ("Utilização média da CPU alocada", "%", "minutos"),
    "memoria": ("Utilização média da memória alocada", "%", "minutos"),
    "custo": ("Custo estimado por topologia e perfil de carga", "US$", None),
}

K6_PANEL_META = {
    "vazao": ("Operações emitidas pelo cliente", "operações/s"),
    "p95": ("Tempo da operação de entrada no cliente, p95", "ms"),
    "falha": ("Chamadas não concluídas pelo cliente", "%"),
}

PANEL_SHEET = {
    "vazao": "vazão",
    "alb": "ALB",
    "evento": "evento",
    "excecoes": "exceções",
    "processamento": "processamento",
    "nao-concluidas": "não concluídas",
    "vazao-k6": "vazão k6",
    "taxa": "fila taxa",
    "fila": "fila acúmulo",
    "cpu": "CPU",
    "memoria": "memória",
    "custo": "custo",
    "p95": "p95",
    "falha": "falha",
}


def _sheet_title(figura: str, painel: str) -> str:
    names = {"spike": "Pico", "steady": "Estável", "custo": "Custo", "k6-resumo": "k6"}
    panel = PANEL_SHEET.get(painel, painel)
    if figura in {"custo", "k6-resumo"}:
        prefix = names[figura]
    else:
        profile = figura.split("-", 1)[0]
        prefix = names.get(profile, profile)
    if figura == "custo":
        return "Custo"
    return f"{prefix} - {panel}"[:31]


def _paint_lines(chart, names: list[str], painel: str) -> None:
    palette = EVENT_PERCENTILE if painel == "evento" else SERIES_STYLE
    for serie, name in zip(chart.series, names):
        color, dash = palette.get(name, ("1F77B4", "solid"))
        graf = serie.graphicalProperties
        graf.line = LineProperties(w=18000, solidFill=color, prstDash=dash)
        if serie.marker is not None:
            serie.marker.symbol = "none"


def _paint_bars(chart, names: list[str], categories: list[str]) -> None:
    for serie, name in zip(chart.series, names):
        if name in SERIES_STYLE and name not in {"Requisição", "Evento", "p50"}:
            color, _dash = SERIES_STYLE[name]
            serie.graphicalProperties.solidFill = color
            continue
        for i, category in enumerate(categories):
            color = _hex(REQUEST_COLOR) if category.startswith("Requisição") else _hex(EVENT_COLOR)
            point = DataPoint(idx=i)
            point.graphicalProperties.solidFill = color
            serie.data_points.append(point)


def _add_chart(
    ws,
    anchor: str,
    width: float,
    height: float,
    start_col: int,
    headers: list[str],
    n_rows: int,
    painel: str,
    tipo: str,
    y_title: str,
    x_title: str | None,
    secondary_names: list[str],
) -> None:
    primary = [name for name in headers[1:] if name not in secondary_names]
    cats = Reference(ws, min_col=start_col, min_row=2, max_row=n_rows)
    if tipo == "linha":
        chart = LineChart()
        data = Reference(ws, min_col=start_col + 1, min_row=1, max_col=start_col + len(primary), max_row=n_rows)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        _style(chart, y_title, x_title or "minutos")
        _paint_lines(chart, primary, painel)
        if secondary_names:
            other = LineChart()
            other.y_axis.axId = 200
            other.y_axis.title = "s"
            other.y_axis.scaling.min = 0
            sec_col = start_col + headers.index(secondary_names[0])
            sec_data = Reference(ws, min_col=sec_col, min_row=1, max_col=sec_col, max_row=n_rows)
            other.add_data(sec_data, titles_from_data=True)
            other.set_categories(cats)
            _paint_lines(other, secondary_names, painel)
            other.y_axis.crosses = "max"
            chart.y_axis.crosses = "min"
            chart += other
    else:
        chart = BarChart()
        chart.type = "col"
        chart.grouping = "stacked" if tipo == "barras_empilhadas" else "clustered"
        chart.overlap = 100 if tipo == "barras_empilhadas" else 0
        data = Reference(ws, min_col=start_col + 1, min_row=1, max_col=start_col + len(headers) - 1, max_row=n_rows)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        _style(chart, y_title, x_title)
        _paint_bars(chart, headers[1:], [ws.cell(r, start_col).value for r in range(2, n_rows + 1)])
    title, _y, _x = _panel_title(painel, y_title)
    chart.title = title
    chart.width = width
    chart.height = height
    ws.add_chart(chart, anchor)


def _panel_title(painel: str, y_title: str) -> tuple[str, str, str | None]:
    if painel in K6_PANEL_META and y_title in {"operações/s", "ms", "%"}:
        title, axis = K6_PANEL_META[painel]
        return title, axis, None
    meta = PANEL_META.get(painel)
    if meta:
        return meta
    return painel, y_title, "minutos"


def _write_block(ws, start_col: int, headers: list[str], table: list[list]) -> tuple[int, int]:
    for col, header in enumerate(headers, start=start_col):
        cell = ws.cell(1, col, header)
        cell.font = Font(name="Arial", size=10, bold=True)
    for r, row in enumerate(table, start=2):
        for c, value in enumerate(row, start=start_col):
            cell = ws.cell(r, c, value)
            cell.font = Font(name="Arial", size=10)
    return start_col, len(table) + 1


def build_workbook(rows: list[dict], path: Path) -> None:
    grouped: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        grouped.setdefault(row["figura"], {}).setdefault(row["painel"], []).append(row)

    wb = Workbook()
    first = True
    for figura, panels in grouped.items():
        for painel, records in panels.items():
            ws = wb.active if first else wb.create_sheet()
            first = False
            ws.title = _sheet_title(figura, painel)
            tipo = records[0]["tipo"]
            order: list[str] = []
            for record in records:
                if record["serie"] not in order:
                    order.append(record["serie"])
            categories: list[str] = []
            for record in records:
                if record["eixo_x"] not in categories:
                    categories.append(record["eixo_x"])
            lookup = {(rec["eixo_x"], rec["serie"]): float(rec["valor"]) for rec in records}
            headers = ["eixo"] + order
            table = []
            for category in categories:
                eixo = float(category) if tipo == "linha" else category
                table.append([eixo] + [lookup.get((category, name)) for name in order])
            start, n_rows = _write_block(ws, 40, headers, table)
            y_title = PANEL_META.get(painel, (painel, "", None))[1]
            x_title = PANEL_META.get(painel, (painel, "", "minutos"))[2]
            if figura == "k6-resumo":
                y_title = K6_PANEL_META[painel][1]
                x_title = None
            secondary = list(dict.fromkeys(rec["serie"] for rec in records if rec["eixo"] == "y2"))
            _add_chart(
                ws, "A1", 26, 14, start, headers, n_rows, painel, tipo, y_title, x_title, secondary
            )
            ws.page_setup.orientation = "landscape"
            ws.page_setup.paperSize = ws.PAPERSIZE_A4
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 1
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.4, bottom=0.4)
            ws.sheet_view.showGridLines = False
            ws.sheet_view.zoomScale = 110
            ws.print_area = "A1:R32"
            ws.oddHeader.left.text = ws.title
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def export_chart_files(out_dir: Path, payloads: list[dict], costs: dict, k6_groups: dict) -> tuple[Path, Path]:
    rows = collect_rows(payloads, costs, k6_groups)
    csv_path = out_dir / "dados-graficos.csv"
    xlsx_path = out_dir / "graficos.xlsx"
    write_csv(csv_path, rows)
    loaded = list(csv.DictReader(csv_path.open(encoding="utf-8-sig", newline="")))
    build_workbook(loaded, xlsx_path)
    return csv_path, xlsx_path
