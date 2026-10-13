"""The pure parts of reports: date maths, the burndown guideline, percentiles, rounding,
formatting, the schedule clock, signed download links and the exporters' cell guard.

Each expected value is worked by hand in the comment beside it, so a failure says which
side is wrong.
"""
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest

from schemas.reports import ReportParams, ReportsSettings
from services.reports import jobs, scheduler
from services.reports.exporters import export
from services.reports.exporters.formatting import cell_text, guard
from utils import report_math as rm

MONDAY = date(2026, 10, 5)


class TestDates:
    def test_week_start_follows_the_setting(self):
        wednesday = date(2026, 10, 7)
        assert rm.week_start_of(wednesday, "monday") == date(2026, 10, 5)
        assert rm.week_start_of(wednesday, "sunday") == date(2026, 10, 4)

    @pytest.mark.parametrize("preset, expected", [
        ("today", (MONDAY, MONDAY)),
        ("yesterday", (date(2026, 10, 4), date(2026, 10, 4))),
        ("this_week", (MONDAY, date(2026, 10, 11))),
        ("last_week", (date(2026, 9, 28), date(2026, 10, 4))),
        ("this_month", (date(2026, 10, 1), date(2026, 10, 31))),
        ("last_month", (date(2026, 9, 1), date(2026, 9, 30))),
        ("last_30_days", (date(2026, 9, 6), MONDAY)),
        ("this_quarter", (date(2026, 10, 1), date(2026, 12, 31))),
        ("last_quarter", (date(2026, 7, 1), date(2026, 9, 30))),
        ("this_year", (date(2026, 1, 1), date(2026, 12, 31))),
    ])
    def test_presets(self, preset, expected):
        assert rm.preset_range(preset, MONDAY, "monday") == expected

    def test_a_day_ends_at_local_midnight(self):
        """Asia/Kolkata is UTC+5:30: 5 Oct ends at 18:30 UTC on 5 Oct."""
        tz = rm.zone("Asia/Kolkata")
        assert rm.end_of_day_utc(MONDAY, tz) == datetime(2026, 10, 5, 18, 30)
        assert rm.start_of_day_utc(MONDAY, tz) == datetime(2026, 10, 4, 18, 30)
        assert rm.local_date(datetime(2026, 10, 5, 19, 0), tz) == date(2026, 10, 6)

    def test_unknown_zones_fall_back_to_utc_and_are_refused_by_settings(self):
        assert str(rm.zone("Not/AZone").key) == "UTC"
        assert not rm.is_known_zone("Not/AZone")
        with pytest.raises(ValueError):
            ReportsSettings(timezone="Not/AZone")

    def test_buckets(self):
        assert rm.bucket_key(date(2026, 10, 7), "week") == MONDAY
        assert rm.bucket_key(date(2026, 10, 7), "month") == date(2026, 10, 1)
        assert rm.bucket_keys(date(2026, 9, 30), date(2026, 10, 6), "week") == [
            date(2026, 9, 28), MONDAY]


class TestBurndownGuideline:
    def test_falls_on_working_days_and_is_flat_at_weekends(self):
        """Mon 5 .. Sun 11 Oct, 10 points: 5 working days, 2 points each."""
        days = rm.days_between(MONDAY, date(2026, 10, 11))
        assert rm.guideline(10, days) == [8.0, 6.0, 4.0, 2.0, 0.0, 0.0, 0.0]

    def test_a_weekend_only_range_still_reaches_zero(self):
        days = [date(2026, 10, 10), date(2026, 10, 11)]
        assert rm.guideline(6, days) == [6.0, 0.0]


class TestStatistics:
    def test_percentile_interpolates_like_a_spreadsheet(self):
        """[1,2,3,4]: position (4-1)*0.85 = 2.55 -> 3 + 0.55*(4-3) = 3.55."""
        assert rm.percentile([4, 1, 3, 2], 0.85) == pytest.approx(3.55)
        assert rm.percentile([1, 2, 3], 0.5) == 2
        assert rm.percentile([], 0.5) is None

    def test_rolling_average(self):
        assert rm.rolling_average([2, 4, 6], window=2) == [2, 3, 5]


class TestRounding:
    @pytest.mark.parametrize("seconds, minutes, mode, expected", [
        (7 * 60, 15, "up", 15 * 60),
        (7 * 60, 15, "down", 0),
        (7 * 60, 15, "nearest", 0),
        (8 * 60, 15, "nearest", 15 * 60),
        (7 * 60, 0, "up", 7 * 60),
    ])
    def test_round_seconds(self, seconds, minutes, mode, expected):
        assert rm.round_seconds(seconds, minutes, mode) == expected


class TestFormatting:
    def test_durations(self):
        assert rm.format_duration(5400) == "1.50"
        assert rm.format_duration(5400, "hhmm") == "1:30"
        assert rm.format_duration(5405, "iso") == "PT1H30M5S"
        assert rm.format_duration(0, "iso") == "PT0S"
        assert rm.format_duration(42, "seconds") == "42"
        assert rm.human_duration(5400) == "1h 30m"

    def test_dates(self):
        assert rm.format_date(MONDAY) == "2026-10-05"
        assert rm.format_date(MONDAY, "normal") == "05 Oct 2026"
        assert rm.format_date(date(1970, 1, 2), "epoch") == str(86_400_000)

    def test_text_that_a_spreadsheet_would_run_is_neutralised(self):
        """OWASP CSV injection: =, +, -, @ at the start of user text."""
        assert guard("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
        assert guard("@SUM(A1)") == "'@SUM(A1)"
        assert guard("Fix refunds") == "Fix refunds"
        assert cell_text("-1+1", "text", {}) == "'-1+1"
        # Numbers are not text: a negative variance stays a number.
        assert cell_text(-5, "number", {}) == "-5"
        assert cell_text(8.0, "number", {}) == "8"


class TestParams:
    def test_normalised_params_ignore_order_and_defaults(self):
        one = ReportParams(user_ids=["b", "a"], preset="last_week")
        two = ReportParams(preset="last_week", user_ids=["a", "b"], unit="points")
        assert one.normalized() == two.normalized() == {
            "preset": "last_week", "user_ids": ["a", "b"]}

    def test_group_levels_must_differ(self):
        with pytest.raises(ValueError):
            ReportParams(group_by=["user", "user"])

    def test_scope_must_be_a_known_kind(self):
        with pytest.raises(ValueError):
            ReportParams(scope="issue:123")


class TestScheduleClock:
    def test_weekly_at_nine_london_moves_with_daylight_saving(self):
        """London is UTC+1 until 25 Oct 2026, UTC+0 after: 09:00 local is 08:00 UTC on
        Mon 19 Oct and 09:00 UTC on Mon 26 Oct."""
        first = scheduler.next_run("weekly", 0, None, 9, "Europe/London",
                                   datetime(2026, 10, 14, 12, 0))
        assert first == datetime(2026, 10, 19, 8, 0)
        second = scheduler.next_run("weekly", 0, None, 9, "Europe/London", first)
        assert second == datetime(2026, 10, 26, 9, 0)

    def test_daily_is_strictly_after(self):
        at = datetime(2026, 10, 5, 9, 0)
        assert scheduler.next_run("daily", None, None, 9, "UTC", at) == datetime(2026, 10, 6, 9)
        assert scheduler.next_run("daily", None, None, 9, "UTC",
                                  at - timedelta(seconds=1)) == at

    def test_monthly_on_a_day(self):
        assert scheduler.next_run("monthly", None, 28, 6, "Asia/Kolkata",
                                  datetime(2026, 10, 28, 1, 0)) == datetime(2026, 11, 28, 0, 30)

    def test_the_spring_forward_gap_still_runs_once_that_day(self):
        """02:30 does not exist in New York on 8 Mar 2026; zoneinfo reads it as 03:30 EDT,
        07:30 UTC. Without the gap it would be 07:30 UTC (EDT) the day after, 07:30 UTC."""
        run = scheduler.next_run("daily", None, None, 2, "America/New_York",
                                 datetime(2026, 3, 8, 0, 0))
        assert run.date() == date(2026, 3, 8)


class TestDownloadTokens:
    def test_valid_for_the_job_and_person_it_was_made_for(self):
        token = jobs.download_token("job-1", "user-1")
        assert jobs.token_valid(token, "job-1", "user-1")
        assert not jobs.token_valid(token, "job-1", "user-2")
        assert not jobs.token_valid(token, "job-2", "user-1")
        assert not jobs.token_valid(None, "job-1", "user-1")
        assert not jobs.token_valid("garbage", "job-1", "user-1")
        assert not jobs.token_valid("x.y", "job-1", "user-1")

    def test_expires_after_a_day(self):
        issued = datetime(2026, 10, 5, 12, 0)
        token = jobs.download_token("job-1", "user-1", issued)
        assert jobs.token_valid(token, "job-1", "user-1", issued + timedelta(hours=23))
        assert not jobs.token_valid(token, "job-1", "user-1", issued + timedelta(hours=25))


def _result():
    return {
        "meta": {"type": "timesheet", "title": "Timesheet", "scope": "workspace",
                 "scope_name": "Acme", "date_from": "2026-10-05", "date_to": "2026-10-11",
                 "timezone": "UTC", "generated_at": datetime(2026, 10, 5, 12, 0),
                 "rounding_minutes": 0, "rounding_mode": "nearest", "notes": [],
                 "restricted_to_own": False},
        "columns": [{"key": "user", "label": "Person", "type": "user"},
                    {"key": "total", "label": "Total", "type": "duration"}],
        "rows": [{"user": "=cmd()", "total": 5400}, {"user": "Ann", "total": 1800}],
        "totals": {"user": "Total", "total": 7200},
        "chart": {"kind": "bar", "unit": "duration", "x": ["=cmd()", "Ann"],
                  "series": [{"key": "total", "label": "Total", "values": [5400, 1800]}]},
        "kpis": [{"key": "total", "label": "Total", "value": 7200, "type": "duration"}],
        "sections": [],
    }


class TestExporters:
    def test_csv(self):
        content, content_type, name = export(_result(), "csv", {})
        text = content.decode("utf-8-sig")
        assert content.startswith(b"\xef\xbb\xbf")
        assert content_type.startswith("text/csv")
        assert name.endswith(".csv") and "timesheet" in name
        assert text.splitlines()[:4] == ["Person,Total", "'=cmd(),1.50", "Ann,0.50",
                                         "Total,2.00"]
        assert "Time zone,UTC" in text

    def test_csv_hhmm(self):
        text = export(_result(), "csv", {"duration_format": "hhmm"})[0].decode("utf-8-sig")
        assert "Ann,0:30" in text

    def test_xlsx_keeps_durations_as_numbers(self):
        import openpyxl

        content, content_type, name = export(_result(), "xlsx", {})
        assert name.endswith(".xlsx") and "spreadsheetml" in content_type
        book = openpyxl.load_workbook(BytesIO(content))
        assert book.sheetnames[0] == "Summary"
        sheet = book[book.sheetnames[1]]
        rows = list(sheet.iter_rows(values_only=True))
        assert rows[0] == ("Person", "Total")
        assert rows[1][0] == "'=cmd()"
        assert rows[1][1] == pytest.approx(1.5)

    def test_pdf_is_a_well_formed_document(self):
        content, content_type, name = export(_result(), "pdf", {})
        assert content_type == "application/pdf" and name.endswith(".pdf")
        assert content.startswith(b"%PDF-1.4")
        assert content.rstrip().endswith(b"%%EOF")
        assert b"/Type /Catalog" in content and b"startxref" in content
