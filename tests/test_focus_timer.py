"""Tests for the in-memory focus timer state machine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lifemax.dispatch_history import parse_literal_timer
from lifemax.widgets.focus_timer import FocusTimer


def _now(t: datetime) -> datetime:
    """Helper to keep test intent obvious."""
    return t


@pytest.mark.asyncio
async def test_start_focus_default_25_minutes():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    snap = await timer.start_focus(now=_now(base))
    assert snap["state"] == "running"
    assert snap["phase"] == "focus"
    assert snap["total_seconds"] == 25 * 60
    assert snap["remaining_seconds"] == 25 * 60
    assert snap["last_event"]["kind"] == "started"


@pytest.mark.asyncio
async def test_pause_then_resume_preserves_remaining():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    await timer.start_focus(minutes=20, now=base)
    paused = await timer.pause(now=base + timedelta(minutes=5))
    assert paused["state"] == "paused"
    assert paused["remaining_seconds"] == 15 * 60
    resumed = await timer.resume(now=base + timedelta(minutes=10))
    assert resumed["state"] == "running"
    assert resumed["remaining_seconds"] == 15 * 60


@pytest.mark.asyncio
async def test_extend_adds_minutes_when_running():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    await timer.start_focus(minutes=25, now=base)
    extended = await timer.extend(minutes=5, now=base + timedelta(minutes=10))
    assert extended["state"] == "running"
    assert extended["remaining_seconds"] == 20 * 60


@pytest.mark.asyncio
async def test_stop_returns_to_idle():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    await timer.start_focus(now=base)
    stopped = await timer.stop(now=base + timedelta(minutes=2))
    assert stopped["state"] == "idle"
    assert stopped["remaining_seconds"] == 0


@pytest.mark.asyncio
async def test_break_short_default():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    snap = await timer.start_break(now=base)
    assert snap["state"] == "break"
    assert snap["phase"] in {"break_short", "break_long"}
    assert snap["remaining_seconds"] > 0


@pytest.mark.asyncio
async def test_completed_focus_blocks_increment_after_elapse():
    timer = FocusTimer(timezone_name="America/New_York")
    base = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
    await timer.start_focus(minutes=1, now=base)
    snap = await timer.snapshot(now=base + timedelta(minutes=2))
    assert snap["completed_focus_blocks_today"] >= 1
    assert snap["last_event"]["kind"] == "elapsed"


def test_parse_literal_timer_basic_verbs():
    assert parse_literal_timer("pomodoro") == {"op": "start", "minutes": None, "label": None}
    assert parse_literal_timer("start timer") == {"op": "start", "minutes": None, "label": None}
    assert parse_literal_timer("stop pomodoro") == {"op": "stop", "minutes": None, "label": None}
    assert parse_literal_timer("pause focus") == {"op": "pause", "minutes": None, "label": None}
    assert parse_literal_timer("resume focus block") == {"op": "resume", "minutes": None, "label": None}
    assert parse_literal_timer("take a break") == {"op": "break", "minutes": None, "label": None}


def test_parse_literal_timer_with_minutes():
    assert parse_literal_timer("focus 50") == {"op": "start", "minutes": 50, "label": None}
    assert parse_literal_timer("pomodoro 25 min") == {"op": "start", "minutes": 25, "label": None}
    assert parse_literal_timer("extend 5") == {"op": "extend", "minutes": 5, "label": None}
    assert parse_literal_timer("break 10") == {"op": "break", "minutes": 10, "label": None}


def test_parse_literal_timer_rejects_garbage():
    assert parse_literal_timer("") is None
    assert parse_literal_timer("focus on shipping the docs") is None
    assert parse_literal_timer("extend") is None  # bare extend has no default
    assert parse_literal_timer("focus 0") is None
    assert parse_literal_timer("focus 999") is None
