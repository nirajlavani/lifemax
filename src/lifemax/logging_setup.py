"""Structured logging with light secret/PII redaction.

We can't ship plaintext OpenRouter keys, Telegram tokens, or raw user ids
into the log files. The redaction filter applies to every record, including
those emitted by third-party libraries (uvicorn, httpx, aiogram).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Iterable

# Patterns to scrub from log messages.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenRouter keys
    re.compile(r"sk-or-[A-Za-z0-9_\-]{16,}"),
    # OpenAI-style keys
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    # Telegram bot tokens look like 12345:AA....
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),
    # Generic Bearer tokens
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    # JWT-ish
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
)

_REDACTED = "<redacted>"


def _scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        scrubbed = _scrub(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, extra_loggers: Iterable[str] = ()) -> None:
    """Install JSON logging on stdout with secret redaction."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RedactFilter())

    root = logging.getLogger()
    # Wipe any prior handlers (uvicorn likes to add its own).
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "aiogram", *extra_loggers):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level.upper())
