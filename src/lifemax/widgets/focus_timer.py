"""In-memory pomodoro / focus timer.

The dashboard already enforces a "display-only browser" model: the user
mutates state through the LLM CLI bridge or Telegram. The timer follows the
same pattern — a single in-process state machine the API + bot can drive
through `Intent(action="timer", ...)`.

State machine
-------------

```
            start            ┌───── pause ─────┐
  idle  ─────────────►  running                 │
   ▲                    │   ▲                 paused
   │                    │   │                   │
   │     stop           │   resume              │
   ├──── ◄──────────────┤   │                   │
   │                    │   ▼                   │
   │                    │  running ◄────────────┘
   │                    │
   │       elapsed      ▼
   │  ┌──────────────► break
   │  │                  │
   │  │                  │ start (or elapsed → idle)
   │  │                  ▼
   │  └──────────── running
   │
   └─── stop (always allowed, returns to idle)
```

The model never persists to disk. A process restart resets the timer to
`idle`, which matches the "I'm planning my session right now" mental model.

Time
----

We keep two clocks:

* `started_at_wall` — UTC `datetime`, used for snapshots and the UI.
* `_started_monotonic` — `time.monotonic()`, used for `tick()` accuracy so
  wall-clock skew (NTP sync, sleep / wake) doesn't double-fire transitions.

`remaining_seconds` is computed against the monotonic clock when the timer
is running, and snapped to the value at pause-time when paused.
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ..config import HABIT_DAY_CUTOFF_HOUR


def _local_habit_date(now: datetime, tz_name: str) -> str:
    """Local 'today' ISO with the habit-day 3am cutoff applied.

    Mirrors `widgets.time_utils.habit_day_in_tz` but takes an explicit `now`
    so unit tests can pin the clock.
    """

    local = now.astimezone(ZoneInfo(tz_name))
    if local.hour < HABIT_DAY_CUTOFF_HOUR:
        local = local - timedelta(days=1)
    return local.date().isoformat()


TimerState = Literal["idle", "running", "paused", "break"]
TimerPhase = Literal["focus", "break_short", "break_long"]


DEFAULT_FOCUS_MIN = 25
DEFAULT_BREAK_SHORT_MIN = 5
DEFAULT_BREAK_LONG_MIN = 15
LONG_BREAK_EVERY = 4
MAX_DURATION_MIN = 240
HISTORY_SOURCE_LABEL = "focus block"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _TimerSnapshot:
    """Internal representation. `to_dict()` produces the snapshot payload."""

    state: TimerState = "idle"
    phase: TimerPhase = "focus"
    label: str = ""
    total_seconds: int = 0
    remaining_seconds: int = 0
    started_at: datetime | None = None
    ends_at: datetime | None = None
    paused_remaining: int = 0
    completed_focus_blocks_today: int = 0
    blocks_date_iso: str = ""
    last_event: dict[str, Any] | None = None
    sequence: int = 0  # bumps on every transition so the UI can ack chimes


# Cap how much per-day history we keep in memory. The weekly retro only needs
# the last 7-14 entries, but we keep ~60 to support a future "monthly" rollup
# without redesign and without breaking the "no persistence" rule.
_BLOCK_HISTORY_CAP = 60


def _bounded_minutes(minutes: int | None, default: int) -> int:
    if minutes is None:
        return default
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return default
    if m <= 0:
        return default
    return min(m, MAX_DURATION_MIN)


class FocusTimer:
    """Async-safe focus timer state machine."""

    def __init__(self, *, timezone_name: str) -> None:
        self._tz_name = timezone_name
        self._tz = ZoneInfo(timezone_name)
        self._lock = asyncio.Lock()
        self._snap = _TimerSnapshot()
        self._monotonic_start: float | None = None  # set when running
        # In-memory rollup of completed focus blocks per local "habit day".
        # Backs the weekly retro card. Capped to _BLOCK_HISTORY_CAP entries.
        self._blocks_per_day: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _local_today(self, now: datetime | None = None) -> str:
        ref = now or _utcnow()
        return _local_habit_date(ref, self._tz_name)

    def _record_event(self, kind: str, *, now: datetime, **extra: Any) -> None:
        self._snap.sequence += 1
        self._snap.last_event = {
            "kind": kind,
            "at": now.isoformat(),
            "phase": self._snap.phase,
            "state": self._snap.state,
            "sequence": self._snap.sequence,
            **extra,
        }

    def _reset_block_counter_if_new_day(self, now: datetime) -> None:
        local_iso = self._local_today(now)
        if self._snap.blocks_date_iso != local_iso:
            self._snap.blocks_date_iso = local_iso
            self._snap.completed_focus_blocks_today = 0

    def _phase_for_break(self) -> TimerPhase:
        done = self._snap.completed_focus_blocks_today
        if done > 0 and done % LONG_BREAK_EVERY == 0:
            return "break_long"
        return "break_short"

    # ------------------------------------------------------------------
    # Public API: state transitions
    # ------------------------------------------------------------------
    async def start_focus(
        self,
        *,
        minutes: int | None = None,
        label: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            self._reset_block_counter_if_new_day(ref)
            mins = _bounded_minutes(minutes, DEFAULT_FOCUS_MIN)
            total = mins * 60
            self._snap.state = "running"
            self._snap.phase = "focus"
            self._snap.label = (label or "").strip()
            self._snap.total_seconds = total
            self._snap.remaining_seconds = total
            self._snap.paused_remaining = 0
            self._snap.started_at = ref
            self._snap.ends_at = ref + timedelta(seconds=total)
            self._monotonic_start = _time.monotonic()
            self._record_event("started", now=ref, total_seconds=total)
            return self._to_dict(now=ref)

    async def start_break(
        self,
        *,
        minutes: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            self._reset_block_counter_if_new_day(ref)
            phase = self._phase_for_break()
            default = (
                DEFAULT_BREAK_LONG_MIN if phase == "break_long" else DEFAULT_BREAK_SHORT_MIN
            )
            mins = _bounded_minutes(minutes, default)
            total = mins * 60
            self._snap.state = "break"
            self._snap.phase = phase
            self._snap.label = ""
            self._snap.total_seconds = total
            self._snap.remaining_seconds = total
            self._snap.paused_remaining = 0
            self._snap.started_at = ref
            self._snap.ends_at = ref + timedelta(seconds=total)
            self._monotonic_start = _time.monotonic()
            self._record_event("break_started", now=ref, total_seconds=total)
            return self._to_dict(now=ref)

    async def pause(self, *, now: datetime | None = None) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            if self._snap.state not in ("running", "break"):
                return self._to_dict(now=ref)
            remaining = self._compute_remaining(ref)
            self._snap.paused_remaining = remaining
            self._snap.remaining_seconds = remaining
            self._snap.state = "paused"
            self._monotonic_start = None
            self._record_event("paused", now=ref, remaining_seconds=remaining)
            return self._to_dict(now=ref)

    async def resume(self, *, now: datetime | None = None) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            if self._snap.state != "paused":
                return self._to_dict(now=ref)
            remaining = max(self._snap.paused_remaining, 0)
            if remaining <= 0:
                # Nothing left — finish the block cleanly.
                self._snap.remaining_seconds = 0
                self._snap.state = "idle"
                self._monotonic_start = None
                self._record_event("elapsed", now=ref, remaining_seconds=0)
                return self._to_dict(now=ref)
            target_state: TimerState = "break" if self._snap.phase != "focus" else "running"
            self._snap.state = target_state
            self._snap.remaining_seconds = remaining
            self._snap.started_at = ref
            self._snap.ends_at = ref + timedelta(seconds=remaining)
            self._monotonic_start = _time.monotonic()
            self._snap.paused_remaining = 0
            self._record_event("resumed", now=ref, remaining_seconds=remaining)
            return self._to_dict(now=ref)

    async def extend(
        self,
        *,
        minutes: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            if self._snap.state == "idle":
                return self._to_dict(now=ref)
            extra = _bounded_minutes(minutes, 5) * 60
            new_total = min(self._snap.total_seconds + extra, MAX_DURATION_MIN * 60)
            delta = new_total - self._snap.total_seconds
            if delta <= 0:
                return self._to_dict(now=ref)
            self._snap.total_seconds = new_total
            if self._snap.state == "paused":
                self._snap.paused_remaining += delta
                self._snap.remaining_seconds = self._snap.paused_remaining
            else:
                # Running or break: shift the end forward by `delta`.
                if self._snap.ends_at is not None:
                    self._snap.ends_at = self._snap.ends_at + timedelta(seconds=delta)
                self._snap.remaining_seconds = self._compute_remaining(ref)
            self._record_event("extended", now=ref, added_seconds=delta)
            return self._to_dict(now=ref)

    async def stop(self, *, now: datetime | None = None) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            if self._snap.state == "idle":
                return self._to_dict(now=ref)
            self._snap.state = "idle"
            self._snap.remaining_seconds = 0
            self._snap.paused_remaining = 0
            self._snap.started_at = None
            self._snap.ends_at = None
            self._snap.label = ""
            self._monotonic_start = None
            self._record_event("stopped", now=ref)
            return self._to_dict(now=ref)

    # ------------------------------------------------------------------
    # Snapshot consumed by build_snapshot
    # ------------------------------------------------------------------
    async def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        ref = now or _utcnow()
        async with self._lock:
            self._reset_block_counter_if_new_day(ref)
            self._tick_locked(ref)
            return self._to_dict(now=ref)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _compute_remaining(self, now: datetime) -> int:
        if self._snap.state in ("paused", "idle"):
            return max(self._snap.remaining_seconds, 0)
        if self._snap.ends_at is None:
            return 0
        delta = (self._snap.ends_at - now).total_seconds()
        return max(int(delta), 0)

    def _tick_locked(self, now: datetime) -> None:
        if self._snap.state not in ("running", "break"):
            return
        remaining = self._compute_remaining(now)
        if remaining > 0:
            self._snap.remaining_seconds = remaining
            return
        # The block just elapsed.
        prev_phase = self._snap.phase
        if prev_phase == "focus":
            self._snap.completed_focus_blocks_today += 1
            day_iso = self._local_today(now)
            self._blocks_per_day[day_iso] = self._blocks_per_day.get(day_iso, 0) + 1
            self._evict_block_history()
        self._snap.state = "idle"
        self._snap.remaining_seconds = 0
        self._snap.paused_remaining = 0
        self._snap.ends_at = None
        self._monotonic_start = None
        self._record_event(
            "elapsed",
            now=now,
            remaining_seconds=0,
            previous_phase=prev_phase,
            blocks_today=self._snap.completed_focus_blocks_today,
        )

    def _evict_block_history(self) -> None:
        """Trim the per-day rollup to the most recent `_BLOCK_HISTORY_CAP` days."""
        if len(self._blocks_per_day) <= _BLOCK_HISTORY_CAP:
            return
        # Sort by ISO date string (lexicographic == chronological for YYYY-MM-DD).
        excess = len(self._blocks_per_day) - _BLOCK_HISTORY_CAP
        oldest_keys = sorted(self._blocks_per_day.keys())[:excess]
        for k in oldest_keys:
            self._blocks_per_day.pop(k, None)

    async def focus_blocks_per_day(self, date_isos: list[str]) -> dict[str, int]:
        """Return a `{date_iso: count}` map for the requested dates.

        Missing dates are returned with a count of 0 so the caller doesn't have
        to worry about presence checks. Read-only — does not mutate state.
        """

        async with self._lock:
            return {iso: self._blocks_per_day.get(iso, 0) for iso in date_isos}

    def _to_dict(self, *, now: datetime) -> dict[str, Any]:
        snap = self._snap
        ends_at_iso = snap.ends_at.isoformat() if snap.ends_at else None
        started_iso = snap.started_at.isoformat() if snap.started_at else None
        # Surface a server-side mm:ss for offline / fallback rendering. The
        # browser still ticks its own animation against `ends_at` for smooth
        # second updates between the 2s SSE pushes.
        rem = snap.remaining_seconds if snap.state != "idle" else 0
        mm, ss = divmod(max(rem, 0), 60)
        countdown = f"{mm:02d}:{ss:02d}"
        return {
            "state": snap.state,
            "phase": snap.phase,
            "label": snap.label,
            "total_seconds": snap.total_seconds,
            "remaining_seconds": rem,
            "started_at": started_iso,
            "ends_at": ends_at_iso,
            "completed_focus_blocks_today": snap.completed_focus_blocks_today,
            "long_break_every": LONG_BREAK_EVERY,
            "countdown": countdown,
            "last_event": snap.last_event,
            "sequence": snap.sequence,
        }
