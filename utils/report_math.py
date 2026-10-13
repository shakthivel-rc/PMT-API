"""Pure arithmetic behind the reports: date presets, buckets, working days, the burndown
guideline, percentiles, rounding and duration formats.

No database, no project imports — like `utils/time_tracking.py`, so every rule here is
unit-tested on its own (`tests/unit/test_report_math.py`) and inside `make typecheck`.
docs/design/08-reports.md §5.5 defines each metric in words; this module is that section in
code, and a change to one is a change to the other.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Date presets a report accepts, ClickUp's set plus quarters (docs/design/08-reports.md §4).
DATE_PRESETS: tuple[str, ...] = (
    "today", "yesterday", "this_week", "last_week", "this_month", "last_month",
    "last_30_days", "this_quarter", "last_quarter", "this_year", "custom",
)

#: Rounding a report may apply to each entry before summing. Toggl's common intervals; 6 is
#: the legal profession's tenth of an hour. Storage is never rounded (models/time_tracking.py).
ROUNDING_MINUTES: tuple[int, ...] = (0, 1, 5, 6, 10, 15, 30)
ROUNDING_MODES: tuple[str, ...] = ("up", "down", "nearest")

DURATION_FORMATS: tuple[str, ...] = ("decimal", "hhmm", "iso", "seconds")
DATE_FORMATS: tuple[str, ...] = ("iso", "normal", "epoch")

BUCKETS: tuple[str, ...] = ("day", "week", "month")

#: Monday to Friday, as `date.weekday()` numbers. The workspace setting overrides it.
DEFAULT_WORKING_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4)

#: How many points the control chart's rolling average spans. Jira's control chart widens
#: its window with the number of issues; a fixed five keeps the line readable for a small
#: team and still smooths one outlier.
ROLLING_WINDOW = 5

_SUNDAY = 6


def zone(name: str | None) -> ZoneInfo:
    """The IANA zone `name`, or UTC when it is empty or unknown.

    Unknown falls back instead of failing because the name comes from a stored settings
    document, and one bad value must not take every report in a workspace down; the settings
    schema refuses unknown names on the way in (schemas/reports.ReportsSettings).
    """
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def local_today(now_utc: datetime, tz: ZoneInfo) -> date:
    """Today's date in `tz`, from a naive-UTC or aware instant."""
    aware = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=ZoneInfo("UTC"))
    return aware.astimezone(tz).date()


def end_of_day_utc(day: date, tz: ZoneInfo) -> datetime:
    """The instant `day` ends in `tz` — midnight starting the next day — as naive UTC, the
    form every DateTime column here stores. Daylight saving changes are handled by zoneinfo:
    a 23- or 25-hour day ends when the wall clock says it does."""
    local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def start_of_day_utc(day: date, tz: ZoneInfo) -> datetime:
    return end_of_day_utc(day - timedelta(days=1), tz)


def local_date(instant_utc: datetime, tz: ZoneInfo) -> date:
    """The calendar day a naive-UTC instant falls on in `tz`."""
    return instant_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()


def week_start_of(day: date, week_start: str = "monday") -> date:
    first = _SUNDAY if week_start == "sunday" else 0
    return day - timedelta(days=(day.weekday() - first) % 7)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def preset_range(preset: str, today: date, week_start: str = "monday") -> tuple[date, date]:
    """The inclusive date range a preset names, relative to `today` in the workspace zone.

    "This" periods run to the end of the period, not to today: a report of this week shows
    the empty days ahead, which is what a timesheet shows too.
    """
    if preset == "today":
        return today, today
    if preset == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if preset in ("this_week", "last_week"):
        start = week_start_of(today, week_start)
        if preset == "last_week":
            start -= timedelta(days=7)
        return start, start + timedelta(days=6)
    if preset in ("this_month", "last_month"):
        start = _month_start(today)
        if preset == "last_month":
            start = _add_months(start, -1)
        return start, _add_months(start, 1) - timedelta(days=1)
    if preset == "last_30_days":
        return today - timedelta(days=29), today
    if preset in ("this_quarter", "last_quarter"):
        start = _quarter_start(today)
        if preset == "last_quarter":
            start = _add_months(start, -3)
        return start, _add_months(start, 3) - timedelta(days=1)
    if preset == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    raise ValueError(f"unknown preset {preset!r}")


def days_between(start: date, end: date) -> list[date]:
    """Every day from `start` to `end`, both included."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def bucket_key(day: date, bucket: str, week_start: str = "monday") -> date:
    """The first day of the bucket `day` falls in."""
    if bucket == "day":
        return day
    if bucket == "week":
        return week_start_of(day, week_start)
    if bucket == "month":
        return _month_start(day)
    raise ValueError(f"unknown bucket {bucket!r}")


def bucket_keys(start: date, end: date, bucket: str, week_start: str = "monday") -> list[date]:
    """The buckets covering `start`..`end`, in order, each once."""
    keys: list[date] = []
    for day in days_between(start, end):
        key = bucket_key(day, bucket, week_start)
        if not keys or keys[-1] != key:
            keys.append(key)
    return keys


def guideline(total: float, days: Sequence[date],
              working_days: Iterable[int] = DEFAULT_WORKING_DAYS) -> list[float]:
    """The burndown's ideal line: the remaining total at the end of each day, falling evenly
    over the working days and flat over the rest (Jira's grey line).

    With no working day in the range at all, the line falls on the last day, so a sprint
    that is all weekend still ends at zero rather than never.
    """
    working = set(working_days)
    count = sum(1 for day in days if day.weekday() in working)
    values: list[float] = []
    burned = 0
    for index, day in enumerate(days):
        if count == 0:
            values.append(0.0 if index == len(days) - 1 else float(total))
            continue
        if day.weekday() in working:
            burned += 1
        values.append(round(total * (1 - burned / count), 4))
    return values


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """The `fraction` percentile (0..1) by linear interpolation between closest ranks — the
    method spreadsheets and numpy use by default, so a reader can check it in either."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def rolling_average(values: Sequence[float], window: int = ROLLING_WINDOW) -> list[float]:
    """For each point, the mean of it and up to `window - 1` points before it."""
    result: list[float] = []
    for index in range(len(values)):
        chunk = values[max(0, index - window + 1): index + 1]
        result.append(sum(chunk) / len(chunk))
    return result


def round_seconds(seconds: int, minutes: int, mode: str) -> int:
    """`seconds` rounded to a multiple of `minutes`. 0 minutes means no rounding.

    Applied to each entry before summing (docs/design/08-reports.md §5.5): rounding a sum
    instead would make a report's total disagree with the sum of its own rows.
    """
    if minutes <= 0:
        return seconds
    step = minutes * 60
    if mode == "up":
        return -(-seconds // step) * step
    if mode == "down":
        return (seconds // step) * step
    return math.floor(seconds / step + 0.5) * step


def format_duration(seconds: int | float | None, style: str = "decimal") -> str:
    """A duration as text for an export cell. `decimal` is hours to two places."""
    if seconds is None:
        return ""
    whole = round(seconds)
    if style == "seconds":
        return str(whole)
    if style == "hhmm":
        sign = "-" if whole < 0 else ""
        whole = abs(whole)
        return f"{sign}{whole // 3600}:{(whole % 3600) // 60:02d}"
    if style == "iso":
        sign = "-" if whole < 0 else ""
        whole = abs(whole)
        hours, rest = divmod(whole, 3600)
        mins, secs = divmod(rest, 60)
        parts = (f"{hours}H" if hours else "") + (f"{mins}M" if mins else "") + (
            f"{secs}S" if secs else "")
        return f"{sign}PT{parts or '0S'}"
    return f"{whole / 3600:.2f}"


def format_date(value: date | datetime | None, style: str = "iso") -> str:
    if value is None:
        return ""
    if style == "epoch":
        if isinstance(value, datetime):
            instant = value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
        else:
            instant = datetime.combine(value, time.min, tzinfo=ZoneInfo("UTC"))
        return str(int(instant.timestamp() * 1000))
    if style == "normal":
        if isinstance(value, datetime):
            return value.strftime("%d %b %Y %H:%M")
        return value.strftime("%d %b %Y")
    return value.isoformat()


def human_duration(seconds: int | float | None) -> str:
    """`3h 20m`, the way the app shows durations; for PDF cells and email bodies."""
    if seconds is None:
        return ""
    whole = round(seconds)
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    hours, rest = divmod(whole, 3600)
    mins = rest // 60
    if hours and mins:
        return f"{sign}{hours}h {mins}m"
    if hours:
        return f"{sign}{hours}h"
    return f"{sign}{mins}m"
