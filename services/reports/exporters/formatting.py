"""Cell formatting shared by the exporters, so CSV, XLSX and PDF say the same thing."""
from __future__ import annotations

from datetime import date, datetime

from utils import report_math as rm

#: A cell that starts with one of these is a formula to Excel, LibreOffice and Sheets. A
#: report carries user-typed text (issue titles, notes), so a title of `=HYPERLINK(…)` would
#: run in whoever opens the export (OWASP "CSV injection"). Such text gets a leading `'`.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_TEXT_TYPES = {"text", "user", "issue"}


def guard(text: str) -> str:
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _as_date(value):
    if isinstance(value, str):
        try:
            if "T" in value or " " in value:
                return datetime.fromisoformat(value)
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def cell_text(value, kind: str, options: dict) -> str:
    """A cell as text, the way CSV and PDF show it."""
    if value is None:
        return ""
    if kind == "duration":
        return rm.format_duration(value, options.get("duration_format", "decimal"))
    if kind in ("date", "datetime"):
        parsed = _as_date(value)
        if isinstance(parsed, (date, datetime)):
            return rm.format_date(parsed, options.get("date_format", "iso"))
        return str(value)
    if kind == "boolean":
        return "Yes" if value else "No"
    if kind in ("percent", "number"):
        return number_text(value)
    text = str(value)
    return guard(text) if kind in _TEXT_TYPES else text


def footer_lines(result: dict, options: dict) -> list[tuple[str, str]]:
    """The facts every export states at its end (docs/design/08-reports.md §5.5)."""
    meta = result["meta"]
    rounding = meta.get("rounding_minutes") or 0
    lines = [
        ("Report", str(meta.get("title", ""))),
        ("Scope", str(meta.get("scope_name", ""))),
    ]
    if meta.get("date_from"):
        lines.append(("Range", f"{_iso(meta.get('date_from'))} to {_iso(meta.get('date_to'))}"))
    lines += [
        ("Time zone", str(meta.get("timezone", "UTC"))),
        ("Generated", f"{_iso(meta.get('generated_at'))} UTC"),
        ("Rounding", f"each entry rounded {meta.get('rounding_mode')} to {rounding} minutes"
         if rounding else "none"),
        ("Durations", {"decimal": "hours, 2 decimals", "hhmm": "hours:minutes",
                       "iso": "ISO 8601", "seconds": "seconds"}[
            options.get("duration_format", "decimal")]),
    ]
    if meta.get("restricted_to_own"):
        lines.append(("Visibility", "only your own time is listed"))
    for note in meta.get("notes") or []:
        lines.append(("Note", str(note)))
    return lines


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def number_text(value) -> str:
    """`8`, not `8.0`; `2.5` stays `2.5`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def tables(result: dict) -> list[dict]:
    """The main table, then each section, as `{title, columns, rows, totals}`."""
    out = [{"key": "report", "title": result["meta"].get("title", "Report"),
            "columns": result["columns"], "rows": result["rows"],
            "totals": result.get("totals")}]
    for item in result.get("sections") or []:
        out.append(item)
    return out


def file_name(result: dict, extension: str) -> str:
    meta = result["meta"]
    base = str(meta.get("type", "report"))
    stamp = _iso(meta.get("date_from")) or _iso(meta.get("generated_at"))[:10]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in f"{base}-{stamp}")
    return f"pmt-{safe}.{extension}"
