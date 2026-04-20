"""JSON-backed task store with atomic writes and timestamped backups.

The store is single-user / single-process; concurrent writes from the bot,
the FastAPI dispatch endpoint, and any background tasks are serialised behind
an in-process `asyncio.Lock`. We also use the classic write-temp-then-rename
pattern so the on-disk JSON is never half-written.
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

from .config import BACKUPS_DIR, TASKS_FILE
from .models import Status, Task

ChangeListener = Callable[[], Awaitable[None]]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class TaskStore:
    """Async-safe JSON task store."""

    def __init__(self, path: Path = TASKS_FILE, backup_dir: Path = BACKUPS_DIR) -> None:
        self._path = path
        self._backup_dir = backup_dir
        self._lock = asyncio.Lock()
        self._tasks: dict[str, Task] = {}
        self._loaded = False
        self._listeners: list[ChangeListener] = []
        # Health-vitals bookkeeping. Read by widgets/health.py; nothing in
        # this module branches on these — the existing fall-back behaviour
        # (empty tasks on bad load) is preserved.
        self.last_io_at: float = 0.0  # monotonic clock; set on success
        self.last_load_error: str | None = None
        self.last_save_error: str | None = None

    def add_listener(self, listener: ChangeListener) -> None:
        """Register a coroutine called (no args) after every successful write."""
        self._listeners.append(listener)

    async def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                await listener()
            except Exception:  # noqa: BLE001 - listener errors must not break writes
                pass

    async def load(self) -> None:
        """Load tasks from disk into memory. Safe to call multiple times."""
        async with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if not self._path.exists():
            self._tasks = {}
            self._loaded = True
            self.last_load_error = None
            self.last_io_at = time.monotonic()
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            self._tasks = {}
            self._loaded = True
            self.last_load_error = f"{type(exc).__name__}: {exc}"
            self.last_io_at = time.monotonic()
            return
        items = raw.get("tasks", []) if isinstance(raw, dict) else []
        tasks: dict[str, Task] = {}
        for entry in items:
            try:
                task = Task.model_validate(entry)
            except Exception:  # noqa: BLE001 - skip malformed rows
                continue
            tasks[task.id] = task
        self._tasks = tasks
        self._loaded = True
        self.last_load_error = None
        self.last_io_at = time.monotonic()

    def _save_locked(self) -> None:
        """Write current tasks atomically and drop a timestamped backup."""
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "tasks": [t.model_dump(mode="json") for t in self._tasks.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 1) Backup current file (if any) before overwriting.
        if self._path.exists():
            backup_path = self._backup_dir / f"tasks-{_utc_stamp()}.json"
            try:
                shutil.copy2(self._path, backup_path)
            except OSError:
                pass
            self._prune_backups(keep=50)

        # 2) Write to a temp file in the same directory, then atomic rename.
        fd, tmp_path = tempfile.mkstemp(prefix=".tasks-", suffix=".json", dir=self._path.parent)
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
        backups = sorted(self._backup_dir.glob("tasks-*.json"))
        if len(backups) <= keep:
            return
        for old in backups[: len(backups) - keep]:
            try:
                old.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Public mutation API
    # ------------------------------------------------------------------
    async def all_tasks(self, *, include_archived: bool = False) -> list[Task]:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            return [
                t
                for t in self._tasks.values()
                if include_archived or not t.archived
            ]

    async def get(self, task_id: str) -> Task | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            return self._tasks.get(task_id)

    async def upsert(self, task: Task) -> Task:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            task.touch()
            self._tasks[task.id] = task
            self._save_locked()
        await self._notify()
        return task

    async def update(self, task_id: str, **changes) -> Task | None:
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            existing = self._tasks.get(task_id)
            if existing is None:
                return None
            data = existing.model_dump()
            for key, value in changes.items():
                if value is None:
                    continue
                data[key] = value
            updated = Task.model_validate(data)
            updated.touch()
            self._tasks[task_id] = updated
            self._save_locked()
        await self._notify()
        return updated

    async def complete(self, task_id: str) -> Task | None:
        return await self.update(task_id, status=Status.DONE.value)

    async def archive(self, task_id: str) -> Task | None:
        return await self.update(task_id, archived=True)

    async def find_by_title(self, title_fragment: str) -> Task | None:
        """Best-effort case-insensitive substring lookup."""
        needle = title_fragment.strip().lower()
        if not needle:
            return None
        async with self._lock:
            if not self._loaded:
                self._load_locked()
            candidates = [t for t in self._tasks.values() if not t.archived]
        # 1) exact title match wins
        for t in candidates:
            if t.title.strip().lower() == needle:
                return t
        # 2) substring match
        for t in candidates:
            if needle in t.title.lower():
                return t
        return None
