"""The arithmetic of time tracking, free of the database so it is unit-tested on its own.

Everything here takes and returns **naive UTC** datetimes, the form every DateTime column in
this service is stored and read back in (models/issue.py). Aware values are converted at the
edge, once, by `to_naive_utc`.
"""
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

#: The longest single entry: one day. The target schema's CHECK (`minutes <= 24*60`,
#: docs/design/schema.sql) says the same thing. Anything longer is either a forgotten timer
#: or several days typed as one, and both are better split by a person than accepted.
MAX_ENTRY_SECONDS = 24 * 60 * 60

#: The largest estimate an issue can carry: 1,000 hours. A ticket estimated above that is
#: a project, not a ticket — and the bound keeps a mistyped "3600000h" out of every SUM.
MAX_ESTIMATE_SECONDS = 1_000 * 60 * 60

#: How far ahead of the server's clock a `started_at` may be before it is refused as "in the
#: future". Laptops drift; five minutes absorbs any clock that NTP has touched this year
#: without letting someone log tomorrow's meeting today.
CLOCK_SKEW = timedelta(minutes=5)

#: The furthest-ahead time zone in use, UTC+14 (Line Islands). A `work_date` later than
#: today in that zone is a date that has not started anywhere yet.
_LATEST_OFFSET = timedelta(hours=14)

#: The widest date range one report covers: a quarter. A timesheet is a week and a monthly
#: export is 31 days; the bound stops one request from summing a tenant's whole history.
MAX_REPORT_DAYS = 92


def to_naive_utc(value: datetime | None) -> datetime | None:
    """`value` as naive UTC. A naive input is taken to be UTC already.

    PyMySQL and SQLite both drop an offset silently, so an aware value must be converted
    before it reaches a column or it is stored off by its offset (verified 2026-10-01).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def utc_now() -> datetime:
    """Now, as naive UTC — comparable with anything read back from a DateTime column."""
    return datetime.now(UTC).replace(tzinfo=None)


def is_future_start(started_at: datetime, now: datetime) -> bool:
    """Whether a naive-UTC start is later than `now` by more than clock skew explains."""
    return started_at > now + CLOCK_SKEW


def is_future_work_date(work_date: date, now: datetime) -> bool:
    """Whether `work_date` has not begun in any time zone at the naive-UTC instant `now`."""
    return work_date > (now + _LATEST_OFFSET).date()


def elapsed_seconds(started_at: datetime, now: datetime) -> int:
    """Whole seconds from `started_at` to `now`, never negative (a skewed start is zero)."""
    return max(0, int((now - started_at).total_seconds()))


def timer_entry_seconds(elapsed: int) -> tuple[int, bool]:
    """The duration a stopped timer is saved with, and whether it had to be cut.

    At least one second, because an entry of nothing is not an entry; at most one day,
    because that is the entry ceiling — a timer left running over a weekend becomes one
    day for its owner to correct, rather than a refusal that loses the time altogether.
    """
    if elapsed > MAX_ENTRY_SECONDS:
        return MAX_ENTRY_SECONDS, True
    return max(1, elapsed), False


def remaining_seconds(estimate: int | None, spent: int) -> int | None:
    """Estimate minus time spent, floored at zero; None when there is no estimate.

    Derived every time, never stored — the decoupled stored figure is what makes Jira's
    "remaining" disagree with its own original estimate.
    """
    if estimate is None:
        return None
    return max(0, estimate - spent)


def sum_present(values: Iterable[int | None]) -> int | None:
    """The sum of the values that are not None, or None if every one of them is."""
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def report_range_problem(date_from: date, date_to: date) -> str | None:
    """Why `date_from`..`date_to` (inclusive) is not a range one report may cover, or None."""
    if date_to < date_from:
        return "The end date is before the start date."
    if (date_to - date_from).days + 1 > MAX_REPORT_DAYS:
        return f"A report covers at most {MAX_REPORT_DAYS} days. Narrow the range."
    return None
