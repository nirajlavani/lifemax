"""Weekly retro rollup.

Aggregates the last 7 *local* habit-days into a small, snapshot-friendly
payload the dashboard can render as a Sunday card and the LLM can quote
when asked "how was my week?".

Pure functions — no I/O — so this is easy to unit test and easy to call
from anywhere in the app (HTTP endpoint, snapshot builder, CLI bridge).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import HABIT_DAY_CUTOFF_HOUR
from ..models import Habit, Status, Task

WEEK_LENGTH_DAYS = 7


def local_habit_date_for(now: datetime, tz_name: str) -> str:
    """Return the local 'today' ISO using the same 3 AM cutoff as habits."""

    local = now.astimezone(ZoneInfo(tz_name))
    if local.hour < HABIT_DAY_CUTOFF_HOUR:
        local = local - timedelta(days=1)
    return local.date().isoformat()


def date_range_iso(today_iso: str, *, days: int = WEEK_LENGTH_DAYS) -> list[str]:
    """Return the last `days` ISO dates ending with `today_iso` (inclusive)."""

    today = datetime.fromisoformat(today_iso).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _task_completed_in_window(task: Task, *, dates: set[str], tz_name: str) -> bool:
    """Decide whether a task was 'completed during the window'.

    We treat a task as completed if it's in DONE status and its `updated_at`
    falls within the local-day range we care about. We don't store an explicit
    `completed_at`, so this is the best signal we have without changing the
    model.
    """

    if task.status != Status.DONE:
        return False
    if not task.updated_at:
        return False
    try:
        updated = datetime.fromisoformat(task.updated_at)
    except ValueError:
        return False
    local = updated.astimezone(ZoneInfo(tz_name))
    if local.hour < HABIT_DAY_CUTOFF_HOUR:
        local = local - timedelta(days=1)
    return local.date().isoformat() in dates


def _task_created_in_window(task: Task, *, dates: set[str], tz_name: str) -> bool:
    if not task.created_at:
        return False
    try:
        created = datetime.fromisoformat(task.created_at)
    except ValueError:
        return False
    local = created.astimezone(ZoneInfo(tz_name))
    if local.hour < HABIT_DAY_CUTOFF_HOUR:
        local = local - timedelta(days=1)
    return local.date().isoformat() in dates


def _habit_done_count_in_window(
    habits: Iterable[Habit],
    *,
    dates: set[str],
) -> tuple[int, int, dict[str, int]]:
    """Return (total_completions_in_window, total_possible, per_habit_count).

    "Total possible" = number of (habit, day) pairs that *could* have been
    checked off. Skips archived habits.
    """

    per_habit: dict[str, int] = {}
    completions = 0
    possible = 0
    for habit in habits:
        if habit.archived:
            continue
        possible += len(dates)
        hits = sum(1 for d in habit.completed_dates if d in dates)
        per_habit[habit.title] = hits
        completions += hits
    return completions, possible, per_habit


def compute_weekly_retro(
    *,
    tasks: list[Task],
    habits: list[Habit],
    focus_blocks_per_day: dict[str, int],
    timezone_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Build the weekly retro snapshot for the 7 local days ending `now`.

    All counts are over the local week ending today; "today" is the same
    habit-day used elsewhere in the app (3 AM cutoff). The payload is
    intentionally small and snapshot-friendly — no nested objects beyond
    one level of headline lists.
    """

    today_iso = local_habit_date_for(now, timezone_name)
    window_dates = date_range_iso(today_iso)
    window_set = set(window_dates)

    tasks_completed = [
        t for t in tasks if _task_completed_in_window(t, dates=window_set, tz_name=timezone_name)
    ]
    tasks_created = sum(
        1 for t in tasks if _task_created_in_window(t, dates=window_set, tz_name=timezone_name)
    )
    tasks_archived = sum(
        1
        for t in tasks
        if t.archived
        and _task_created_in_window(t, dates=window_set, tz_name=timezone_name) is False
        and _task_completed_in_window(t, dates=window_set, tz_name=timezone_name) is False
    )

    completions, possible, per_habit = _habit_done_count_in_window(habits, dates=window_set)
    completion_rate = (completions / possible) if possible else 0.0

    # Pick the habit with the strongest week and the focus day with the most blocks.
    top_habit_title: str | None = None
    top_habit_count = 0
    for title, count in per_habit.items():
        if count > top_habit_count:
            top_habit_count = count
            top_habit_title = title

    blocks_total = sum(focus_blocks_per_day.get(d, 0) for d in window_dates)
    best_focus_day_iso: str | None = None
    best_focus_day_count = 0
    for d in window_dates:
        c = focus_blocks_per_day.get(d, 0)
        if c > best_focus_day_count:
            best_focus_day_count = c
            best_focus_day_iso = d

    # `daily` is a list of small dicts the UI can render as a 7-bar strip.
    daily: list[dict[str, Any]] = [
        {
            "date": d,
            "tasks_done": sum(
                1
                for t in tasks_completed
                if _task_completed_in_window(t, dates={d}, tz_name=timezone_name)
            ),
            "habits_done": sum(
                1 for habit in habits if not habit.archived and d in habit.completed_dates
            ),
            "focus_blocks": focus_blocks_per_day.get(d, 0),
        }
        for d in window_dates
    ]

    return {
        "window_start": window_dates[0],
        "window_end": window_dates[-1],
        "today_local_date": today_iso,
        "is_sunday": _is_sunday(today_iso),
        "tasks": {
            "completed": len(tasks_completed),
            "created": tasks_created,
            "archived": tasks_archived,
            "completed_titles": [t.title for t in tasks_completed[:5]],
        },
        "habits": {
            "completions": completions,
            "possible": possible,
            "completion_rate": round(completion_rate, 3),
            "top_habit": top_habit_title,
            "top_habit_count": top_habit_count,
        },
        "focus": {
            "blocks_total": blocks_total,
            "best_day": best_focus_day_iso,
            "best_day_blocks": best_focus_day_count,
        },
        "daily": daily,
    }


def _is_sunday(date_iso: str) -> bool:
    """`weekday()` returns 6 for Sunday in Python's calendar module."""

    try:
        return datetime.fromisoformat(date_iso).weekday() == 6
    except ValueError:
        return False
