"""Tests for HabitStore + the habit-related Intent applier."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lifemax.habits_store import HabitStore
from lifemax.intents import apply_intent
from lifemax.models import Habit, HabitFields, Intent
from lifemax.widgets.time_utils import habit_day_in_tz


@pytest.fixture()
def tmp_habits(tmp_path):
    """Fresh, unseeded HabitStore rooted in a tmp directory."""
    return HabitStore(
        path=tmp_path / "habits.json",
        backup_dir=tmp_path / "backups",
        seed_starter=False,
    )


@pytest.fixture()
def seeded_habits(tmp_path):
    return HabitStore(
        path=tmp_path / "habits.json",
        backup_dir=tmp_path / "backups",
        seed_starter=True,
    )


class _NoopLLM:
    async def answer_query(self, *_a, **_kw):  # pragma: no cover
        return "noop"


# --------------------------------------------------------------------------
# Store-level tests
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_seeds_starter_set_when_file_missing(seeded_habits) -> None:
    await seeded_habits.load()
    items = await seeded_habits.all_habits()
    titles = [h.title for h in items]
    assert "exercise" in titles
    assert len(items) >= 5
    # Sort order must be deterministic for the 3-column grid.
    assert items == sorted(items, key=lambda h: (h.sort_order, h.created_at))


@pytest.mark.asyncio
async def test_add_and_remove(tmp_habits) -> None:
    await tmp_habits.load()
    h = await tmp_habits.add("read 30 min")
    assert h.title == "read 30 min"
    assert h.sort_order == 0  # first item
    found = await tmp_habits.find_by_title("read")
    assert found is not None and found.id == h.id

    removed = await tmp_habits.remove(h.id)
    assert removed is not None
    assert await tmp_habits.find_by_title("read") is None


@pytest.mark.asyncio
async def test_add_rejects_blank_title(tmp_habits) -> None:
    await tmp_habits.load()
    with pytest.raises(ValueError):
        await tmp_habits.add("   ")


@pytest.mark.asyncio
async def test_mark_done_is_idempotent_per_day(tmp_habits) -> None:
    await tmp_habits.load()
    h = await tmp_habits.add("meditate")
    today = "2026-04-19"
    a = await tmp_habits.mark_done(h.id, local_date_iso=today)
    assert a is not None and a.last_done_local_date == today
    # Calling again with the same date is a no-op (no duplicate write side-effects).
    b = await tmp_habits.mark_done(h.id, local_date_iso=today)
    assert b is not None and b.last_done_local_date == today
    assert a.updated_at == b.updated_at


@pytest.mark.asyncio
async def test_undo_only_clears_when_date_matches(tmp_habits) -> None:
    await tmp_habits.load()
    h = await tmp_habits.add("journal")
    yesterday = "2026-04-18"
    today = "2026-04-19"
    await tmp_habits.mark_done(h.id, local_date_iso=yesterday)
    # Undoing for a different date does not wipe yesterday's record.
    refreshed = await tmp_habits.undo_done(h.id, local_date_iso=today)
    assert refreshed is not None
    assert refreshed.last_done_local_date == yesterday
    # Undoing for the matching date clears it.
    cleared = await tmp_habits.undo_done(h.id, local_date_iso=yesterday)
    assert cleared is not None
    assert cleared.last_done_local_date is None


@pytest.mark.asyncio
async def test_writes_persist_across_instances(tmp_path) -> None:
    path = tmp_path / "habits.json"
    backups = tmp_path / "backups"
    a = HabitStore(path=path, backup_dir=backups, seed_starter=False)
    await a.load()
    h = await a.add("walk 10k steps")
    await a.mark_done(h.id, local_date_iso="2026-04-19")

    b = HabitStore(path=path, backup_dir=backups, seed_starter=False)
    await b.load()
    items = await b.all_habits()
    assert [it.title for it in items] == ["walk 10k steps"]
    assert items[0].last_done_local_date == "2026-04-19"


# --------------------------------------------------------------------------
# 3am cutoff behavior
# --------------------------------------------------------------------------

class _FakeNow:
    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def __call__(self, _tz_name: str) -> datetime:
        return self._dt


def test_habit_day_uses_previous_day_before_cutoff(monkeypatch) -> None:
    from lifemax.widgets import time_utils

    tz = ZoneInfo("America/New_York")
    # 1:30am local — pre-cutoff, should still count for the previous date.
    monkeypatch.setattr(
        time_utils, "now_in_tz", _FakeNow(datetime(2026, 4, 19, 1, 30, tzinfo=tz))
    )
    assert habit_day_in_tz("America/New_York", cutoff_hour=3).isoformat() == "2026-04-18"

    # Exactly 3:00am — rolls into the new day.
    monkeypatch.setattr(
        time_utils, "now_in_tz", _FakeNow(datetime(2026, 4, 19, 3, 0, tzinfo=tz))
    )
    assert habit_day_in_tz("America/New_York", cutoff_hour=3).isoformat() == "2026-04-19"

    # 8pm — clearly the same calendar day.
    monkeypatch.setattr(
        time_utils, "now_in_tz", _FakeNow(datetime(2026, 4, 19, 20, 0, tzinfo=tz))
    )
    assert habit_day_in_tz("America/New_York", cutoff_hour=3).isoformat() == "2026-04-19"


# --------------------------------------------------------------------------
# Intent applier
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_add_habit(tmp_store, tmp_habits) -> None:
    await tmp_habits.load()
    intent = Intent(action="add_habit", habit=HabitFields(title="hydrate"))
    result = await apply_intent(
        intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert result.ok is True
    assert result.habit is not None and result.habit.title == "hydrate"


@pytest.mark.asyncio
async def test_apply_add_habit_dedupes(tmp_store, tmp_habits) -> None:
    await tmp_habits.load()
    await tmp_habits.add("hydrate")
    intent = Intent(action="add_habit", habit=HabitFields(title="hydrate"))
    result = await apply_intent(
        intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert result.ok is False
    assert "already" in result.message.lower()


@pytest.mark.asyncio
async def test_apply_check_and_uncheck(tmp_store, tmp_habits) -> None:
    await tmp_habits.load()
    h = await tmp_habits.add("exercise")
    today_iso = habit_day_in_tz("America/New_York", cutoff_hour=3).isoformat()

    check_intent = Intent(
        action="check_habit", habit=HabitFields(match_title="exercise")
    )
    res = await apply_intent(
        check_intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert res.ok is True
    refreshed = await tmp_habits.get(h.id)
    assert refreshed is not None and refreshed.last_done_local_date == today_iso

    uncheck_intent = Intent(
        action="uncheck_habit", habit=HabitFields(match_title="exercise")
    )
    res2 = await apply_intent(
        uncheck_intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert res2.ok is True
    cleared = await tmp_habits.get(h.id)
    assert cleared is not None and cleared.last_done_local_date is None


@pytest.mark.asyncio
async def test_apply_remove_habit(tmp_store, tmp_habits) -> None:
    await tmp_habits.load()
    await tmp_habits.add("phone-free hour")
    intent = Intent(
        action="remove_habit", habit=HabitFields(match_title="phone")
    )
    res = await apply_intent(
        intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert res.ok is True
    assert (await tmp_habits.find_by_title("phone")) is None


@pytest.mark.asyncio
async def test_apply_check_unknown_habit_is_polite_no_op(tmp_store, tmp_habits) -> None:
    await tmp_habits.load()
    intent = Intent(
        action="check_habit", habit=HabitFields(match_title="not a real item")
    )
    res = await apply_intent(
        intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=tmp_habits,
    )
    assert res.ok is False


@pytest.mark.asyncio
async def test_habit_actions_require_store_present(tmp_store) -> None:
    intent = Intent(action="add_habit", habit=HabitFields(title="walk"))
    res = await apply_intent(
        intent,
        tmp_store,
        _NoopLLM(),
        timezone_name="America/New_York",
        habits=None,
    )
    assert res.ok is False
