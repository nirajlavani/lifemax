"""Deadline tier classification + 'next due' countdown picker.

Pure helpers — no I/O, no async — so they're trivially testable and can be
called inside `build_snapshot` next to `pick_focus`. The output drives:

- the topbar 'next due' countdown,
- the per-task `tier` annotation on kanban cards (so the JS can colour the
  card edges without re-parsing deadlines on the client).

`tier` is one of {overdue, today, soon, later, none}. `none` is reserved for
tasks with no deadline at all; the UI doesn't draw an accent for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from ..models import Status, Task

Tier = str  # one of {"overdue", "today", "soon", "later", "none"}

# Tasks that fall within `_SOON_WINDOW_S` of now but aren't due today are
# treated as the 'soon' bucket. 24h matches the focus-card heuristic.
_SOON_WINDOW_S = 24 * 60 * 60


@dataclass(slots=True, frozen=True)
class _Classified:
    task: Task
    tier: Tier
    deadline: datetime | None  # in the local tz, if any


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


def _classify(task: Task, now: datetime, tz: ZoneInfo) -> _Classified:
    if task.archived or task.status == Status.DONE:
        return _Classified(task=task, tier="none", deadline=None)
    deadline = _parse_deadline(task.deadline, tz)
    if deadline is None:
        return _Classified(task=task, tier="none", deadline=None)

    delta_s = (deadline - now).total_seconds()
    if delta_s < 0:
        return _Classified(task=task, tier="overdue", deadline=deadline)

    end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=0)
    if deadline <= end_of_today:
        return _Classified(task=task, tier="today", deadline=deadline)

    if delta_s <= _SOON_WINDOW_S:
        return _Classified(task=task, tier="soon", deadline=deadline)

    return _Classified(task=task, tier="later", deadline=deadline)


def _humanize_delta(target: datetime, now: datetime) -> str:
    """Compact, lower-case relative time string ("in 2h", "overdue 1d").

    Mirrors `widgets.focus._humanize_delta` so the focus card and topbar
    countdown speak the same language.
    """

    delta = target - now
    seconds = delta.total_seconds()
    if seconds < 0:
        ago = -seconds
        if ago < 60:
            return "overdue"
        if ago < 3600:
            return f"overdue {int(ago // 60)}m"
        if ago < 86400:
            return f"overdue {int(ago // 3600)}h"
        return f"overdue {int(ago // 86400)}d"
    if seconds < 60:
        return "due now"
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    if seconds < 86400:
        return f"in {int(seconds // 3600)}h"
    return f"in {int(seconds // 86400)}d"


def compute_nudges(
    tasks: Iterable[Task],
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> dict:
    """Classify tasks and pick the 'next due' headline for the topbar.

    Returns a JSON-friendly dict shaped like:

    {
      "next_due": {
        "task_id": "abc-123",
        "title": "ship the deck",
        "tier": "today",
        "countdown_label": "in 2h",
        "deadline": "2026-04-18T17:00:00-04:00"
      } | None,
      "tier_counts": {"overdue": 2, "today": 4, "soon": 1, "later": 7},
      "task_tiers": {"abc-123": "today", ...},
      "computed_at": "2026-04-18T15:00:00-04:00"
    }
    """

    tz = ZoneInfo(timezone_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    classified: list[_Classified] = []
    for t in tasks:
        classified.append(_classify(t, now, tz))

    tier_counts = {"overdue": 0, "today": 0, "soon": 0, "later": 0}
    task_tiers: dict[str, Tier] = {}
    for row in classified:
        task_tiers[row.task.id] = row.tier
        if row.tier in tier_counts:
            tier_counts[row.tier] += 1

    # Pick the headline for the topbar:
    # 1. oldest overdue first (most pain), then
    # 2. earliest deadline today, then
    # 3. earliest deadline soon.
    #
    # Tasks in 'later' are intentionally excluded from the countdown — the
    # topbar should only nudge when there's something pressing.
    overdue = [c for c in classified if c.tier == "overdue" and c.deadline]
    today = [c for c in classified if c.tier == "today" and c.deadline]
    soon = [c for c in classified if c.tier == "soon" and c.deadline]

    overdue.sort(key=lambda c: c.deadline)  # oldest first
    today.sort(key=lambda c: c.deadline)
    soon.sort(key=lambda c: c.deadline)

    headline: _Classified | None = None
    if overdue:
        headline = overdue[0]
    elif today:
        headline = today[0]
    elif soon:
        headline = soon[0]

    next_due_payload: dict | None = None
    if headline is not None and headline.deadline is not None:
        next_due_payload = {
            "task_id": headline.task.id,
            "title": headline.task.title,
            "tier": headline.tier,
            "countdown_label": _humanize_delta(headline.deadline, now),
            "deadline": headline.deadline.isoformat(),
        }

    return {
        "next_due": next_due_payload,
        "tier_counts": tier_counts,
        "task_tiers": task_tiers,
        "computed_at": now.isoformat(),
    }
