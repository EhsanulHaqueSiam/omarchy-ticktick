"""Natural-language quick add: ``submit report tomorrow 5pm !high #work *daily``.

Design rule, applied everywhere below: **mangling a title is worse than missing a
date**. Every token must be a whitespace-delimited standalone word, must parse
unambiguously, and only the characters actually matched are removed — which is why
``call #1 supplier``, ``meeting at 5 people`` and ``review PR !important`` come out
untouched. Anything not recognised stays in the title verbatim.

`parse()` never raises and never reads the wall clock directly: the reference time
comes from the caller, or from `dates.now()` when omitted.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as _dt
import re
from typing import Callable, Iterator

from . import dates as _dates

# A token must start a whitespace-delimited word and end one (a little trailing
# punctuation is tolerated). This is what keeps "monday.com" and "5pmail" intact.
_PRE = r"(?<!\S)"
_POST = r"(?=[\s,;!?)]|$)"

_PRIORITIES = {
    "high": 5, "h": 5, "p1": 5,
    "medium": 3, "med": 3, "m": 3, "p2": 3,
    "low": 1, "l": 1, "p3": 1,
    "none": 0, "p0": 0,
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_FREQ = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}

_SIMPLE_REPEAT = {
    "daily": "RRULE:FREQ=DAILY;INTERVAL=1",
    "weekly": "RRULE:FREQ=WEEKLY;INTERVAL=1",
    "monthly": "RRULE:FREQ=MONTHLY;INTERVAL=1",
    "yearly": "RRULE:FREQ=YEARLY;INTERVAL=1",
    "weekdays": "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR",
}

# Longest alternative first so "monday" is never clipped to "mon".
def _alt(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_MONTH_ALT = _alt(_MONTHS)
_WEEKDAY_ALT = _alt(_WEEKDAYS)

# Some names double as ordinary English and would quietly eat words out of a title.
# They stay usable in the spellings that cannot be misread — "5 may" (a number in
# front), "next wed" (qualified) — and are dropped from the bare ones, where
# "may 5 people attend", "buy sun cream" and "i sat the exam" would lose a word.
_MONTH_FIRST_ALT = _alt(set(_MONTHS) - {"may", "march", "mar"})
_BARE_WEEKDAY_ALT = _alt(set(_WEEKDAYS) - {"sat", "sun", "wed"})


@dataclasses.dataclass
class Parsed:
    """Result of a quick-add parse. `all_day` is meaningful only when `due` is set."""

    title: str
    due: _dt.datetime | None = None
    all_day: bool = True
    priority: int | None = None  # 0/1/3/5, None = "not specified"
    project: str | None = None  # project NAME as typed; the CLI resolves it
    repeat: str | None = None  # RRULE string
    reminders: list[str] | None = None
    matched: list[str] = dataclasses.field(default_factory=list)


def parse(text: str, *, now: _dt.datetime | None = None) -> Parsed:
    """Pull due date, priority, project and repeat out of `text`; never raises."""
    if not isinstance(text, str) or not text.strip():
        return Parsed(title=text.strip() if isinstance(text, str) else "")

    ref = now or _dates.now()
    if ref.tzinfo is None:  # a naive clock would make `due` naive too
        ref = ref.astimezone()
    today = ref.date()

    out = Parsed(title="")
    spans: list[tuple[int, int]] = []

    for match in _candidates(_RE_PRIORITY, text, spans):
        out.priority = _PRIORITIES[match.group(1).lower()]
        spans.append(match.span())
        break

    for match in _candidates(_RE_PROJECT, text, spans):
        out.project = match.group(1) or match.group(2)
        spans.append(match.span())
        break

    for match in _candidates(_RE_REPEAT, text, spans):
        rule = _rrule(match)
        if rule is None:
            continue
        out.repeat = rule
        spans.append(match.span())
        break

    day: _dt.date | None = None
    clock: _dt.time | None = None
    for pattern, handler in _DATE_RULES:
        for match in _candidates(pattern, text, spans):
            found = handler(match, today)
            if found is None:  # e.g. "32/13" — leave it in the title
                continue
            day, clock = found
            spans.append(match.span())
            break
        if day is not None:
            break

    for match in _candidates(_RE_TIME, text, spans):
        explicit = _clock(match)
        if explicit is None:
            continue
        clock = explicit  # an explicit time beats the one "tonight"/"eod" implied
        spans.append(match.span())
        break

    if day is None and clock is not None:
        # Bare time: today if it is still ahead of us, otherwise tomorrow.
        candidate = _dt.datetime.combine(today, clock, tzinfo=ref.tzinfo)
        day = today if candidate > ref else today + _dt.timedelta(days=1)
    if day is not None:
        out.all_day = clock is None
        out.due = _dt.datetime.combine(
            day, clock if clock is not None else _dt.time(), tzinfo=ref.tzinfo
        )

    spans.sort()
    out.matched = [text[start:end] for start, end in spans]
    out.title = _strip(text, spans)
    return out


# --- token patterns ----------------------------------------------------------

_RE_PRIORITY = re.compile(_PRE + r"!(" + _alt(_PRIORITIES) + r")" + _POST, re.I)

# `#"two words"` or `#word`; the leading letter is what saves "call #1 supplier".
_RE_PROJECT = re.compile(
    _PRE + r'#(?:"([^"\n]{1,64})"|([^\W\d_][\w-]{0,63}))' + _POST
)

_RE_REPEAT = re.compile(
    _PRE + r"\*(?:(daily|weekly|monthly|yearly|weekdays)"
    r"|every\s+(\d{1,3})\s+(day|week|month|year)s?)" + _POST,
    re.I,
)

# `5pm`, `5:30 pm`, `@9am`, `17:30`, `noon`, `midnight` — optionally introduced by
# "at", which is consumed too so titles do not end on a dangling preposition.
_RE_TIME = re.compile(
    _PRE + r"(?:at\s+)?@?(?:"
    r"(?P<h12>\d{1,2})(?::(?P<m12>\d{2}))?\s*(?P<ap>[ap]m)"
    r"|(?P<h24>\d{1,2}):(?P<m24>\d{2})"
    r"|(?P<word>noon|midnight))" + _POST,
    re.I,
)


def _clock(match: re.Match[str]) -> _dt.time | None:
    """Time of day for a `_RE_TIME` match, or None when the numbers are nonsense."""
    word = match.group("word")
    if word:
        return _dt.time(12) if word.lower() == "noon" else _dt.time(0)
    if match.group("h12"):
        hour, minute = int(match.group("h12")), int(match.group("m12") or 0)
        if not (1 <= hour <= 12 and minute <= 59):
            return None
        hour %= 12
        if match.group("ap").lower() == "pm":
            hour += 12
        return _dt.time(hour, minute)
    hour, minute = int(match.group("h24")), int(match.group("m24"))
    if hour > 23 or minute > 59:
        return None
    return _dt.time(hour, minute)


def _rrule(match: re.Match[str]) -> str | None:
    """RRULE for a `*repeat` token, or None if the interval is degenerate."""
    word = (match.group(1) or "").lower()
    if word:
        return _SIMPLE_REPEAT[word]
    interval = int(match.group(2))
    if interval < 1:
        return None
    return f"RRULE:FREQ={_FREQ[match.group(3).lower()]};INTERVAL={interval}"


# --- date rules --------------------------------------------------------------

_Found = tuple[_dt.date, _dt.time | None]
_Handler = Callable[[re.Match[str], _dt.date], "_Found | None"]


def _mk(year: int, month: int, day: int) -> _dt.date | None:
    try:
        return _dt.date(year, month, day)
    except ValueError:  # 2026-13-40, 31 Feb, 29 Feb in a common year
        return None


def _roll(day: _dt.date | None, today: _dt.date) -> _dt.date | None:
    """A year-less date always means the next occurrence, never one in the past."""
    if day is None or day >= today:
        return day
    return _mk(day.year + 1, day.month, day.day)


def _add_months(day: _dt.date, count: int) -> _dt.date:
    index = day.month - 1 + count
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def _weekday(today: _dt.date, target: int, *, weeks: int = 0) -> _dt.date:
    """Next `target` strictly after today; `weeks=1` pushes it to the week after."""
    ahead = (target - today.weekday()) % 7 or 7
    return today + _dt.timedelta(days=ahead + 7 * weeks)


def _h_iso(m: re.Match[str], today: _dt.date) -> _Found | None:
    day = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (day, None) if day else None


def _h_slash_year(m: re.Match[str], today: _dt.date) -> _Found | None:
    year = int(m.group(3))
    day = _mk(year + 2000 if year < 100 else year, int(m.group(2)), int(m.group(1)))
    return (day, None) if day else None


def _h_slash(m: re.Match[str], today: _dt.date) -> _Found | None:
    day = _roll(_mk(today.year, int(m.group(2)), int(m.group(1))), today)
    return (day, None) if day else None


def _h_day_month(m: re.Match[str], today: _dt.date) -> _Found | None:
    month = _MONTHS[m.group(2).lower()]
    year = m.group(3)
    day = _mk(int(year), month, int(m.group(1))) if year else _roll(
        _mk(today.year, month, int(m.group(1))), today
    )
    return (day, None) if day else None


def _h_month_day(m: re.Match[str], today: _dt.date) -> _Found | None:
    month = _MONTHS[m.group(1).lower()]
    year = m.group(3)
    day = _mk(int(year), month, int(m.group(2))) if year else _roll(
        _mk(today.year, month, int(m.group(2))), today
    )
    return (day, None) if day else None


def _h_in(m: re.Match[str], today: _dt.date) -> _Found | None:
    count, unit = int(m.group(1)), m.group(2).lower().rstrip("s")
    if unit == "day":
        return today + _dt.timedelta(days=count), None
    if unit == "week":
        return today + _dt.timedelta(weeks=count), None
    return _add_months(today, count * (12 if unit == "year" else 1)), None


def _h_next_weekday(m: re.Match[str], today: _dt.date) -> _Found | None:
    return _weekday(today, _WEEKDAYS[m.group(1).lower()], weeks=1), None


def _h_next_unit(m: re.Match[str], today: _dt.date) -> _Found | None:
    unit = m.group(1).lower()
    if unit == "week":
        return today + _dt.timedelta(days=7), None
    return _add_months(today, 12 if unit == "year" else 1), None


def _h_bare_weekday(m: re.Match[str], today: _dt.date) -> _Found | None:
    return _weekday(today, _WEEKDAYS[m.group(1).lower()]), None


def _h_word(m: re.Match[str], today: _dt.date) -> _Found | None:
    word = m.group(1).lower()
    if word == "today":
        return today, None
    if word in ("tomorrow", "tmr"):
        return today + _dt.timedelta(days=1), None
    if word == "yesterday":
        return today - _dt.timedelta(days=1), None
    if word == "tonight":
        return today, _dt.time(20)
    if word == "eod":
        return today, _dt.time(23, 59)
    # eow: the coming Friday — "end of the week" is the end of the work week, and
    # it stays today when today already is Friday.
    return today + _dt.timedelta(days=(4 - today.weekday()) % 7), None


# Order is significance, not text position: the multi-word forms have to be tried
# before the single words they contain ("next monday" before "monday").
_DATE_RULES: list[tuple[re.Pattern[str], _Handler]] = [
    (re.compile(_PRE + r"(\d{4})-(\d{1,2})-(\d{1,2})" + _POST), _h_iso),
    (re.compile(_PRE + r"(\d{1,2})/(\d{1,2})/(\d{2,4})" + _POST), _h_slash_year),
    (re.compile(_PRE + r"(\d{1,2})/(\d{1,2})" + _POST), _h_slash),
    (re.compile(_PRE + r"(\d{1,2})\s+(" + _MONTH_ALT + r")\.?(?:\s+(\d{4}))?" + _POST, re.I), _h_day_month),
    (re.compile(_PRE + r"(" + _MONTH_FIRST_ALT + r")\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?" + _POST, re.I), _h_month_day),
    (re.compile(_PRE + r"in\s+(\d{1,3})\s+(days?|weeks?|months?|years?)" + _POST, re.I), _h_in),
    (re.compile(_PRE + r"next\s+(" + _WEEKDAY_ALT + r")" + _POST, re.I), _h_next_weekday),
    (re.compile(_PRE + r"next\s+(week|month|year)" + _POST, re.I), _h_next_unit),
    (re.compile(_PRE + r"(" + _BARE_WEEKDAY_ALT + r")" + _POST, re.I), _h_bare_weekday),
    (re.compile(_PRE + r"(today|tomorrow|tmr|tonight|yesterday|eod|eow)" + _POST, re.I), _h_word),
]


# --- text surgery ------------------------------------------------------------


def _candidates(
    pattern: re.Pattern[str], text: str, spans: list[tuple[int, int]]
) -> Iterator[re.Match[str]]:
    """Matches that do not collide with text an earlier token already claimed."""
    for match in pattern.finditer(text):
        start, end = match.span()
        if not any(start < e and s < end for s, e in spans):
            yield match


def _strip(text: str, spans: list[tuple[int, int]]) -> str:
    """Everything the tokens did not consume, with whitespace collapsed."""
    kept, cursor = [], 0
    for start, end in spans:
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return re.sub(r"\s+", " ", "".join(kept)).strip()
