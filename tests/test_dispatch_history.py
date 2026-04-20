"""Tests for the dispatch history ring buffer + literal-undo helper.

Covers ordering, snapshot fields, reversible-only `pop_reversible`, and the
'undo / oops / revert / rollback' literal short-circuit.
"""

from __future__ import annotations

import pytest

from lifemax.dispatch_history import DispatchHistory, all_undo_keywords, is_literal_undo


@pytest.mark.asyncio
async def test_push_and_snapshot_ordering() -> None:
    h = DispatchHistory()

    await h.push({"input_text": "add laundry", "action": "create", "ok": True, "subject": "laundry"})
    await h.push({"input_text": "complete laundry", "action": "complete", "ok": True, "subject": "laundry"})
    await h.push({"input_text": "what's due today?", "action": "query", "ok": True})

    snap = await h.snapshot()

    # newest first, all three present
    assert [item["action"] for item in snap] == ["query", "complete", "create"]
    # required public fields
    for item in snap:
        assert {"id", "ts", "age_seconds", "action", "ok", "undid", "input_text", "subject", "message", "reversible"} <= item.keys()


@pytest.mark.asyncio
async def test_pop_reversible_skips_non_reversible_and_undone() -> None:
    h = DispatchHistory()

    # Non-reversible (no undo_payload).
    await h.push({"input_text": "what's due today?", "action": "query", "ok": True})
    # Already undone (undid=True) — should be skipped even with payload.
    await h.push(
        {
            "input_text": "previous undo",
            "action": "undo",
            "ok": True,
            "undid": True,
            "undo_payload": {"kind": "task_create", "task_id": "x"},
        }
    )
    # Reversible add_event from earlier.
    await h.push(
        {
            "input_text": "add lunch tomorrow",
            "action": "add_event",
            "ok": True,
            "subject": "lunch",
            "undo_payload": {"kind": "event_create", "event_id": "evt-1", "title": "lunch"},
        }
    )
    # Reversible task_create more recently — should win.
    await h.push(
        {
            "input_text": "add task buy milk",
            "action": "create",
            "ok": True,
            "subject": "buy milk",
            "undo_payload": {"kind": "task_create", "task_id": "abc"},
        }
    )

    popped = await h.pop_reversible()
    assert popped is not None
    assert popped["undo_payload"]["kind"] == "task_create"

    # Next pop returns the older reversible event.
    popped2 = await h.pop_reversible()
    assert popped2 is not None
    assert popped2["undo_payload"]["kind"] == "event_create"

    # Nothing reversible left.
    assert await h.pop_reversible() is None


@pytest.mark.asyncio
async def test_snapshot_strips_internal_undo_payload_fields() -> None:
    h = DispatchHistory()
    await h.push(
        {
            "input_text": "add task",
            "action": "create",
            "ok": True,
            "subject": "task A",
            "undo_payload": {"kind": "task_create", "task_id": "secret-id"},
        }
    )
    snap = await h.snapshot()
    payload = snap[0]["undo_payload"]
    # Only `kind` should leak to the browser.
    assert payload == {"kind": "task_create"}
    assert snap[0]["reversible"] is True


@pytest.mark.parametrize(
    "text",
    [
        "undo",
        "  Undo  ",
        "OOPS",
        "revert",
        "Rollback.",
        "oops!",
    ],
)
def test_is_literal_undo_true(text: str) -> None:
    assert is_literal_undo(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "undo the last gym task",  # extra words → not a literal undo
        "please rollback the schema",
        "I goofed, oops there's another task",
    ],
)
def test_is_literal_undo_false(text: str) -> None:
    assert is_literal_undo(text) is False


def test_undo_keywords_set_is_stable() -> None:
    keywords = list(all_undo_keywords())
    # Sorted, deduplicated
    assert keywords == sorted(set(keywords))
    # Sanity: contains the canonical four
    assert {"undo", "oops", "revert", "rollback"} <= set(keywords)
