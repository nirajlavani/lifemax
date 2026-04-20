"""Timezone-aware helpers for the ambient clock and "today" filtering."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_ONE_DAY = timedelta(days=1)


def now_in_tz(tz_name: str) -> datetime:
    """Current wall-clock time in the given IANA timezone."""
    return datetime.now(ZoneInfo(tz_name))


def today_in_tz(tz_name: str) -> date:
    """Today's calendar date in the given IANA timezone."""
    return now_in_tz(tz_name).date()


def habit_day_in_tz(tz_name: str, *, cutoff_hour: int = 3) -> date:
    """Habit "today" with a configurable late-night cutoff.

    A check-off recorded at, say, 1:30am still counts for the previous calendar
    day so users can close out late-night routines. Anything at/after the cutoff
    rolls into the new day. Mirror logic on the client must use the same cutoff.
    """
    if not 0 <= cutoff_hour <= 23:
        raise ValueError("cutoff_hour must be 0..23")
    now = now_in_tz(tz_name)
    if now.hour < cutoff_hour:
        return (now - _ONE_DAY).date()
    return now.date()


def format_clock(now: datetime) -> dict:
    """Render the clock card as a small JSON-friendly dict."""
    return {
        "iso": now.isoformat(),
        "time_12h": now.strftime("%-I:%M %p").lower(),  # e.g. "3:07 pm"
        "time_24h": now.strftime("%H:%M"),
        "date_short": now.strftime("%a, %b %-d"),
        "date_long": now.strftime("%A, %B %-d, %Y"),
        "tz_label": now.strftime("%Z"),
    }


def format_today_summary(d: date) -> str:
    return d.strftime("%A, %B %-d, %Y")


def is_due_today(deadline_iso: str | None, tz_name: str) -> bool:
    """Return True if `deadline_iso` falls on today's date in `tz_name`."""
    if not deadline_iso:
        return False
    try:
        dt = datetime.fromisoformat(deadline_iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name)).date() == today_in_tz(tz_name)
