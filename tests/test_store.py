"""Tests for the JSON-backed task store: atomic writes + backups + listeners."""

from __future__ import annotations

import json

import pytest

from lifemax.models import Priority, Status, Task, Urgency


@pytest.mark.asyncio
async def test_upsert_persists_and_reloads(tmp_store) -> None:
    task = Task(title="write q2 plan", priority=Priority.HIGH, urgency=Urgency.URGENT)
    await tmp_store.upsert(task)

    fresh = type(tmp_store)(path=tmp_store._path, backup_dir=tmp_store._backup_dir)
    await fresh.load()
    items = await fresh.all_tasks()
    assert len(items) == 1
    assert items[0].title == "write q2 plan"
    assert items[0].priority == Priority.HIGH
    assert items[0].urgency == Urgency.URGENT


@pytest.mark.asyncio
async def test_atomic_write_no_partial_file(tmp_store) -> None:
    """The on-disk file should always be valid JSON, even between writes."""
    for i in range(5):
        await tmp_store.upsert(Task(title=f"t{i}"))
    raw = tmp_store._path.read_text()
    payload = json.loads(raw)
    assert payload["version"] == 1
    assert len(payload["tasks"]) == 5


@pytest.mark.asyncio
async def test_backups_created(tmp_store) -> None:
    await tmp_store.upsert(Task(title="first"))
    await tmp_store.upsert(Task(title="second"))
    backups = sorted(tmp_store._backup_dir.glob("tasks-*.json"))
    # First write has no prior file to back up; second write should produce one.
    assert len(backups) >= 1


@pytest.mark.asyncio
async def test_complete_and_archive(tmp_store) -> None:
    task = await tmp_store.upsert(Task(title="gym session"))
    completed = await tmp_store.complete(task.id)
    assert completed is not None and completed.status == Status.DONE

    archived = await tmp_store.archive(task.id)
    assert archived is not None and archived.archived is True

    visible = await tmp_store.all_tasks(include_archived=False)
    assert all(t.id != task.id for t in visible)

    everything = await tmp_store.all_tasks(include_archived=True)
    assert any(t.id == task.id for t in everything)


@pytest.mark.asyncio
async def test_find_by_title(tmp_store) -> None:
    await tmp_store.upsert(Task(title="Morning Run"))
    await tmp_store.upsert(Task(title="Grocery shopping"))
    match = await tmp_store.find_by_title("morning")
    assert match is not None
    assert match.title == "Morning Run"

    no_match = await tmp_store.find_by_title("nonexistent")
    assert no_match is None


@pytest.mark.asyncio
async def test_listener_called_after_write(tmp_store) -> None:
    calls = {"n": 0}

    async def listener() -> None:
        calls["n"] += 1

    tmp_store.add_listener(listener)
    await tmp_store.upsert(Task(title="hi"))
    assert calls["n"] == 1
    await tmp_store.update((await tmp_store.all_tasks())[0].id, title="bye")
    assert calls["n"] == 2
