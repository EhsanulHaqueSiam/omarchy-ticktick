"""TickTick timestamp handling: parse, format, bucket, label.

TickTick speaks ``"yyyy-MM-dd'T'HH:mm:ssZ"`` — e.g. ``2019-11-13T03:00:00+0000``,
with no colon in the UTC offset. We emit exactly that and accept the friendlier
ISO-8601 spellings (``+00:00``, trailing ``Z``, no offset at all) on the way in.

Everything that needs the current time takes it as a keyword argument; `now()` is
the single indirection so tests can pin the clock.
"""

from __future__ import annotations

from datetime import date as _date, datetime, time as _time, tzinfo as _tzinfo
from zoneinfo import ZoneInfo

TT_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

BUCKET_ORDER: dict[str, int] = {
    "overdue": 0,
    "today": 1,
    "tomorrow": 2,
    "upcoming": 3,
    "later": 4,
    "undated": 5,
}

# Locale-independent by construction: %-d / %b depend on the C library's locale
# and %-d does not exist everywhere, so day and month names are built by hand.
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def now() -> datetime:
    """Local, timezone-aware wall-clock now."""
    return datetime.now().astimezone()


def parse(
    raw: str | None,
    *,
    all_day: bool = False,
    tz_name: str | None = None,
) -> datetime | None:
    """Parse a TickTick timestamp into an aware datetime, or None if unusable.

    Timed values are converted to the machine's local zone. All-day values carry no
    meaningful instant, so they resolve to midnight of their calendar date in
    `tz_name` (the task's own zone), falling back to local when it is missing or
    unknown. Never raises.
    """
    dt = _instant(raw)
    if dt is None:
        return None
    if not all_day:
        return dt.astimezone()
    tz = _zone(tz_name)
    return _midnight(_all_day_date(dt, tz), tz)


def format(dt: datetime, *, all_day: bool = False) -> str:  # noqa: A001 - frozen name
    """Render a datetime the way the API wants it: ``2026-08-14T09:00:00+0000``.

    all_day pins the clock to 00:00:00 in the datetime's own zone rather than
    converting, because for an all-day task only the calendar date survives.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    if all_day:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.strftime(TT_FORMAT)


def bucket(
    due: datetime | None,
    *,
    all_day: bool,
    now: datetime,
    upcoming_days: int,
) -> str:
    """Classify a due date into the group the widget renders it under.

    Returns 'overdue' | 'today' | 'tomorrow' | 'upcoming' | 'later' | 'undated'.
    An all-day task is compared by calendar date only, so it is never 'overdue'
    on its own due date — only from the following day onward.
    """
    if due is None:
        return "undated"
    if all_day:
        delta = (due.date() - now.date()).days
    else:
        if due < now:
            return "overdue"
        delta = (due.astimezone(now.tzinfo).date() - now.date()).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return "upcoming" if delta <= upcoming_days else "later"


def label(due: datetime | None, *, all_day: bool, now: datetime) -> str:
    """Short human label for a due date.

    '' | 'today' | 'tomorrow' | '14:30' | 'Mon' | 'Mon 14:30' | '14 Aug' |
    '14 Aug 2027'. Same-day timed values collapse to the clock; anything within the
    next six days shows its weekday; the year appears only when it is not this one.
    """
    if due is None:
        return ""
    local = due if all_day else due.astimezone(now.tzinfo)
    day = local.date()
    delta = (day - now.date()).days
    if all_day:
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if 2 <= delta <= 6:
            return _WEEKDAY_NAMES[day.weekday()]
        return _day_month(day, now)
    clock = f"{local.hour:02d}:{local.minute:02d}"
    if delta == 0:
        return clock
    if 1 <= delta <= 6:
        return f"{_WEEKDAY_NAMES[day.weekday()]} {clock}"
    return f"{_day_month(day, now)} {clock}"


# --- internals ---------------------------------------------------------------


def _instant(raw: str | None) -> datetime | None:
    """Aware datetime for any spelling TickTick (or a human) might hand us."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, TT_FORMAT)
        except ValueError:
            return None
    # A naive stamp is read as local wall time; astimezone() attaches the offset
    # that was actually in force on that date, so DST is not smeared.
    return dt if dt.tzinfo is not None else dt.astimezone()


def _zone(tz_name: str | None) -> _tzinfo | None:
    """ZoneInfo for a task's timeZone; None means 'use the machine's local zone'."""
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:  # unknown/garbage zone: local beats blowing up a task list
        return None


def _midnight(day: _date, tz: _tzinfo | None) -> datetime:
    if tz is None:
        return datetime.combine(day, _time()).astimezone()
    return datetime.combine(day, _time(), tzinfo=tz)


def _all_day_date(dt: datetime, tz: _tzinfo | None) -> _date:
    """The calendar date an all-day stamp denotes.

    TickTick is not consistent here. Some payloads carry midnight of the task's zone
    expressed in UTC (``2026-08-13T18:00:00+0000`` for Asia/Dhaka); others carry the
    intended date with an arbitrary clock (``2019-11-13T03:00:00+0000`` with
    timeZone America/Los_Angeles — straight out of the API docs, and converting that
    one would move it to the 12th). Converting only fixes the first form, so convert
    when the result lands exactly on midnight and otherwise trust the date as written.
    """
    local = dt.astimezone(tz) if tz is not None else dt.astimezone()
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        return local.date()
    return dt.date()


def _day_month(day: _date, now: datetime) -> str:
    text = f"{day.day} {_MONTH_NAMES[day.month - 1]}"
    return text if day.year == now.year else f"{text} {day.year}"
