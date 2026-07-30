"""Tests for ticktick.nlp.

Clock frozen at Thursday 2026-07-30 14:00 Europe/Berlin so weekday maths, the
"time already gone -> tomorrow" rule and the year rollover are stable forever.
"""

import pathlib
import sys
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ticktick import dates, nlp  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 30, 14, 0, tzinfo=BERLIN)  # Thursday

# Everything a user might plausibly type, one token of each class at most, used
# both as feature tests and as the idempotence corpus.
CORPUS = [
    "submit report tomorrow 5pm !high #work *daily",
    "call bob at 17:30",
    "pay rent 14/08",
    "standup next monday",
    "retro in 3 weeks",
    "wrap up eow",
    "dinner tonight",
    "gym @9am",
    "lunch noon",
    "water plants *every 3 days",
    'groceries #"weekend chores"',
    "ship jan 3 2027",
    "review deck 2026-08-14 !p2",
    "tmr",
    "book flights 14 aug 2027 midnight",
]


def parse(text):
    return nlp.parse(text, now=NOW)


class PriorityTest(unittest.TestCase):
    def test_every_alias(self):
        table = {
            "!high": 5, "!h": 5, "!p1": 5,
            "!medium": 3, "!med": 3, "!m": 3, "!p2": 3,
            "!low": 1, "!l": 1, "!p3": 1,
            "!none": 0, "!p0": 0,
        }
        for token, expected in table.items():
            with self.subTest(token=token):
                out = parse(f"do a thing {token}")
                self.assertEqual(out.priority, expected)
                self.assertEqual(out.title, "do a thing")
                self.assertEqual(out.matched, [token])

    def test_case_insensitive_and_position_free(self):
        self.assertEqual(parse("!HIGH ship it").priority, 5)
        self.assertEqual(parse("ship !Med it").title, "ship it")

    def test_only_the_first_priority_wins(self):
        out = parse("thing !high !low")
        self.assertEqual(out.priority, 5)


class ProjectTest(unittest.TestCase):
    def test_bare_word(self):
        out = parse("buy milk #groceries")
        self.assertEqual(out.project, "groceries")
        self.assertEqual(out.title, "buy milk")

    def test_quoted_two_words(self):
        out = parse('buy milk #"weekend chores" today')
        self.assertEqual(out.project, "weekend chores")
        self.assertEqual(out.title, "buy milk")

    def test_case_is_preserved_for_the_resolver(self):
        self.assertEqual(parse("x #Work-Stuff").project, "Work-Stuff")


class RepeatTest(unittest.TestCase):
    def test_simple_frequencies(self):
        table = {
            "*daily": "RRULE:FREQ=DAILY;INTERVAL=1",
            "*weekly": "RRULE:FREQ=WEEKLY;INTERVAL=1",
            "*monthly": "RRULE:FREQ=MONTHLY;INTERVAL=1",
            "*yearly": "RRULE:FREQ=YEARLY;INTERVAL=1",
            "*weekdays": "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR",
        }
        for token, expected in table.items():
            with self.subTest(token=token):
                out = parse(f"standup {token}")
                self.assertEqual(out.repeat, expected)
                self.assertEqual(out.title, "standup")

    def test_every_n_units(self):
        table = {
            "*every 3 days": "RRULE:FREQ=DAILY;INTERVAL=3",
            "*every 2 weeks": "RRULE:FREQ=WEEKLY;INTERVAL=2",
            "*every 6 months": "RRULE:FREQ=MONTHLY;INTERVAL=6",
            "*every 1 year": "RRULE:FREQ=YEARLY;INTERVAL=1",
        }
        for token, expected in table.items():
            with self.subTest(token=token):
                self.assertEqual(parse(f"x {token}").repeat, expected)


class TimeTest(unittest.TestCase):
    def assertDue(self, text, expected):
        out = parse(text)
        self.assertEqual(out.due, expected)
        self.assertFalse(out.all_day)

    def test_clock_forms(self):
        table = {
            "call 5pm": datetime(2026, 7, 30, 17, 0, tzinfo=BERLIN),
            "call 5:30pm": datetime(2026, 7, 30, 17, 30, tzinfo=BERLIN),
            "call 5 pm": datetime(2026, 7, 30, 17, 0, tzinfo=BERLIN),
            "call @14:30": datetime(2026, 7, 30, 14, 30, tzinfo=BERLIN),
            "call 17:30": datetime(2026, 7, 30, 17, 30, tzinfo=BERLIN),
            "call 12pm": datetime(2026, 7, 30, 12, 0, tzinfo=BERLIN) + timedelta(days=1),
            "call noon": datetime(2026, 7, 31, 12, 0, tzinfo=BERLIN),
        }
        for text, expected in table.items():
            with self.subTest(text=text):
                self.assertDue(text, expected)

    def test_a_time_already_gone_means_tomorrow(self):
        self.assertDue("gym @9am", datetime(2026, 7, 31, 9, 0, tzinfo=BERLIN))
        self.assertDue("sleep midnight", datetime(2026, 7, 31, 0, 0, tzinfo=BERLIN))
        self.assertDue("call 12am", datetime(2026, 7, 31, 0, 0, tzinfo=BERLIN))

    def test_leading_at_is_consumed_with_the_time(self):
        out = parse("call bob at 5pm")
        self.assertEqual(out.title, "call bob")
        self.assertEqual(out.matched, ["at 5pm"])

    def test_time_combines_with_a_date(self):
        out = parse("submit report tomorrow 5pm")
        self.assertEqual(out.due, datetime(2026, 7, 31, 17, 0, tzinfo=BERLIN))
        self.assertFalse(out.all_day)

    def test_explicit_time_overrides_the_one_tonight_implies(self):
        self.assertDue("drinks tonight 9pm", datetime(2026, 7, 30, 21, 0, tzinfo=BERLIN))


class DateTest(unittest.TestCase):
    def assertDay(self, text, expected, *, all_day=True):
        out = parse(text)
        self.assertIsNotNone(out.due, text)
        self.assertEqual(out.due.date(), expected)
        self.assertEqual(out.all_day, all_day)
        self.assertEqual(out.due.tzinfo, NOW.tzinfo)

    def test_words(self):
        self.assertDay("x today", date(2026, 7, 30))
        self.assertDay("x tomorrow", date(2026, 7, 31))
        self.assertDay("x tmr", date(2026, 7, 31))
        self.assertDay("x yesterday", date(2026, 7, 29))
        self.assertDay("x tonight", date(2026, 7, 30), all_day=False)
        self.assertDay("x eod", date(2026, 7, 30), all_day=False)
        self.assertDay("x eow", date(2026, 7, 31))  # the coming Friday

    def test_eod_is_the_last_minute_of_today(self):
        self.assertEqual(parse("x eod").due.time(), time(23, 59))

    def test_bare_weekday_is_the_next_one_strictly_after_today(self):
        self.assertDay("x friday", date(2026, 7, 31))
        self.assertDay("x fri", date(2026, 7, 31))
        self.assertDay("x monday", date(2026, 8, 3))
        self.assertDay("x sunday", date(2026, 8, 2))
        # today is Thursday: a bare "thursday" is a week out, never today
        self.assertDay("x thursday", date(2026, 8, 6))
        self.assertDay("x thu", date(2026, 8, 6))

    def test_weekday_abbreviations_that_are_english_words_need_a_qualifier(self):
        # bare "sat"/"sun"/"wed" would eat words out of ordinary titles
        for text in ("x sat", "x sun", "x wed"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text).due)
        self.assertDay("x next sat", date(2026, 8, 8))
        self.assertDay("x saturday", date(2026, 8, 1))

    def test_next_weekday_is_the_week_after_that(self):
        self.assertDay("x next monday", date(2026, 8, 10))
        self.assertDay("x next friday", date(2026, 8, 7))
        self.assertDay("x next thursday", date(2026, 8, 13))
        self.assertDay("x next wed", date(2026, 8, 12))

    def test_next_unit(self):
        self.assertDay("x next week", date(2026, 8, 6))
        self.assertDay("x next month", date(2026, 8, 30))
        self.assertDay("x next year", date(2027, 7, 30))

    def test_in_n_units(self):
        self.assertDay("x in 3 days", date(2026, 8, 2))
        self.assertDay("x in 1 day", date(2026, 7, 31))
        self.assertDay("x in 2 weeks", date(2026, 8, 13))
        self.assertDay("x in 7 months", date(2027, 2, 28))  # clamped, Feb has no 30th
        self.assertDay("x in 1 year", date(2027, 7, 30))

    def test_explicit_dates(self):
        self.assertDay("x 2026-08-14", date(2026, 8, 14))
        self.assertDay("x 2027-01-03", date(2027, 1, 3))
        self.assertDay("x 14/08", date(2026, 8, 14))
        self.assertDay("x 14/08/2027", date(2027, 8, 14))
        self.assertDay("x 14/08/27", date(2027, 8, 14))
        self.assertDay("x 14 aug", date(2026, 8, 14))
        self.assertDay("x 14 august", date(2026, 8, 14))
        self.assertDay("x aug 14", date(2026, 8, 14))
        self.assertDay("x jan 3 2027", date(2027, 1, 3))
        self.assertDay("x sept 1", date(2026, 9, 1))

    def test_month_names_that_are_english_words_only_parse_day_first(self):
        self.assertDay("x 5 may", date(2027, 5, 5))
        self.assertDay("x 3 march", date(2027, 3, 3))
        for text in ("may 5 people attend", "march 3 miles", "mar 4 candidates"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text).due)

    def test_slash_dates_are_day_first(self):
        # Documented ambiguity: 5/10 is 5 October, never 10 May.
        self.assertDay("x 5/10", date(2026, 10, 5))

    def test_year_less_dates_roll_forward(self):
        # 3 Jan is behind us in 2026, so it means 2027.
        self.assertDay("x jan 3", date(2027, 1, 3))
        self.assertDay("x 3/1", date(2027, 1, 3))
        self.assertDay("x today", date(2026, 7, 30))  # ... but today is not "next year"

    def test_impossible_dates_are_left_in_the_title(self):
        for text in ("x 2026-13-40", "x 32/13", "x 31 feb", "x feb 31"):
            with self.subTest(text=text):
                out = parse(text)
                self.assertIsNone(out.due)
                self.assertEqual(out.title, text)

    def test_all_day_dates_start_at_midnight(self):
        out = parse("x 2026-08-14")
        self.assertEqual(out.due.time(), time(0, 0))
        self.assertTrue(out.all_day)


class NegativeTest(unittest.TestCase):
    """Text that must survive completely untouched."""

    CASES = [
        "call #1 supplier",
        "meeting at 5 people",
        "review PR !important",
        "review PR !p9",
        "monday.com integration",
        "check tomorrows numbers",
        "ping @alice about the deploy",
        "read chapter 3",
        "buy 5 apples",
        "bump to v1.5pm",
        "run 100:30 split",
        "1:1 with bob",
        "sync at 25:99",
        "release *important fix",
        "fix #-tag parsing",
        "email todayish",
        "eodx",
        "in three days",
        "in 3 fortnights",
        "*every 0 days is nonsense",
        "quote 'tomorrow' as a string",
        "path /home/siam/14/08",
        "compare 5pm-6pm window",
        "grep -h flag",
        "issue #42 triage",
        "buy sun cream",
        "i sat the exam already",
        "wed with the team",
        "may 5 people attend",
        "march 3 miles",
        "may need to refactor auth",
    ]

    def test_nothing_is_consumed(self):
        for text in self.CASES:
            with self.subTest(text=text):
                out = parse(text)
                self.assertEqual(out.title, text)
                self.assertEqual(out.matched, [])
                self.assertIsNone(out.due)
                self.assertIsNone(out.priority)
                self.assertIsNone(out.project)
                self.assertIsNone(out.repeat)


class ParsedShapeTest(unittest.TestCase):
    def test_everything_at_once(self):
        out = parse("submit report tomorrow 5pm !high #work *daily")
        self.assertEqual(out.title, "submit report")
        self.assertEqual(out.due, datetime(2026, 7, 31, 17, 0, tzinfo=BERLIN))
        self.assertFalse(out.all_day)
        self.assertEqual(out.priority, 5)
        self.assertEqual(out.project, "work")
        self.assertEqual(out.repeat, "RRULE:FREQ=DAILY;INTERVAL=1")
        self.assertIsNone(out.reminders)

    def test_matched_is_in_text_order_and_verbatim(self):
        text = "x next friday 5:30pm !low #home *weekly"
        out = parse(text)
        self.assertEqual(
            out.matched, ["next friday", "5:30pm", "!low", "#home", "*weekly"]
        )
        for token in out.matched:
            self.assertIn(token, text)

    def test_no_tokens_leaves_everything_default(self):
        out = parse("just a plain task")
        self.assertEqual(out.title, "just a plain task")
        self.assertIsNone(out.due)
        self.assertTrue(out.all_day)  # meaningless without a due date
        self.assertEqual(out.matched, [])

    def test_degenerate_input_never_raises(self):
        for text in (None, "", "   ", 42, [], "#", "!", "*", "@", "!!!", "####"):
            with self.subTest(text=text):
                out = nlp.parse(text, now=NOW)
                self.assertIsInstance(out, nlp.Parsed)
                self.assertIsNone(out.due)

    def test_whitespace_is_collapsed_not_left_ragged(self):
        self.assertEqual(parse("write   the    docs tomorrow").title, "write the docs")
        self.assertEqual(parse("tomorrow ship it").title, "ship it")
        self.assertEqual(parse("ship it tomorrow").title, "ship it")

    def test_title_can_end_up_empty(self):
        out = parse("tomorrow")
        self.assertEqual(out.title, "")
        self.assertEqual(out.due.date(), date(2026, 7, 31))


class ClockInjectionTest(unittest.TestCase):
    def test_default_now_goes_through_dates_now(self):
        with mock.patch.object(dates, "now", return_value=NOW) as clock:
            out = nlp.parse("x tomorrow")
        clock.assert_called_once_with()
        self.assertEqual(out.due.date(), date(2026, 7, 31))

    def test_naive_now_still_yields_an_aware_due(self):
        out = nlp.parse("x tomorrow", now=datetime(2026, 7, 30, 14, 0))
        self.assertIsNotNone(out.due.tzinfo)
        self.assertIsNotNone(out.due.utcoffset())

    def test_relative_dates_follow_the_injected_clock(self):
        out = nlp.parse("x tomorrow", now=datetime(2030, 2, 28, 8, 0, tzinfo=BERLIN))
        self.assertEqual(out.due.date(), date(2030, 3, 1))


class IdempotenceTest(unittest.TestCase):
    def test_reparsing_a_title_finds_nothing(self):
        for text in CORPUS + NegativeTest.CASES:
            with self.subTest(text=text):
                first = parse(text)
                second = parse(first.title)
                self.assertEqual(second.title, first.title)
                self.assertEqual(second.matched, [])
                self.assertIsNone(second.due)
                self.assertIsNone(second.priority)
                self.assertIsNone(second.project)
                self.assertIsNone(second.repeat)

    def test_second_date_token_is_deliberately_left_alone(self):
        # Only one date is consumed; the leftover stays visible in the title rather
        # than being silently eaten.
        out = parse("x tomorrow monday")
        self.assertEqual(out.due.date(), date(2026, 8, 3))  # rule order, not text order
        self.assertEqual(out.title, "x tomorrow")


if __name__ == "__main__":
    unittest.main()
