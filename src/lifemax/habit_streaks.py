"""Pure helpers for daily-checklist streak math.

Streaks are derived from a habit's `completed_dates` history (a sorted list of
"YYYY-MM-DD" strings, deduped). Keeping the math here means both the store and
the API can reason about it without touching the persistence layer.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

_ONE = timedelta(days=1)


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def normalize_history(dates: Iterable[str], *, max_keep: int) -> list[str]:
    """Return a sorted, deduped, length-capped history of ISO date strings.

    Invalid entries are dropped silently; we trust the caller (the store) to
    only ever feed already-validated dates, but we keep the cleanup here so a
    bad on-disk row never crashes the server.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in dates:
        if not isinstance(raw, str):
            continue
        try:
            parsed = _parse(raw)
        except ValueError:
            continue
        iso = parsed.isoformat()
        if iso in seen:
            continue
        seen.add(iso)
        cleaned.append(iso)
    cleaned.sort()
    if max_keep > 0 and len(cleaned) > max_keep:
        cleaned = cleaned[-max_keep:]
    return cleaned


def current_streak(dates: list[str], *, today_iso: str) -> int:
    """Length of the run of consecutive days ending at today or yesterday.

    A streak stays "alive" through the very next day so users don't lose it
    just because they haven't checked off today yet. The moment we cross to
    the day after that, the streak resets.
    """
    if not dates:
        return 0
    try:
        today = _parse(today_iso)
    except ValueError:
        return 0
    last = _parse(dates[-1])
    gap = (today - last).days
    if gap < 0 or gap > 1:
        return 0
    streak = 1
    cursor = last
    for raw in reversed(dates[:-1]):
        prev = _parse(raw)
        if (cursor - prev).days == 1:
            streak += 1
            cursor = prev
        else:
            break
    return streak


def best_streak(dates: list[str]) -> int:
    """Longest consecutive run anywhere in the history."""
    if not dates:
        return 0
    best = 1
    run = 1
    for prev_raw, curr_raw in zip(dates, dates[1:]):
        prev = _parse(prev_raw)
        curr = _parse(curr_raw)
        if (curr - prev).days == 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def done_last_7(dates: list[str], *, today_iso: str) -> list[bool]:
    """Return [today-6, ..., today] as booleans of whether each day is done."""
    try:
        today = _parse(today_iso)
    except ValueError:
        return [False] * 7
    in_set = set(dates)
    out: list[bool] = []
    for offset in range(6, -1, -1):
        d = today - (offset * _ONE)
        out.append(d.isoformat() in in_set)
    return out
