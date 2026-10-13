"""The pure arithmetic of time tracking (`utils/time_tracking.py`).

Unit tests rather than integration ones on purpose: the coverage floor is measured on the
`-m unit` subset (pyproject.toml), so logic that only an endpoint test reached would lower
it, and these are the rules a wrong answer in would put into every timesheet.
"""
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from utils import time_tracking as tt

pytestmark = pytest.mark.unit

NOON = datetime(2026, 10, 1, 12, 0, 0)


class TestToNaiveUtc:
    def test_an_offset_value_is_converted_not_truncated(self):
        """Dropping the tzinfo without converting is the bug both drivers have built in."""
        ist = timezone(timedelta(hours=5, minutes=30))
        assert tt.to_naive_utc(datetime(2026, 10, 1, 10, 0, tzinfo=ist)) == datetime(
            2026, 10, 1, 4, 30)

    def test_naive_is_taken_as_utc_and_none_stays_none(self):
        assert tt.to_naive_utc(NOON) == NOON
        assert tt.to_naive_utc(None) is None

    def test_utc_now_is_naive(self):
        assert tt.utc_now().tzinfo is None
        assert abs(tt.utc_now() - datetime.now(UTC).replace(tzinfo=None)) < timedelta(seconds=5)


class TestFuture:
    def test_a_start_within_clock_skew_is_accepted(self):
        assert not tt.is_future_start(NOON + timedelta(minutes=4), NOON)
        assert tt.is_future_start(NOON + timedelta(minutes=6), NOON)

    def test_a_work_date_is_future_only_once_it_has_not_begun_anywhere(self):
        """At 12:00 UTC it is already the next day in UTC+14, so tomorrow is allowed — a
        person in Kiribati logging today's work must not be told it is in the future."""
        assert not tt.is_future_work_date(date(2026, 10, 2), NOON)
        assert tt.is_future_work_date(date(2026, 10, 3), NOON)
        assert not tt.is_future_work_date(date(2026, 10, 2), datetime(2026, 10, 1, 10, 0))
        assert tt.is_future_work_date(date(2026, 10, 2), datetime(2026, 10, 1, 9, 59))


class TestTimer:
    def test_elapsed_is_whole_seconds_and_never_negative(self):
        assert tt.elapsed_seconds(NOON, NOON + timedelta(seconds=90, milliseconds=900)) == 90
        assert tt.elapsed_seconds(NOON + timedelta(seconds=5), NOON) == 0

    @pytest.mark.parametrize("elapsed, expected", [
        (0, (1, False)),
        (59, (59, False)),
        (tt.MAX_ENTRY_SECONDS, (tt.MAX_ENTRY_SECONDS, False)),
        (tt.MAX_ENTRY_SECONDS + 1, (tt.MAX_ENTRY_SECONDS, True)),
    ])
    def test_a_stopped_timer_is_at_least_a_second_and_at_most_a_day(self, elapsed, expected):
        assert tt.timer_entry_seconds(elapsed) == expected


class TestEstimates:
    def test_remaining_is_derived_and_floored_at_zero(self):
        assert tt.remaining_seconds(None, 100) is None
        assert tt.remaining_seconds(3600, 600) == 3000
        assert tt.remaining_seconds(3600, 7200) == 0

    def test_a_sum_of_estimates_ignores_the_missing_and_is_none_if_all_are(self):
        assert tt.sum_present([None, None]) is None
        assert tt.sum_present([]) is None
        assert tt.sum_present([None, 60, 0]) == 60


class TestReportRange:
    def test_a_forwards_range_up_to_a_quarter_is_fine(self):
        start = date(2026, 1, 1)
        assert tt.report_range_problem(start, start) is None
        assert tt.report_range_problem(start, start + timedelta(days=tt.MAX_REPORT_DAYS - 1)) \
            is None

    def test_backwards_or_too_wide_is_explained(self):
        start = date(2026, 1, 1)
        assert "before" in tt.report_range_problem(start, start - timedelta(days=1))
        assert "at most" in tt.report_range_problem(
            start, start + timedelta(days=tt.MAX_REPORT_DAYS))
