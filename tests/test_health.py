"""Tests for the health-vitals badge module."""

from __future__ import annotations

import time

import pytest

from lifemax.widgets.health import HealthMonitor


class _FakeStore:
    """Minimal stand-in matching the duck-typed contract of TaskStore/HabitStore."""

    def __init__(
        self,
        *,
        last_io_at: float = 0.0,
        last_load_error: str | None = None,
        last_save_error: str | None = None,
    ) -> None:
        self.last_io_at = last_io_at
        self.last_load_error = last_load_error
        self.last_save_error = last_save_error


class _FakeWeather:
    def __init__(
        self,
        *,
        last_fetch_at: float = 0.0,
        last_fetch_error: str | None = None,
    ) -> None:
        self.last_fetch_at = last_fetch_at
        self.last_fetch_error = last_fetch_error


def _badge(payload: dict, key: str) -> dict:
    for b in payload["badges"]:
        if b["key"] == key:
            return b
    raise AssertionError(f"badge {key!r} missing from {payload['badges']!r}")


@pytest.mark.asyncio
async def test_compose_returns_one_badge_per_subsystem():
    monitor = HealthMonitor()
    payload = await monitor.compose(
        tasks_store=_FakeStore(),
        habits_store=_FakeStore(),
        weather=_FakeWeather(),
        calendar_status=None,
    )
    keys = [b["key"] for b in payload["badges"]]
    assert keys == ["store", "habits", "weather", "calendar", "llm"]
    assert payload["tier"] in {"ok", "degraded", "down", "unknown"}
    assert "computed_at_wall" in payload


@pytest.mark.asyncio
async def test_store_save_error_is_down():
    monitor = HealthMonitor()
    bad = _FakeStore(
        last_io_at=time.monotonic(),
        last_save_error="OSError: disk full",
    )
    payload = await monitor.compose(
        tasks_store=bad,
        habits_store=None,
        weather=_FakeWeather(),
        calendar_status=None,
    )
    store_badge = _badge(payload, "store")
    assert store_badge["tier"] == "down"
    assert "OSError" in store_badge["message"]
    # Worst wins the top-line tier.
    assert payload["tier"] == "down"


@pytest.mark.asyncio
async def test_store_load_fallback_is_degraded():
    monitor = HealthMonitor()
    nope = _FakeStore(
        last_io_at=time.monotonic(),
        last_load_error="JSONDecodeError: bad",
    )
    payload = await monitor.compose(
        tasks_store=nope,
        habits_store=None,
        weather=_FakeWeather(),
        calendar_status=None,
    )
    assert _badge(payload, "store")["tier"] == "degraded"


@pytest.mark.asyncio
async def test_weather_unknown_when_no_fetch_yet():
    monitor = HealthMonitor()
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),
        habits_store=None,
        weather=_FakeWeather(),  # last_fetch_at = 0 → never fetched
        calendar_status=None,
    )
    assert _badge(payload, "weather")["tier"] == "unknown"


@pytest.mark.asyncio
async def test_weather_recent_success_is_ok():
    monitor = HealthMonitor()
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),
        habits_store=None,
        weather=_FakeWeather(last_fetch_at=time.monotonic()),
        calendar_status=None,
    )
    assert _badge(payload, "weather")["tier"] == "ok"


@pytest.mark.asyncio
async def test_calendar_status_maps_to_badge():
    monitor = HealthMonitor()

    # Available + no error → ok
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),
        habits_store=None,
        weather=_FakeWeather(last_fetch_at=time.monotonic()),
        calendar_status={"available": True, "error": None, "write_calendar": "lifemax"},
    )
    assert _badge(payload, "calendar")["tier"] == "ok"

    # Error → down
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),
        habits_store=None,
        weather=_FakeWeather(last_fetch_at=time.monotonic()),
        calendar_status={"available": False, "error": "denied", "write_calendar": "lifemax"},
    )
    assert _badge(payload, "calendar")["tier"] == "down"

    # No status → unknown
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),
        habits_store=None,
        weather=_FakeWeather(last_fetch_at=time.monotonic()),
        calendar_status=None,
    )
    assert _badge(payload, "calendar")["tier"] == "unknown"


@pytest.mark.asyncio
async def test_llm_records_ok_and_error_round_trip():
    monitor = HealthMonitor()
    # Initially unknown.
    snap = await monitor.llm_snapshot()
    assert snap["tier"] == "unknown"
    # OK round-trip.
    await monitor.record_llm_ok(model="x-ai/grok-4-fast")
    snap = await monitor.llm_snapshot()
    assert snap["tier"] == "ok"
    assert "openrouter" in snap["message"]
    assert snap["age_seconds"] is not None and snap["age_seconds"] >= 0.0
    # Error round-trip wipes the OK state with a "down" badge.
    await monitor.record_llm_error("rate limited", model="x-ai/grok-4-fast")
    snap = await monitor.llm_snapshot()
    assert snap["tier"] == "down"
    assert "rate limited" in snap["message"]


@pytest.mark.asyncio
async def test_llm_error_message_truncated_and_single_line():
    monitor = HealthMonitor()
    await monitor.record_llm_error("\n".join(["line one", "line two"]) + " " + "x" * 500)
    snap = await monitor.llm_snapshot()
    # No newlines, capped length.
    assert "\n" not in snap["message"]
    assert len(snap["message"]) <= 160


@pytest.mark.asyncio
async def test_overall_tier_picks_worst():
    monitor = HealthMonitor()
    await monitor.record_llm_error("nope")  # llm = down
    payload = await monitor.compose(
        tasks_store=_FakeStore(last_io_at=time.monotonic()),  # ok
        habits_store=None,
        weather=_FakeWeather(last_fetch_at=time.monotonic()),  # ok
        calendar_status={"available": True, "error": None, "write_calendar": "lifemax"},
    )
    assert payload["tier"] == "down"
