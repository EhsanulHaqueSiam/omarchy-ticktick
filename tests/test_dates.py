"""Tests for ticktick.dates.

The clock is frozen at Thursday 2026-07-30 14:00 Europe/Berlin (CEST, +02:00) and
TZ is pinned to Europe/Berlin so the "convert to local" paths are deterministic on
any machine.
"""

import locale
import os
import pathlib
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ticktick import dates  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 30, 14, 0, tzinfo=BERLIN)  # a Thursday
UTC = timezone.utc


def setUpModule() -> None:
    os.environ["TZ"] = "Europe/Berlin"
    if hasattr(time, "tzset"):
        time.tzset()


def berlin(*args: int) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


class ParseTest(unittest.TestCase):
    def test_every_offset_spelling_is_the_same_instant(self):
        expected = datetime(2019, 11, 13, 3, 0, tzinfo=UTC)
        for raw in (
            "2019-11-13T03:00:00+0000",
            "2019-11-13T03:00:00+00:00",
            "2019-11-13T03:00:00Z",
            "2019-11-13T03:00:00z",
            "2019-11-13T03:00:00.000+0000",
            "  2019-11-13T03:00:00+0000  ",
            "2019-11-13T04:00:00+0100",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(dates.parse(raw), expected)

    def test_timed_values_land_in_local_time(self):
        dt = dates.parse("2019-11-13T03:00:00+0000")
        self.assertEqual(dt.utcoffset(), timedelta(hours=1))  # Berlin is on CET in November
        self.assertEqual((dt.hour, dt.minute), (4, 0))

    def test_naive_input_is_read_as_local_wall_time(self):
        self.assertEqual(dates.parse("2026-08-14T09:00:00"), berlin(2026, 8, 14, 9, 0))

    def test_malformed_input_returns_none(self):
        for raw in (
            None,
            "",
            "   ",
            "garbage",
            "2026-13-01T00:00:00+0000",  # month 13
            "2026-08-14T25:00:00+0000",  # hour 25
            "2026-02-30T00:00:00+0000",  # no such day
            "14/08/2026",
            "T",
            0,
            12345,
            [],
            {"dueDate": "2026-08-14"},
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(dates.parse(raw))

    def test_all_day_midnight_expressed_in_utc_keeps_its_local_date(self):
        # 2026-08-14 00:00 Asia/Dhaka, serialised as UTC.
        dt = dates.parse("2026-08-13T18:00:00+0000", all_day=True, tz_name="Asia/Dhaka")
        self.assertEqual(dt.date(), date(2026, 8, 14))
        self.assertEqual((dt.hour, dt.minute), (0, 0))
        self.assertEqual(dt.utcoffset(), timedelta(hours=6))

    def test_all_day_docs_form_does_not_slide_a_day_back(self):
        # Verbatim from the TickTick API docs: 03:00 UTC with timeZone LA, isAllDay.
        # Naive conversion would call this the 12th.
        dt = dates.parse(
            "2019-11-13T03:00:00+0000", all_day=True, tz_name="America/Los_Angeles"
        )
        self.assertEqual(dt.date(), date(2019, 11, 13))
        self.assertEqual((dt.hour, dt.minute), (0, 0))

    def test_all_day_without_zone_uses_local(self):
        dt = dates.parse("2026-08-14T00:00:00+0000", all_day=True)
        self.assertEqual(dt.date(), date(2026, 8, 14))
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))

    def test_unknown_timezone_falls_back_instead_of_raising(self):
        dt = dates.parse("2026-08-14T03:00:00+0000", all_day=True, tz_name="Mars/Phobos")
        self.assertEqual(dt.date(), date(2026, 8, 14))
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))


class DstTest(unittest.TestCase):
    def test_ambiguous_local_hour_keeps_two_distinct_instants(self):
        # 2026-10-25 02:30 happens twice in Berlin; both spellings must survive.
        first = dates.parse("2026-10-25T00:30:00+0000")
        second = dates.parse("2026-10-25T01:30:00+0000")
        self.assertNotEqual(first, second)
        self.assertEqual(first.utcoffset(), timedelta(hours=2))
        self.assertEqual(second.utcoffset(), timedelta(hours=1))
        self.assertEqual(
            dates.label(first, all_day=False, now=berlin(2026, 10, 25, 1, 0)),
            dates.label(second, all_day=False, now=berlin(2026, 10, 25, 1, 0)),
        )

    def test_all_day_on_a_dst_boundary_stays_on_its_date(self):
        dt = dates.parse(
            "2026-10-24T22:00:00+0000", all_day=True, tz_name="Europe/Berlin"
        )
        self.assertEqual(dt.date(), date(2026, 10, 25))
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))

    def test_midnight_that_does_not_exist_still_yields_that_date(self):
        # Chile jumps 2026-09-05 24:00 -> 2026-09-06 01:00, so 00:00 is missing.
        dt = dates.parse(
            "2026-09-06T12:00:00+0000", all_day=True, tz_name="America/Santiago"
        )
        self.assertEqual(dt.date(), date(2026, 9, 6))
        self.assertEqual(
            dates.bucket(dt, all_day=True, now=berlin(2026, 9, 6, 10, 0), upcoming_days=7),
            "today",
        )


class FormatTest(unittest.TestCase):
    def test_offset_has_no_colon(self):
        dt = datetime(2026, 8, 14, 9, 0, tzinfo=ZoneInfo("Asia/Dhaka"))
        self.assertEqual(dates.format(dt), "2026-08-14T09:00:00+0600")

    def test_all_day_pins_midnight_in_the_datetimes_own_zone(self):
        dt = datetime(2026, 8, 14, 23, 45, 12, tzinfo=ZoneInfo("Asia/Dhaka"))
        self.assertEqual(dates.format(dt, all_day=True), "2026-08-14T00:00:00+0600")

    def test_naive_input_gets_the_local_offset(self):
        self.assertEqual(
            dates.format(datetime(2026, 8, 14, 9, 0)), "2026-08-14T09:00:00+0200"
        )

    def test_round_trip(self):
        dt = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
        self.assertEqual(dates.parse(dates.format(dt)), dt)


class BucketTest(unittest.TestCase):
    def bucket(self, due, *, all_day=False, now=NOW, days=7):
        return dates.bucket(due, all_day=all_day, now=now, upcoming_days=days)

    def test_undated(self):
        self.assertEqual(self.bucket(None), "undated")
        self.assertEqual(self.bucket(None, all_day=True), "undated")

    def test_timed_boundaries(self):
        table = [
            (berlin(2026, 7, 30, 0, 0), "overdue"),  # earlier today
            (berlin(2026, 7, 30, 13, 59), "overdue"),
            (berlin(2026, 7, 30, 14, 0), "today"),  # exactly now
            (berlin(2026, 7, 30, 23, 59), "today"),
            (berlin(2026, 7, 31, 0, 0), "tomorrow"),  # one minute later
            (berlin(2026, 7, 31, 23, 59), "tomorrow"),
            (berlin(2026, 8, 1, 0, 0), "upcoming"),
            (berlin(2026, 8, 6, 23, 59), "upcoming"),  # day 7
            (berlin(2026, 8, 7, 0, 0), "later"),  # day 8
            (berlin(2020, 1, 1, 0, 0), "overdue"),
        ]
        for due, expected in table:
            with self.subTest(due=due):
                self.assertEqual(self.bucket(due), expected)

    def test_all_day_is_never_overdue_on_its_own_date(self):
        due = berlin(2026, 7, 30, 0, 0)  # as an instant this is 14 hours in the past
        self.assertEqual(self.bucket(due, all_day=True), "today")
        self.assertEqual(
            self.bucket(due, all_day=True, now=berlin(2026, 7, 30, 23, 59)), "today"
        )
        self.assertEqual(
            self.bucket(due, all_day=True, now=berlin(2026, 7, 31, 0, 0)), "overdue"
        )

    def test_all_day_boundaries(self):
        table = [
            (date(2026, 7, 29), "overdue"),
            (date(2026, 7, 30), "today"),
            (date(2026, 7, 31), "tomorrow"),
            (date(2026, 8, 1), "upcoming"),
            (date(2026, 8, 6), "upcoming"),
            (date(2026, 8, 7), "later"),
        ]
        for day, expected in table:
            with self.subTest(day=day):
                due = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
                self.assertEqual(self.bucket(due, all_day=True), expected)

    def test_all_day_ignores_the_tasks_offset(self):
        # Midnight on 2026-07-30 is a wildly different instant in +14 and -11, but
        # both are "today" because only the calendar date counts.
        for zone in ("Pacific/Kiritimati", "Pacific/Niue", "UTC"):
            with self.subTest(zone=zone):
                due = datetime(2026, 7, 30, tzinfo=ZoneInfo(zone))
                self.assertEqual(self.bucket(due, all_day=True), "today")

    def test_upcoming_window_shrinks(self):
        due = berlin(2026, 8, 1, 9, 0)
        self.assertEqual(self.bucket(due, days=7), "upcoming")
        self.assertEqual(self.bucket(due, days=1), "later")
        # tomorrow keeps its own bucket even with a zero-day window
        self.assertEqual(self.bucket(berlin(2026, 7, 31, 9, 0), days=0), "tomorrow")

    def test_timed_due_is_bucketed_in_the_callers_zone(self):
        # 23:30 UTC on the 30th is already 01:30 on the 31st in Berlin.
        due = datetime(2026, 7, 30, 23, 30, tzinfo=UTC)
        self.assertEqual(self.bucket(due), "tomorrow")


class LabelTest(unittest.TestCase):
    def label(self, due, *, all_day=False, now=NOW):
        return dates.label(due, all_day=all_day, now=now)

    def test_undated(self):
        self.assertEqual(self.label(None), "")
        self.assertEqual(self.label(None, all_day=True), "")

    def test_all_day_branches(self):
        table = [
            (date(2026, 7, 30), "today"),
            (date(2026, 7, 31), "tomorrow"),
            (date(2026, 8, 1), "Sat"),
            (date(2026, 8, 5), "Wed"),  # +6, last weekday day
            (date(2026, 8, 6), "6 Aug"),  # +7, weekday would be ambiguous
            (date(2026, 7, 29), "29 Jul"),  # the past never shows a weekday
            (date(2026, 12, 31), "31 Dec"),
            (date(2027, 1, 1), "1 Jan 2027"),  # year rollover
            (date(2025, 12, 31), "31 Dec 2025"),
        ]
        for day, expected in table:
            with self.subTest(day=day):
                due = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
                self.assertEqual(self.label(due, all_day=True), expected)

    def test_timed_branches(self):
        table = [
            (berlin(2026, 7, 30, 9, 5), "09:05"),
            (berlin(2026, 7, 30, 14, 0), "14:00"),
            (berlin(2026, 7, 30, 23, 59), "23:59"),
            (berlin(2026, 7, 31, 14, 30), "Fri 14:30"),
            (berlin(2026, 8, 5, 9, 0), "Wed 09:00"),
            (berlin(2026, 8, 6, 9, 0), "6 Aug 09:00"),
            (berlin(2027, 1, 1, 9, 0), "1 Jan 2027 09:00"),
            (berlin(2026, 7, 29, 8, 0), "29 Jul 08:00"),
        ]
        for due, expected in table:
            with self.subTest(due=due):
                self.assertEqual(self.label(due), expected)

    def test_timed_label_uses_the_callers_zone(self):
        due = datetime(2026, 7, 30, 23, 30, tzinfo=UTC)  # 01:30 on the 31st in Berlin
        self.assertEqual(self.label(due), "Fri 01:30")

    def test_no_zero_padded_day_and_no_locale_leak(self):
        due = berlin(2027, 1, 1, 0, 0)
        self.assertEqual(self.label(due, all_day=True), "1 Jan 2027")
        try:
            locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
        except locale.Error:
            self.skipTest("de_DE.UTF-8 not installed")
        try:
            self.assertEqual(
                self.label(berlin(2026, 12, 31, 0, 0), all_day=True), "31 Dec"
            )
            self.assertEqual(self.label(berlin(2026, 8, 1, 0, 0), all_day=True), "Sat")
        finally:
            locale.setlocale(locale.LC_TIME, "C")


class MiscTest(unittest.TestCase):
    def test_bucket_order_is_the_render_order(self):
        self.assertEqual(
            dates.BUCKET_ORDER,
            {"overdue": 0, "today": 1, "tomorrow": 2, "upcoming": 3, "later": 4, "undated": 5},
        )
        self.assertEqual(
            sorted(dates.BUCKET_ORDER, key=dates.BUCKET_ORDER.__getitem__),
            ["overdue", "today", "tomorrow", "upcoming", "later", "undated"],
        )

    def test_now_is_aware_and_current(self):
        stamp = dates.now()
        self.assertIsNotNone(stamp.tzinfo)
        self.assertIsNotNone(stamp.utcoffset())
        self.assertLess(abs((stamp - datetime.now(UTC)).total_seconds()), 5)

    def test_tt_format_matches_the_api_contract(self):
        self.assertEqual(dates.TT_FORMAT, "%Y-%m-%dT%H:%M:%S%z")


if __name__ == "__main__":
    unittest.main()
