"""Tests for the weekly retro rollup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lifemax.models import Habit, Status, Task
from lifemax.widgets.retro import (
    WEEK_LENGTH_DAYS,
    compute_weekly_retro,
    date_range_iso,
    local_habit_date_for,
)


def test_local_habit_date_for_3am_cutoff():
    # Late-night-but-still-yesterday: 1 AM ET on Apr 19 → still Apr 18.
    base = datetime(2026, 4, 19, 5, 0, tzinfo=timezone.utc)  # 1 AM ET
    assert local_habit_date_for(base, "America/New_York") == "2026-04-18"
    # After cutoff: 4 AM ET on Apr 19 → Apr 19.
    base = datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)
    assert local_habit_date_for(base, "America/New_York") == "2026-04-19"


def test_date_range_iso_default_week():
    iso = date_range_iso("2026-04-19")
    assert len(iso) == WEEK_LENGTH_DAYS
    assert iso[0] == "2026-04-13"
    assert iso[-1] == "2026-04-19"


def test_date_range_iso_custom_length():
    iso = date_range_iso("2026-04-19", days=3)
    assert iso == ["2026-04-17", "2026-04-18", "2026-04-19"]


def _task(*, title: str, status: Status = Status.TODO, archived: bool = False, days_ago: int = 0):
    """Build a Task whose created_at + updated_at are anchored `days_ago`."""

    t = Task(title=title, status=status, archived=archived)
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    t.created_at = when
    t.updated_at = when
    return t


def test_retro_counts_completed_and_created_in_window():
    now = datetime(2026, 4, 19, 16, 0, tzinfo=timezone.utc)  # noon ET
    tasks = [
        _task(title="ship docs", status=Status.DONE, days_ago=2),  # completed in window
        _task(title="ship code", status=Status.DONE, days_ago=10),  # outside window
        _task(title="plan q3", days_ago=1),  # created in window, not done
        _task(title="archived junk", status=Status.DONE, archived=True, days_ago=3),
    ]
    retro = compute_weekly_retro(
        tasks=tasks,
        habits=[],
        focus_blocks_per_day={},
        timezone_name="America/New_York",
        now=now,
    )
    assert retro["window_end"] == "2026-04-19"
    assert retro["is_sunday"] is True
    assert retro["tasks"]["completed"] == 2  # ship docs + archived junk are both DONE in window
    assert retro["tasks"]["created"] == 3  # docs, plan q3, archived junk
    assert retro["tasks"]["completed_titles"][0] == "ship docs"


def test_retro_habits_compute_rate_and_top():
    now = datetime(2026, 4, 19, 16, 0, tzinfo=timezone.utc)
    h1 = Habit(title="exercise")
    h1.completed_dates = ["2026-04-13", "2026-04-15", "2026-04-17", "2026-04-19"]
    h2 = Habit(title="meditate")
    h2.completed_dates = ["2026-04-19"]
    h3 = Habit(title="read", archived=True)  # archived → ignored
    retro = compute_weekly_retro(
        tasks=[],
        habits=[h1, h2, h3],
        focus_blocks_per_day={},
        timezone_name="America/New_York",
        now=now,
    )
    # Possible = 2 active habits * 7 days = 14. Hits = 4 + 1 = 5.
    assert retro["habits"]["possible"] == 14
    assert retro["habits"]["completions"] == 5
    assert retro["habits"]["completion_rate"] == round(5 / 14, 3)
    assert retro["habits"]["top_habit"] == "exercise"
    assert retro["habits"]["top_habit_count"] == 4


def test_retro_focus_summary_picks_best_day():
    now = datetime(2026, 4, 19, 16, 0, tzinfo=timezone.utc)
    blocks = {
        "2026-04-13": 2,
        "2026-04-15": 5,  # the best day
        "2026-04-19": 3,
    }
    retro = compute_weekly_retro(
        tasks=[],
        habits=[],
        focus_blocks_per_day=blocks,
        timezone_name="America/New_York",
        now=now,
    )
    assert retro["focus"]["blocks_total"] == 10
    assert retro["focus"]["best_day"] == "2026-04-15"
    assert retro["focus"]["best_day_blocks"] == 5


def test_retro_daily_strip_has_seven_entries():
    now = datetime(2026, 4, 19, 16, 0, tzinfo=timezone.utc)
    h = Habit(title="exercise")
    h.completed_dates = ["2026-04-19"]
    retro = compute_weekly_retro(
        tasks=[],
        habits=[h],
        focus_blocks_per_day={"2026-04-19": 4},
        timezone_name="America/New_York",
        now=now,
    )
    daily = retro["daily"]
    assert len(daily) == WEEK_LENGTH_DAYS
    today_row = daily[-1]
    assert today_row["date"] == "2026-04-19"
    assert today_row["habits_done"] == 1
    assert today_row["focus_blocks"] == 4


def test_is_sunday_flag_is_correct_for_other_days():
    # Apr 18 2026 is a Saturday.
    now = datetime(2026, 4, 18, 16, 0, tzinfo=timezone.utc)
    retro = compute_weekly_retro(
        tasks=[],
        habits=[],
        focus_blocks_per_day={},
        timezone_name="America/New_York",
        now=now,
    )
    assert retro["is_sunday"] is False
