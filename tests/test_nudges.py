"""Tests for `widgets.nudges.compute_nudges`: tier classification + headline pick."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from lifemax.models import Priority, Status, Task, Urgency
from lifemax.widgets.nudges import compute_nudges


_TZ_NAME = "America/New_York"
_TZ = ZoneInfo(_TZ_NAME)


def _make_task(
    *,
    task_id: str,
    title: str = "x",
    deadline: datetime | None = None,
    status: Status = Status.TODO,
    archived: bool = False,
    priority: Priority = Priority.MEDIUM,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        deadline=deadline.isoformat() if deadline else None,
        status=status,
        archived=archived,
        priority=priority,
        urgency=Urgency.NON_URGENT,
        created_at="2026-04-18T10:00:00-04:00",
        updated_at="2026-04-18T10:00:00-04:00",
    )


def _now() -> datetime:
    # Pin "now" so the test isn't time-of-day flaky.
    return datetime(2026, 4, 18, 12, 0, tzinfo=_TZ)


def test_classifies_overdue_today_soon_later_and_none():
    now = _now()
    tasks = [
        _make_task(task_id="overdue", deadline=now - timedelta(hours=2)),
        _make_task(task_id="today",   deadline=now + timedelta(hours=3)),
        _make_task(task_id="soon",    deadline=now + timedelta(hours=18)),
        _make_task(task_id="later",   deadline=now + timedelta(days=3)),
        _make_task(task_id="none",    deadline=None),
    ]
    out = compute_nudges(tasks, timezone_name=_TZ_NAME, now=now)
    tiers = out["task_tiers"]
    assert tiers == {
        "overdue": "overdue",
        "today": "today",
        "soon": "soon",
        "later": "later",
        "none": "none",
    }
    counts = out["tier_counts"]
    assert counts == {"overdue": 1, "today": 1, "soon": 1, "later": 1}


def test_done_and_archived_are_classified_as_none():
    now = _now()
    tasks = [
        _make_task(task_id="done",     status=Status.DONE,     deadline=now - timedelta(hours=5)),
        _make_task(task_id="archived", archived=True,           deadline=now - timedelta(hours=5)),
    ]
    out = compute_nudges(tasks, timezone_name=_TZ_NAME, now=now)
    assert out["task_tiers"]["done"] == "none"
    assert out["task_tiers"]["archived"] == "none"
    assert out["tier_counts"]["overdue"] == 0


def test_headline_prefers_oldest_overdue_then_today_then_soon():
    now = _now()
    overdue_old = _make_task(task_id="o-old", deadline=now - timedelta(days=2), title="oldest pain")
    overdue_new = _make_task(task_id="o-new", deadline=now - timedelta(hours=1), title="recent pain")
    due_today_late = _make_task(task_id="t-late", deadline=now + timedelta(hours=8), title="due tonight")
    due_today_early = _make_task(task_id="t-early", deadline=now + timedelta(hours=2), title="due soon today")
    soon = _make_task(task_id="s", deadline=now + timedelta(hours=20), title="due tomorrow")

    out = compute_nudges(
        [soon, due_today_late, overdue_new, due_today_early, overdue_old],
        timezone_name=_TZ_NAME,
        now=now,
    )
    assert out["next_due"]["task_id"] == "o-old"
    assert out["next_due"]["tier"] == "overdue"
    assert out["next_due"]["countdown_label"].startswith("overdue")


def test_headline_falls_back_to_today_when_no_overdue():
    now = _now()
    out = compute_nudges(
        [
            _make_task(task_id="t1", deadline=now + timedelta(hours=5), title="t1"),
            _make_task(task_id="t2", deadline=now + timedelta(hours=2), title="t2"),
        ],
        timezone_name=_TZ_NAME,
        now=now,
    )
    assert out["next_due"]["task_id"] == "t2"
    assert out["next_due"]["tier"] == "today"


def test_headline_skips_later_when_only_later_exists():
    now = _now()
    out = compute_nudges(
        [_make_task(task_id="far", deadline=now + timedelta(days=10))],
        timezone_name=_TZ_NAME,
        now=now,
    )
    assert out["next_due"] is None
    assert out["tier_counts"]["later"] == 1


def test_empty_input_is_safe():
    now = _now()
    out = compute_nudges([], timezone_name=_TZ_NAME, now=now)
    assert out["next_due"] is None
    assert out["task_tiers"] == {}
    assert out["tier_counts"] == {"overdue": 0, "today": 0, "soon": 0, "later": 0}
    assert out["computed_at"] == now.isoformat()


def test_naive_now_is_treated_as_local_tz():
    # If a caller passes a naive datetime we attach the timezone rather than
    # blowing up; this matches the focus widget's behaviour.
    now_naive = datetime(2026, 4, 18, 12, 0)
    tasks = [
        _make_task(task_id="t", deadline=datetime(2026, 4, 18, 10, 0, tzinfo=_TZ)),
    ]
    out = compute_nudges(tasks, timezone_name=_TZ_NAME, now=now_naive)
    assert out["task_tiers"]["t"] == "overdue"
