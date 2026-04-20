"""Pytest fixtures: ensure tests run with isolated paths and no real network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the `src/` layout importable.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """Provide a fresh `TaskStore` rooted in a tmp directory."""
    from lifemax import store as store_module

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    tasks_path = tmp_path / "tasks.json"
    return store_module.TaskStore(path=tasks_path, backup_dir=backup_dir)
