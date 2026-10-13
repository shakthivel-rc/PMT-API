"""PDF export: a printable page of the report — title and facts, KPI tiles, the chart, then
every table, paginated with its header repeated. Drawn with `pdf_writer`."""
from __future__ import annotations

from services.reports.exporters.formatting import cell_text, footer_lines, number_text, tables
from services.reports.exporters.pdf_writer import PdfDocument, fit, text_width
from utils import report_math as rm

CONTENT_TYPE = "application/pdf"
EXTENSION = "pdf"

#: A PDF is for reading; past this many rows a table says so and stops. The CSV or XLSX of
#: the same report has every row.
MAX_PDF_ROWS = 5_000

_MARGIN = 36.0
_ROW = 14.0
_INK = (0.12, 0.13, 0.16)
_MUTED = (0.42, 0.44, 0.5)
_RULE = (0.85, 0.86, 0.9)
_HEADER_FILL = (0.93, 0.94, 0.97)
#: Okabe-Ito, the colour-blind-safe palette the SPA's charts use too.
PALETTE = [(0.0, 0.447, 0.698), (0.902, 0.624, 0.0), (0.0, 0.62, 0.451),
           (0.8, 0.475, 0.655), (0.337, 0.706, 0.914), (0.835, 0.369, 0.0),
           (0.941, 0.894, 0.259), (0.4, 0.4, 0.4)]
_NAMED = {"success": (0.0, 0.62, 0.451), "danger": (0.835, 0.369, 0.0),
          "muted": (0.6, 0.6, 0.65)}


class _Layout:
    def __init__(self, doc: PdfDocument, footer: str):
        self.doc = doc
        self.footer = footer
        self.y = 0.0
        self.new_page()

    def new_page(self) -> None:
        self.doc.add_page()
        self.y = self.doc.height - _MARGIN
        self.doc.text(_MARGIN, 18, f"{self.footer} — page {self.doc.page_count}", 7, color=_MUTED)

    def need(self, height: float) -> bool:
        """Start a new page unless `height` fits; True if it did."""
        if self.y - height < _MARGIN:
            self.new_page()
            return True
        return False


def _number(value, unit: str | None) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 3600 if unit == "duration" else number


def _draw_chart(layout: _Layout, chart: dict, width: float) -> None:
    kind = chart.get("kind")
    doc = layout.doc
    height = 150.0
    layout.need(height + 40)
    top = layout.y
    bottom = top - height
    left = _MARGIN + 34
    right = _MARGIN + width
    unit = chart.get("unit")
    series = chart.get("series") or []
    labels = [str(x) for x in chart.get("x") or []][:60]

    if kind == "scatter":
        points = chart.get("points") or []
        lines = chart.get("lines") or []
        ys = [p["y"] for p in points if p.get("y") is not None]
        top_value = max(ys + [r["value"] or 0 for r in chart.get("references") or []] + [1])
        n = max(len(points), 1)
        _axes(doc, left, bottom, right, top, top_value, unit)
        for index, point in enumerate(points):
            x = left + (right - left) * (index + 0.5) / n
            y = bottom + (top - bottom) * (point["y"] or 0) / top_value
            doc.rect(x - 1.5, y - 1.5, 3, 3, fill=PALETTE[0])
        for line in lines:
            pts = [(left + (right - left) * (i + 0.5) / n,
                    bottom + (top - bottom) * (p["y"] or 0) / top_value)
                   for i, p in enumerate(line.get("points") or [])]
            doc.polyline(pts, PALETTE[1])
        for ref in chart.get("references") or []:
            if ref.get("value") is not None:
                y = bottom + (top - bottom) * ref["value"] / top_value
                doc.line(left, y, right, y, _NAMED["muted"], dash=True)
                doc.text(right - 90, y + 2, f"{ref['label']}: {ref['value']}", 6.5, color=_MUTED)
        legend = [("Issues", PALETTE[0])] + [(ln["label"], PALETTE[1]) for ln in lines]
    elif kind == "donut":
        values = [_number(v, unit) for v in (series[0]["values"] if series else [])]
        total = sum(values) or 1
        x = left
        for index, value in enumerate(values):
            w = (right - left) * value / total
            doc.rect(x, bottom + height / 2 - 12, w, 24, fill=PALETTE[index % len(PALETTE)])
            x += w
        legend = [(f"{labels[i] if i < len(labels) else ''}: {values[i]:g}",
                   PALETTE[i % len(PALETTE)]) for i in range(len(values))]
    else:
        n = max(len(labels), 1)
        matrix = [[_number(v, unit) for v in (s.get("values") or [])[:60]] for s in series]
        stacked = kind in ("stacked_bar", "stacked_area")
        if stacked:
            sums = [sum(col[i] for col in matrix if i < len(col)) for i in range(n)]
            top_value = max([*sums, 1])
        else:
            top_value = max([v for col in matrix for v in col] + [1])
        _axes(doc, left, bottom, right, top, top_value, unit)
        slot = (right - left) / n
        if kind in ("line", "stacked_area"):
            base = [0.0] * n
            for s_index, values in enumerate(matrix):
                color = _series_color(series[s_index], s_index)
                pts = []
                for i, value in enumerate(values):
                    raw = (series[s_index].get("values") or [])[i]
                    if raw is None:
                        continue
                    level = value + (base[i] if stacked else 0)
                    y = bottom + (top - bottom) * level / top_value
                    pts.append((left + slot * (i + 0.5), y))
                    if stacked:
                        base[i] = level
                doc.polyline(pts, color, dash=bool(series[s_index].get("dashed")))
        else:
            base = [0.0] * n
            groups = len(matrix) if kind == "grouped_bar" or kind == "bullet" else 1
            for s_index, values in enumerate(matrix):
                color = _series_color(series[s_index], s_index)
                for i, value in enumerate(values):
                    h = (top - bottom) * value / top_value
                    if stacked:
                        y0 = bottom + (top - bottom) * base[i] / top_value
                        doc.rect(left + slot * i + slot * 0.15, y0, slot * 0.7, h, fill=color)
                        base[i] += value
                    else:
                        w = slot * 0.7 / groups
                        offset = slot * 0.15 + (w * s_index if groups > 1 else 0)
                        doc.rect(left + slot * i + offset, bottom, w, h, fill=color)
            reference = chart.get("reference")
            if reference and reference.get("value") is not None:
                y = bottom + (top - bottom) * _number(reference["value"], unit) / top_value
                doc.line(left, y, right, y, _NAMED["muted"], dash=True)
        step = max(1, n // 12)
        for i in range(0, n, step):
            if i < len(labels):
                doc.text(left + slot * i + 1, bottom - 10, fit(labels[i], slot * step - 2, 6), 6,
                         color=_MUTED)
        legend = [(str(s.get("label", "")), _series_color(s, i)) for i, s in enumerate(series)]
    y = bottom - 24
    x = left
    for label, color in legend[:12]:
        label = fit(label, 120, 7)
        doc.rect(x, y, 7, 7, fill=color)
        doc.text(x + 10, y + 0.5, label, 7, color=_MUTED)
        x += text_width(label, 7) + 24
        if x > right - 100:
            break
    layout.y = y - 14


def _series_color(series: dict, index: int):
    return _NAMED.get(str(series.get("color")), PALETTE[index % len(PALETTE)])


def _axes(doc: PdfDocument, left, bottom, right, top, top_value, unit) -> None:
    doc.line(left, bottom, right, bottom, _RULE)
    for fraction in (0.5, 1.0):
        y = bottom + (top - bottom) * fraction
        doc.line(left, y, right, y, _RULE, width=0.3)
        doc.text(_MARGIN, y - 3, f"{top_value * fraction:.4g}{'h' if unit == 'duration' else ''}",
                 6.5, color=_MUTED)


def _draw_table(layout: _Layout, table: dict, options: dict, width: float) -> None:
    doc = layout.doc
    columns = table["columns"]
    if not columns:
        return
    weights = [3.0 if c["type"] in ("text", "user") else 1.4 for c in columns]
    scale = width / sum(weights)
    widths = [w * scale for w in weights]

    def header() -> None:
        doc.rect(_MARGIN, layout.y - _ROW + 3, width, _ROW, fill=_HEADER_FILL)
        x = _MARGIN
        for col, w in zip(columns, widths, strict=True):
            doc.text(x + 3, layout.y - 7, fit(str(col["label"]), w - 6, 7.5, True), 7.5, True)
            x += w
        layout.y -= _ROW

    layout.need(_ROW * 4)
    doc.text(_MARGIN, layout.y - 10, str(table["title"]), 11, True)
    layout.y -= 18
    header()
    rows = table["rows"]
    shown = rows[:MAX_PDF_ROWS]
    lines = [(row, False) for row in shown]
    if table.get("totals"):
        lines.append((table["totals"], True))
    if not rows:
        doc.text(_MARGIN + 3, layout.y - 7, "Nothing in this period.", 8, color=_MUTED)
        layout.y -= _ROW
    for row, is_total in lines:
        if layout.need(_ROW):
            header()
        x = _MARGIN
        for position, (col, w) in enumerate(zip(columns, widths, strict=True)):
            value = row.get(col["key"])
            text = cell_text(value, col["type"], options).lstrip("'") if value is not None \
                else ""
            if position == 0 and row.get("_depth"):
                text = "   " * int(row["_depth"]) + text
            numeric = col["type"] in ("number", "duration", "percent")
            text = fit(text, w - 6, 7.5, is_total)
            tx = x + w - 3 - text_width(text, 7.5, is_total) if numeric else x + 3
            doc.text(tx, layout.y - 7, text, 7.5, is_total)
            x += w
        doc.line(_MARGIN, layout.y - _ROW + 3, _MARGIN + width, layout.y - _ROW + 3, _RULE, 0.3)
        layout.y -= _ROW
    if len(rows) > MAX_PDF_ROWS:
        doc.text(_MARGIN, layout.y - 8, f"{len(rows) - MAX_PDF_ROWS:,} more rows are in the "
                                        "CSV or XLSX export of this report.", 7.5, color=_MUTED)
        layout.y -= _ROW
    layout.y -= 10


def render(result: dict, options: dict) -> bytes:
    meta = result["meta"]
    doc = PdfDocument(title=str(meta.get("title", "Report")))
    layout = _Layout(doc, f"PMT — {meta.get('title', '')}")
    width = doc.width - 2 * _MARGIN
    doc.text(_MARGIN, layout.y - 14, fit(str(meta.get("title", "")), width, 16, True), 16, True)
    layout.y -= 28
    facts = [str(meta.get("scope_name", ""))]
    if meta.get("date_from"):
        facts.append(f"{meta.get('date_from')} to {meta.get('date_to')}")
    facts.append(f"Time zone {meta.get('timezone', 'UTC')}")
    facts.append(f"Generated {str(meta.get('generated_at', ''))[:16]} UTC")
    doc.text(_MARGIN, layout.y, fit(" · ".join(facts), width, 8.5), 8.5, color=_MUTED)
    layout.y -= 18
    if meta.get("goal"):
        doc.text(_MARGIN, layout.y, fit(f"Goal: {meta['goal']}", width, 9), 9)
        layout.y -= 16

    kpis = result.get("kpis") or []
    if kpis:
        per_row = 6
        tile = (width - (per_row - 1) * 8) / per_row
        for start in range(0, len(kpis), per_row):
            layout.need(48)
            for index, item in enumerate(kpis[start:start + per_row]):
                x = _MARGIN + index * (tile + 8)
                doc.rect(x, layout.y - 40, tile, 40, stroke=_RULE)
                doc.text(x + 6, layout.y - 13, fit(str(item["label"]), tile - 12, 7), 7,
                         color=_MUTED)
                value = item.get("value")
                if item.get("type") == "duration":
                    shown = rm.human_duration(value) if value is not None else "—"
                elif item.get("type") == "percent":
                    shown = f"{number_text(value)}%" if value is not None else "—"
                else:
                    shown = "—" if value is None else number_text(value)
                doc.text(x + 6, layout.y - 31, fit(shown, tile - 12, 13, True), 13, True)
            layout.y -= 52

    chart = result.get("chart")
    if chart and chart.get("kind") not in (None, "none") and (
            chart.get("series") or chart.get("points")):
        _draw_chart(layout, chart, width)

    for table in tables(result):
        _draw_table(layout, table, options, width)

    layout.need(_ROW * 9)
    for label, value in footer_lines(result, options):
        doc.text(_MARGIN, layout.y - 7, f"{label}:", 7.5, True, color=_MUTED)
        doc.text(_MARGIN + 70, layout.y - 7, fit(value, width - 70, 7.5), 7.5, color=_MUTED)
        layout.y -= 11
    return doc.render()
