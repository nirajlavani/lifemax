"""Pick the single 'do this now' task for the focus card.

Pure ranking — no I/O, no async — so it's trivial to test and to call from
inside `build_snapshot`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from ..models import Priority, Status, Task


@dataclass(slots=True, frozen=True)
class FocusPick:
    task: Task
    score: float
    reason: str


_PRIORITY_OFFSET = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 10,
    Priority.LOW: 20,
}


def _parse_deadline(raw: str | None, tz: ZoneInfo) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _humanize_delta(target: datetime, now: datetime) -> str:
    """Compact, lower-case relative time string ("in 2h", "overdue 1d")."""
    delta = target - now
    seconds = delta.total_seconds()
    if seconds < 0:
        ago = -seconds
        if ago < 3600:
            return f"overdue {int(ago // 60)}m"
        if ago < 86400:
            return f"overdue {int(ago // 3600)}h"
        return f"overdue {int(ago // 86400)}d"
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    if seconds < 86400:
        return f"in {int(seconds // 3600)}h"
    return f"in {int(seconds // 86400)}d"


def _score(task: Task, now: datetime, tz: ZoneInfo) -> tuple[float, str]:
    deadline = _parse_deadline(task.deadline, tz)
    is_urgent = task.urgency.value == "urgent"
    is_in_progress = task.status == Status.IN_PROGRESS
    prio_off = _PRIORITY_OFFSET.get(task.priority, 20)

    if deadline is not None:
        delta_s = (deadline - now).total_seconds()
        # Overdue → always wins.
        if delta_s < 0:
            days_overdue = max(1, int(-delta_s // 86400))
            return (-1000 - days_overdue * 10, f"overdue {days_overdue}d")
        # Due today (within ~end of local day).
        end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if deadline <= end_of_today:
            base = 0 if is_urgent else (5 if task.priority == Priority.HIGH else 10)
            return (base, f"due today · {_humanize_delta(deadline, now)}")
        # Within next 24 hours.
        if delta_s <= 86400:
            base = 15 if is_urgent else 25
            return (base, f"soon · {_humanize_delta(deadline, now)}")
        # Future, deadline known.
        base = 60 + prio_off
        return (base, f"due {_humanize_delta(deadline, now)}")

    # No deadline.
    if is_in_progress:
        return (30, "in progress")
    if is_urgent:
        return (40, "urgent")
    return (100 + prio_off, task.priority.value)


def pick_focus(
    tasks: Iterable[Task],
    *,
    timezone_name: str,
    now: datetime | None = None,
    runners_up: int = 2,
) -> dict | None:
    """Return the single best 'do this now' task, with optional runners-up.

    The return shape is the SSE-friendly dict (Pydantic-serialized task) so
    `build_snapshot` can drop it straight into the payload. Returns `None` if
    there is nothing actionable.
    """
    tz = ZoneInfo(timezone_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    actionable: list[tuple[float, str, Task]] = []
    for t in tasks:
        if t.archived or t.status == Status.DONE:
            continue
        score, reason = _score(t, now, tz)
        actionable.append((score, reason, t))

    if not actionable:
        return None

    actionable.sort(
        key=lambda row: (
            row[0],
            row[2].deadline or "9999-12-31T00:00:00",
            row[2].created_at,
        )
    )
    primary_score, primary_reason, primary = actionable[0]
    rest = actionable[1 : 1 + max(0, runners_up)]
    return {
        "task": primary.model_dump(mode="json"),
        "reason": primary_reason,
        "score": primary_score,
        "next_up": [
            {"id": t.id, "title": t.title, "reason": reason}
            for _score_value, reason, t in rest
        ],
    }
