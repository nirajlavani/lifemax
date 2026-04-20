"""In-memory dispatch history with reversible-action records.

The dashboard is display-only, but commands flow in from Telegram + the CLI
all day. Without feedback the user has no way to confirm "did that take?"
without scanning the kanban. We keep a small ring buffer of recent
dispatches in memory and expose the most recent few via the SSE snapshot.

Each entry can also carry an `undo_payload` describing how to reverse the
side effect. `lifemax undo` (or any natural-language undo intent) pops the
most recent reversible entry and runs the inverse action.

This module is intentionally storage-free — restarting the server clears
history. That's the right default for a personal Mac mini setup.
"""

from __future__ import annotations

import asyncio
import re as _re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

# Snapshot exposes a trimmed copy. Server keeps a slightly bigger ring so
# undo can still reach back through a few non-reversible queries.
_RING_CAP = 20
_SNAPSHOT_VISIBLE = 6


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DispatchHistory:
    """Async-safe ring buffer of recent dispatch outcomes."""

    def __init__(self, *, capacity: int = _RING_CAP) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    async def push(self, entry: dict[str, Any]) -> dict[str, Any]:
        # Accept both `input_text` (preferred) and legacy `input` for the raw
        # message text, so callers can use either name without surprise.
        input_text = entry.get("input_text", entry.get("input"))
        record = {
            "id": uuid.uuid4().hex,
            "ts": entry.get("ts") or _now_iso(),
            "monotonic": time.monotonic(),
            "action": entry.get("action") or "?",
            "ok": bool(entry.get("ok")),
            "undid": bool(entry.get("undid")),
            "input_text": _truncate(input_text, 240),
            "subject": _truncate(entry.get("subject"), 120),
            "message": _truncate(entry.get("message"), 200),
            "undo_payload": entry.get("undo_payload"),
        }
        async with self._lock:
            self._buf.append(record)
        return record

    async def pop_reversible(self) -> dict[str, Any] | None:
        async with self._lock:
            for i in range(len(self._buf) - 1, -1, -1):
                rec = self._buf[i]
                if rec.get("undo_payload") and rec.get("ok") and not rec.get("undid"):
                    del self._buf[i]
                    return rec
        return None

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = list(self._buf)
        # newest first, trimmed for UI
        items.reverse()
        now = time.monotonic()
        return [_strip_for_snapshot(r, now=now) for r in items[:_SNAPSHOT_VISIBLE]]

    async def clear(self) -> None:
        async with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


def _truncate(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    return f"{cut}\u2026"


def _strip_for_snapshot(rec: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    elapsed_ref = now if now is not None else time.monotonic()
    age = max(0.0, elapsed_ref - float(rec.get("monotonic") or elapsed_ref))
    payload: dict[str, Any] | None = rec.get("undo_payload") if isinstance(rec.get("undo_payload"), dict) else None
    return {
        "id": rec["id"],
        "ts": rec["ts"],
        "age_seconds": round(age, 1),
        "action": rec["action"],
        "ok": rec["ok"],
        "undid": bool(rec.get("undid")),
        "input_text": rec.get("input_text", ""),
        "subject": rec.get("subject", ""),
        "message": rec["message"],
        "reversible": bool(payload),
        # Strip down the payload before sending to the browser so we don't
        # leak internal IDs more than necessary. The presence of any payload
        # is enough for the UI to show an "undo" hint.
        "undo_payload": ({"kind": payload.get("kind", "")} if payload else None),
    }


# ---------------------------------------------------------------------------
# Helpers used by the dispatch endpoints + bot to short-circuit literal undo.
# Keeping these here means we don't have to thread regex/strings through callers.
# ---------------------------------------------------------------------------
_UNDO_KEYWORDS: frozenset[str] = frozenset({"undo", "revert", "oops", "rollback"})


def is_literal_undo(text: str) -> bool:
    cleaned = (text or "").strip().lower().rstrip(".!?,")
    return cleaned in _UNDO_KEYWORDS


def all_undo_keywords() -> Iterable[str]:
    return tuple(sorted(_UNDO_KEYWORDS))


# ---------------------------------------------------------------------------
# Literal "timer" verbs the API + Telegram bot can dispatch without an LLM
# round-trip. The model still owns nuanced phrasing ("focus on deep work for
# 50"); this only catches the obvious cases.
# ---------------------------------------------------------------------------

_TIMER_OP_PATTERNS: tuple[tuple[_re.Pattern[str], str], ...] = (
    (_re.compile(r"^(?:start|begin)\s+(?:a\s+)?(?:pomodoro|timer|focus(?:\s+block)?)$"), "start"),
    (_re.compile(r"^(?:start|begin)\s+(?:a\s+)?focus$"), "start"),
    (_re.compile(r"^pomodoro$"), "start"),
    (_re.compile(r"^timer\s+start$"), "start"),
    (_re.compile(r"^stop\s+(?:the\s+)?(?:pomodoro|timer|focus(?:\s+block)?)$"), "stop"),
    (_re.compile(r"^timer\s+stop$"), "stop"),
    (_re.compile(r"^cancel\s+(?:the\s+)?(?:pomodoro|timer|focus(?:\s+block)?)$"), "stop"),
    (_re.compile(r"^pause\s+(?:the\s+)?(?:pomodoro|timer|focus(?:\s+block)?)$"), "pause"),
    (_re.compile(r"^resume\s+(?:the\s+)?(?:pomodoro|timer|focus(?:\s+block)?)$"), "resume"),
    (_re.compile(r"^(?:take\s+a\s+)?break$"), "break"),
    (_re.compile(r"^short\s+break$"), "break"),
)

_TIMER_DURATION = _re.compile(
    r"""(?ix)
    ^(?P<verb>start|begin|focus|pomodoro|break|extend)
    \s+(?:for\s+|by\s+|a\s+)?
    (?P<n>\d{1,3})
    \s*(?:m|min|mins|minute|minutes)?$
    """
)

_TIMER_VERB_TO_OP: dict[str, str] = {
    "start": "start",
    "begin": "start",
    "focus": "start",
    "pomodoro": "start",
    "break": "break",
    "extend": "extend",
}


def parse_literal_timer(text: str) -> dict[str, Any] | None:
    """Try to map an obvious timer phrase to a `Timer` op + minutes.

    Returns a dict suitable for `Intent(timer=TimerFields(**ret))` when
    the input is a plain verb. Otherwise returns `None` so the caller
    falls back to the LLM router.
    """

    cleaned = (text or "").strip().lower().rstrip(".!?,")
    if not cleaned:
        return None
    # Plain verbs first.
    for pattern, op in _TIMER_OP_PATTERNS:
        if pattern.match(cleaned):
            return {"op": op, "minutes": None, "label": None}
    # `start 25`, `pomodoro 50`, `extend 5`, `break 10`...
    match = _TIMER_DURATION.match(cleaned)
    if match:
        verb = match.group("verb").lower()
        op = _TIMER_VERB_TO_OP.get(verb)
        if op is None:
            return None
        try:
            minutes = int(match.group("n"))
        except ValueError:
            return None
        if minutes <= 0 or minutes > 240:
            return None
        return {"op": op, "minutes": minutes, "label": None}
    return None
