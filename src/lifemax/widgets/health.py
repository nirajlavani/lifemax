"""Health vitals badge strip.

Tiny, in-process health monitor that composes per-subsystem reports into a
single ``health`` block. Each subsystem owns its own freshness signal:

- The task and habit stores expose ``last_load_error`` / ``last_save_error``
  + ``last_io_at`` (set by ``_load_locked`` / ``_save_locked``).
- The weather widget exposes ``last_fetch_at`` and ``last_fetch_error``.
- The Apple calendar widget already returns ``get_status()`` (kept as-is).
- The LLM doesn't run on a timer; instead the dispatch handler calls
  ``HealthMonitor.record_llm_ok`` / ``record_llm_error`` after every probe
  so the badge reflects the *last real* round-trip rather than a synthetic
  ping.

The result is a single payload the snapshot ships without growing the macro
grid (the badges live in a small fixed strip top-right of ``#stage``).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

# Tier values are intentionally a closed set so the UI can mechanically
# colour-grade them with palette tokens (``--cream`` / ``--blue`` / ``--red``).
HealthTier = Literal["ok", "degraded", "down", "unknown"]

# Anything older than this stops being "fresh" — the badge falls to
# ``degraded`` even if the last result was a success. Picks up cases where
# the probing loop has stalled silently.
_FRESH_AFTER_SECONDS: dict[str, float] = {
    "store": 5 * 60.0,
    "calendar": 15 * 60.0,
    "weather": 30 * 60.0,
    "llm": 24 * 60 * 60.0,  # llm is on-demand; only flag if the last probe was old AND failed
}


@dataclass(slots=True)
class _LLMProbe:
    """In-memory record of the last LLM round-trip.

    Kept tiny on purpose: the dashboard only needs to answer "did the most
    recent dispatch reach OpenRouter?" and "how long ago was it?".
    """

    tier: HealthTier = "unknown"
    message: str = "no recent calls"
    at_monotonic: float = 0.0
    at_wall: float = 0.0
    model: str | None = None


def _age_seconds(at_monotonic: float, now_monotonic: float) -> float | None:
    if at_monotonic <= 0:
        return None
    return max(0.0, now_monotonic - at_monotonic)


def _coerce_tier(value: Any, *, fallback: HealthTier = "unknown") -> HealthTier:
    if value in ("ok", "degraded", "down", "unknown"):
        return value  # type: ignore[return-value]
    return fallback


def _store_health(
    *,
    label: str,
    last_io_at: float,
    last_load_error: str | None,
    last_save_error: str | None,
    now_monotonic: float,
) -> dict[str, Any]:
    """Compose a store badge from its in-memory error/freshness signals."""

    age = _age_seconds(last_io_at, now_monotonic)
    if last_save_error:
        # A failed write is the worst case — the user's intent silently dropped.
        tier: HealthTier = "down"
        message = f"save failed · {last_save_error}"
    elif last_load_error:
        # Reads can fall back to an empty store, but flag it loudly.
        tier = "degraded"
        message = f"load fallback · {last_load_error}"
    elif age is None:
        tier = "unknown"
        message = "not yet read"
    elif age > _FRESH_AFTER_SECONDS["store"]:
        tier = "degraded"
        message = f"idle · last touched {int(age)}s ago"
    else:
        tier = "ok"
        message = "writes ok"
    return {
        "key": "store",
        "label": label,
        "tier": tier,
        "message": message,
        "age_seconds": age,
    }


def _weather_health(
    *,
    last_fetch_at: float,
    last_fetch_error: str | None,
    now_monotonic: float,
) -> dict[str, Any]:
    age = _age_seconds(last_fetch_at, now_monotonic)
    if last_fetch_error and (age is None or age > _FRESH_AFTER_SECONDS["weather"]):
        tier: HealthTier = "down"
        message = f"open-meteo · {last_fetch_error}"
    elif last_fetch_error:
        tier = "degraded"
        message = f"open-meteo · {last_fetch_error}"
    elif age is None:
        tier = "unknown"
        message = "no fetch yet"
    elif age > _FRESH_AFTER_SECONDS["weather"]:
        tier = "degraded"
        message = f"stale · {int(age // 60)}m old"
    else:
        tier = "ok"
        message = "open-meteo ok"
    return {
        "key": "weather",
        "label": "weather",
        "tier": tier,
        "message": message,
        "age_seconds": age,
    }


def _calendar_health(status: dict[str, Any] | None) -> dict[str, Any]:
    """The calendar widget already returns a dict; map it onto our shape."""

    if status is None:
        return {
            "key": "calendar",
            "label": "calendar",
            "tier": "unknown",
            "message": "no read yet",
            "age_seconds": None,
        }
    available = bool(status.get("available"))
    err = status.get("error")
    if available and not err:
        tier: HealthTier = "ok"
        message = "eventkit ok"
    elif err:
        tier = "down"
        message = f"eventkit · {err}"
    else:
        tier = "degraded"
        message = "permission pending"
    return {
        "key": "calendar",
        "label": "calendar",
        "tier": tier,
        "message": message,
        "age_seconds": None,  # calendar widget doesn't expose timing
    }


class HealthMonitor:
    """Owns the LLM probe and composes the final badge strip payload."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._llm = _LLMProbe()

    # ------------------------------------------------------------------
    # LLM probe — called from the dispatch path so the badge reflects the
    # *last real* request, not a synthetic ping. Cheap, no extra HTTP calls.
    # ------------------------------------------------------------------
    async def record_llm_ok(self, *, model: str | None = None) -> None:
        async with self._lock:
            self._llm = _LLMProbe(
                tier="ok",
                message="openrouter ok" if not model else f"openrouter · {model}",
                at_monotonic=time.monotonic(),
                at_wall=time.time(),
                model=model,
            )

    async def record_llm_error(self, message: str, *, model: str | None = None) -> None:
        # Truncate long error bodies so the tooltip stays readable. Drop
        # newlines so the badge renders on one line.
        safe = (message or "openrouter error").splitlines()[0][:160]
        async with self._lock:
            self._llm = _LLMProbe(
                tier="down",
                message=safe,
                at_monotonic=time.monotonic(),
                at_wall=time.time(),
                model=model,
            )

    async def llm_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            llm = self._llm
        now_mono = time.monotonic()
        age = _age_seconds(llm.at_monotonic, now_mono)
        return {
            "key": "llm",
            "label": "llm",
            "tier": llm.tier,
            "message": llm.message,
            "age_seconds": age,
        }

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------
    async def compose(
        self,
        *,
        tasks_store: Any,
        habits_store: Any | None,
        weather: Any,
        calendar_status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return the unified ``health`` payload for ``build_snapshot``."""

        now_mono = time.monotonic()
        badges: list[dict[str, Any]] = []

        # Tasks store badge — relabel to "store" so the visible row stays
        # short. The message includes "tasks" for the tooltip.
        badges.append(
            _store_health(
                label="tasks",
                last_io_at=getattr(tasks_store, "last_io_at", 0.0),
                last_load_error=getattr(tasks_store, "last_load_error", None),
                last_save_error=getattr(tasks_store, "last_save_error", None),
                now_monotonic=now_mono,
            )
        )
        if habits_store is not None:
            badge = _store_health(
                label="habits",
                last_io_at=getattr(habits_store, "last_io_at", 0.0),
                last_load_error=getattr(habits_store, "last_load_error", None),
                last_save_error=getattr(habits_store, "last_save_error", None),
                now_monotonic=now_mono,
            )
            badge["key"] = "habits"
            badges.append(badge)

        badges.append(
            _weather_health(
                last_fetch_at=getattr(weather, "last_fetch_at", 0.0),
                last_fetch_error=getattr(weather, "last_fetch_error", None),
                now_monotonic=now_mono,
            )
        )

        badges.append(_calendar_health(calendar_status))
        badges.append(await self.llm_snapshot())

        # Top-line summary tier picks the worst report so the strip
        # gives a single-glance verdict (matches the rest of the dashboard's
        # "answer in one breath" voice).
        rank = {"down": 3, "degraded": 2, "unknown": 1, "ok": 0}
        worst: HealthTier = "ok"
        for b in badges:
            t = _coerce_tier(b.get("tier"), fallback="unknown")
            if rank[t] > rank[worst]:
                worst = t

        return {
            "tier": worst,
            "badges": badges,
            "computed_at_wall": time.time(),
        }
