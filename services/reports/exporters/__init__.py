"""Turn a `ReportResult` into a file. One module per format, each with
`render(result, options) -> bytes`, `CONTENT_TYPE` and `EXTENSION`; `export` picks one."""
from __future__ import annotations

from services.reports.exporters import csv_export, pdf_export, xlsx_export
from services.reports.exporters.formatting import file_name

FORMATS = {"csv": csv_export, "xlsx": xlsx_export, "pdf": pdf_export}


def export(result: dict, fmt: str, options: dict) -> tuple[bytes, str, str]:
    """`(content, content_type, file_name)`."""
    module = FORMATS[fmt]
    return module.render(result, options), module.CONTENT_TYPE, file_name(result, module.EXTENSION)
