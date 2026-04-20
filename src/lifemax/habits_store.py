"""JSON-backed daily-checklist store.

Mirrors the design of `TaskStore` — single-process / single-user, async lock,
atomic writes, timestamped backups — but for `Habit` records. The on-disk
format only stores each habit's `last_done_local_date` (YYYY-MM-DD); whether
something is "done today" is computed against the current habit-day each time
the snapshot is rendered.

A small starter set of items is seeded on first launch (configurable via
`STARTER_HABITS` in `config.py`) so the UI is never empty.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .config import BACKUPS_DIR, HABITS_FILE, STARTER_HABITS
from .habit_streaks import best_streak as compute_best_streak
from .habit_streaks import normalize_history
from .models import Habit

ChangeListener = Callable[[], Awaitable[None]]

# Keep at most ~2 months of completion history per habit. Plenty to render
# 7-day strips and meaningful streaks; small enough to keep `habits.json` tiny.
_HISTORY_MAX_KEEP = 60


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class HabitStore:
    """Async-safe JSON daily-checklist store."""

    def __init__(
        self,
        path: Path = HABITS_FILE,
        backup_dir: Path = BACKUPS_DIR,
        *,
        seed_starter: bool = True,
    ) -> None:
        self._path = path
        self._backup_dir = backup_dir
        self._lock = asyncio.Lock()
        self._habits: dict[str, Habit] = {}
        self._loaded = False
        self._listeners: list[ChangeListener] = []
        self._seed_starter = seed_starter
        # Health bookkeeping. Read by widgets/health.py; nothing in this
        # module branches on these — failed loads still fall back to an
        # empty store so the dashboard keeps rendering.
        self.last_io_at: float = 0.0
        self.last_load_error: str | None = None
        self.last_save_error: str | None = None

    def add_listener(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    async def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                await listener()
            except Exception:  # noqa: BLE001 - never let a listener kill a write
                pass

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def load(self) -> None:
        async with self._lock:
            self._load_locked()
            if self._seed_starter and not self._habits and not self._path.exists():
                self._seed_locked()
                self._save_locked()
        await self._notify()

    def _load_locked(self) -> None:
        if not self._path.exists():
            self._habits = {}
            self._loaded = True
            self.last_load_error = None
            self.last_io_at = time.monotonic()
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            self._habits = {}
            self._loaded = True
            self.last_load_error = f"{type(exc).__name__}: {exc}"
            self.last_io_at = time.monotonic()
            return
        items = raw.get("habits", []) if isinstance(raw, dict) else []
        habits: dict[str, Habit] = {}
        for entry in items:
            try:
                h = Habit.model_validate(entry)
            except Exception:  # noqa: BLE001 - skip malformed rows
                continue
            # Heal/migrate older rows that may not have history yet but do have
            # a `last_done_local_date`. We seed history with that single entry
            # so existing users see a streak of 1 instead of 0 the day they
            # upgrade.
            history = normalize_history(h.completed_dates, max_keep=_HISTORY_MAX_KEEP)
            if not history and h.last_done_local_date:
                history = normalize_history([h.last_done_local_date], max_keep=_HISTORY_MAX_KEEP)
            h.completed_dates = history
            h.best_streak_cached = max(h.best_streak_cached, compute_best_streak(history))
            habits[h.id] = h
        self._habits = habits
        self._loaded = True
        self.last_load_error = None
        self.last_io_at = time.monotonic()

    def _seed_locked(self) -> None:
        for idx, title in enumerate(STARTER_HABITS):
            h = Habit(title=title, sort_order=idx)
            self._habits[h.id] = h

    def _save_locked(self) -> None:
        ordered = sorted(self._habits.values(), key=lambda h: (h.sort_order, h.created_at))
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "habits": [h.model_dump(mode="json") for h in ordered],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            backup_path = self._backup_dir / f"habits-{_utc_stamp()}.json"
            try:
                shutil.copy2(self._path, backup_path)
            except OSError:
                pass
            self._prune_backups(keep=50)

        fd, tmp_path = tempfile.mkstemp(prefix=".habits-", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.last_save_error = f"{type(exc).__name__}: {exc}"
            self.last_io_at = time.monotonic()
            raise
        else:
            self.last_save_error = None
            self.last_io_at = time.monotonic()

    def _prune_backups(self, keep: int) -> None:
        backups = sorted(self._backup_dir.glob("habits-*.json"))
        if len(backups) <= keep:
            return
        for old in backups[: len(backups) - keep]:
            try:
                old.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------
    async def all_habits(self, *, include_archived: bool = False) -> list[Habit]:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            items = [h for h in self._habits.values() if include_archived or not h.archived]
        items.sort(key=lambda h: (h.sort_order, h.created_at))
        return items

    async def get(self, habit_id: str) -> Habit | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            return self._habits.get(habit_id)

    async def find_by_title(self, fragment: str) -> Habit | None:
        """Case-insensitive lookup. Exact match first, then substring."""
        needle = (fragment or "").strip().lower()
        if not needle:
            return None
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            candidates = [h for h in self._habits.values() if not h.archived]
        for h in candidates:
            if h.title.strip().lower() == needle:
                return h
        for h in candidates:
            if needle in h.title.lower():
                return h
        return None

    # ------------------------------------------------------------------
    # Public mutations
    # ------------------------------------------------------------------
    async def add(self, title: str) -> Habit:
        title = title.strip()
        if not title:
            raise ValueError("habit title cannot be empty")
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            next_order = max((h.sort_order for h in self._habits.values()), default=-1) + 1
            habit = Habit(title=title, sort_order=next_order)
            self._habits[habit.id] = habit
            self._save_locked()
        await self._notify()
        return habit

    async def remove(self, habit_id: str) -> Habit | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            existing = self._habits.pop(habit_id, None)
            if existing is None:
                return None
            self._save_locked()
        await self._notify()
        return existing

    async def mark_done(self, habit_id: str, *, local_date_iso: str) -> Habit | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            existing = self._habits.get(habit_id)
            if existing is None:
                return None
            already_in_history = local_date_iso in existing.completed_dates
            if existing.last_done_local_date == local_date_iso and already_in_history:
                return existing  # idempotent
            existing.last_done_local_date = local_date_iso
            if not already_in_history:
                existing.completed_dates = normalize_history(
                    [*existing.completed_dates, local_date_iso],
                    max_keep=_HISTORY_MAX_KEEP,
                )
                existing.best_streak_cached = max(
                    existing.best_streak_cached,
                    compute_best_streak(existing.completed_dates),
                )
            existing.touch()
            self._save_locked()
        await self._notify()
        return existing

    async def undo_done(self, habit_id: str, *, local_date_iso: str) -> Habit | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            existing = self._habits.get(habit_id)
            if existing is None:
                return None
            # Only clear if it actually matches today (don't wipe unrelated state).
            if existing.last_done_local_date != local_date_iso:
                return existing
            existing.last_done_local_date = None
            if local_date_iso in existing.completed_dates:
                existing.completed_dates = [
                    d for d in existing.completed_dates if d != local_date_iso
                ]
            existing.touch()
            self._save_locked()
        await self._notify()
        return existing
