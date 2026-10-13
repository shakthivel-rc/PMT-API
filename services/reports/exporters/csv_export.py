"""CSV export: UTF-8 with a byte-order mark, so Excel opens it as UTF-8 rather than in the
machine's ANSI code page, and every text cell guarded against formula injection."""
from __future__ import annotations

import csv
import io

from services.reports.exporters.formatting import cell_text, footer_lines, guard, tables

CONTENT_TYPE = "text/csv; charset=utf-8"
EXTENSION = "csv"


def render(result: dict, options: dict) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for index, table in enumerate(tables(result)):
        if index:
            writer.writerow([])
            writer.writerow([guard(str(table["title"]))])
        columns = table["columns"]
        writer.writerow([guard(str(c["label"])) for c in columns])
        for row in table["rows"]:
            prefix = "  " * int(row.get("_depth", 0) or 0)
            cells = []
            for position, col in enumerate(columns):
                text = cell_text(row.get(col["key"]), col["type"], options)
                cells.append(prefix + text if position == 0 and prefix else text)
            writer.writerow(cells)
        totals = table.get("totals")
        if totals:
            writer.writerow([cell_text(totals.get(c["key"]), c["type"], options)
                             if c["key"] in totals else "" for c in columns])
    writer.writerow([])
    for label, value in footer_lines(result, options):
        writer.writerow([label, guard(value)])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
