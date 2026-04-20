"""Curated 'quote of the day' rotator.

Quotes live in `data/quotes.json` (a flat list of `{text, attribution}`
records). The widget never hits the network — keeping the dashboard fully
offline-capable when OpenRouter / RSS feeds are flapping.

Picking is deterministic: a stable hash of `(local_date, slot)` indexes
into the curated list. Same date + slot → same quote across page reloads,
so the dashboard never feels jittery, but each new local day rotates the
selection and there are `QUOTES_PER_DAY` distinct picks within a day for
the on-screen rotator to cycle through.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from ..config import QUOTES_FILE, QUOTES_PER_DAY

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Quote:
    text: str
    attribution: str


_FALLBACK_QUOTES: tuple[_Quote, ...] = (
    _Quote(
        text="We are what we repeatedly do. Excellence, then, is not an act, but a habit.",
        attribution="Will Durant",
    ),
)


def _validate_record(raw: Any) -> _Quote | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    attribution = raw.get("attribution", "")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    if not isinstance(attribution, str):
        attribution = ""
    return _Quote(text=text, attribution=attribution.strip())


def _load_quotes(path: Path) -> tuple[_Quote, ...]:
    """Load + validate the quotes file once. Falls back to a single quote."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("quotes file missing at %s; using fallback", path)
        return _FALLBACK_QUOTES
    except OSError as exc:
        logger.warning("quotes file unreadable (%s); using fallback", exc)
        return _FALLBACK_QUOTES
    except json.JSONDecodeError as exc:
        logger.warning("quotes file is not valid JSON (%s); using fallback", exc)
        return _FALLBACK_QUOTES

    if not isinstance(raw, list):
        logger.warning("quotes file root is not a list; using fallback")
        return _FALLBACK_QUOTES

    cleaned: list[_Quote] = []
    for entry in raw:
        rec = _validate_record(entry)
        if rec is not None:
            cleaned.append(rec)
    if not cleaned:
        logger.warning("quotes file had no valid entries; using fallback")
        return _FALLBACK_QUOTES
    return tuple(cleaned)


def _slot_index(local_date: date_cls, slot: int, total: int) -> int:
    """Deterministic, evenly-distributed index for (date, slot)."""

    if total <= 0:
        return 0
    payload = f"{local_date.isoformat()}|{int(slot)}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    n = int.from_bytes(digest[:8], "big")
    return n % total


class QuoteRotator:
    """Loads the curated quotes list once and serves picks for the snapshot."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or QUOTES_FILE
        self._lock = asyncio.Lock()
        self._quotes: tuple[_Quote, ...] | None = None

    async def _ensure_loaded(self) -> tuple[_Quote, ...]:
        async with self._lock:
            if self._quotes is None:
                self._quotes = await asyncio.to_thread(_load_quotes, self._path)
            return self._quotes

    async def pick_for(
        self,
        local_date: date_cls,
        *,
        slot: int = 0,
    ) -> dict[str, Any]:
        """Return the snapshot-friendly payload for one date + slot.

        The shape mirrors `compute_nudges`: a single dict that can be
        dropped straight into `build_snapshot`.
        """

        quotes = await self._ensure_loaded()
        total = len(quotes)
        # Wrap slot to [0, QUOTES_PER_DAY) so callers can pass any int.
        per_day = max(1, int(QUOTES_PER_DAY))
        slot_norm = int(slot) % per_day
        idx = _slot_index(local_date, slot_norm, total)
        q = quotes[idx]
        return {
            "text": q.text,
            "attribution": q.attribution,
            "slot": slot_norm,
            "slots_per_day": per_day,
            "date_iso": local_date.isoformat(),
            "total": total,
        }
