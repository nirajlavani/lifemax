"""Tests for the LLM intent JSON schema and the OpenRouter client wiring (mocked)."""

from __future__ import annotations

import json

import httpx
import pytest

from lifemax.llm import LLMRouter, OpenRouterError
from lifemax.models import INTENT_JSON_SCHEMA, Intent, Task


_EMPTY_EVENT = {
    "title": None,
    "start": None,
    "end": None,
    "all_day": False,
    "notes": None,
    "location": None,
}

_EMPTY_HABIT = {"title": None, "match_title": None}

_EMPTY_TIMER = {"op": None, "minutes": None, "label": None}


def test_intent_schema_validates_strict_payload() -> None:
    """A well-formed strict payload must round-trip through the model."""
    payload = {
        "action": "create",
        "task_id": None,
        "task_match_title": None,
        "fields": {
            "title": "ship the dashboard",
            "description": None,
            "deadline": None,
            "priority": "high",
            "urgency": "urgent",
            "status": None,
        },
        "event": _EMPTY_EVENT,
        "habit": _EMPTY_HABIT,
        "timer": _EMPTY_TIMER,
        "query": None,
    }
    intent = Intent.model_validate(payload)
    assert intent.action == "create"
    assert intent.fields.title == "ship the dashboard"


def test_intent_schema_validates_add_event_payload() -> None:
    payload = {
        "action": "add_event",
        "task_id": None,
        "task_match_title": None,
        "fields": {
            "title": None,
            "description": None,
            "deadline": None,
            "priority": None,
            "urgency": None,
            "status": None,
        },
        "event": {
            "title": "Dentist",
            "start": "2026-04-25T15:00:00-04:00",
            "end": "2026-04-25T16:00:00-04:00",
            "all_day": False,
            "notes": None,
            "location": None,
        },
        "habit": _EMPTY_HABIT,
        "timer": _EMPTY_TIMER,
        "query": None,
    }
    intent = Intent.model_validate(payload)
    assert intent.action == "add_event"
    assert intent.event.title == "Dentist"
    assert intent.event.start.startswith("2026-04-25")


def test_intent_schema_validates_add_habit_payload() -> None:
    payload = {
        "action": "add_habit",
        "task_id": None,
        "task_match_title": None,
        "fields": {
            "title": None,
            "description": None,
            "deadline": None,
            "priority": None,
            "urgency": None,
            "status": None,
        },
        "event": _EMPTY_EVENT,
        "habit": {"title": "drink 2L water", "match_title": None},
        "timer": _EMPTY_TIMER,
        "query": None,
    }
    intent = Intent.model_validate(payload)
    assert intent.action == "add_habit"
    assert intent.habit.title == "drink 2L water"


def test_intent_schema_top_level_required_fields() -> None:
    required = set(INTENT_JSON_SCHEMA["required"])
    assert required == {
        "action",
        "task_id",
        "task_match_title",
        "fields",
        "event",
        "habit",
        "timer",
        "query",
    }
    assert INTENT_JSON_SCHEMA["additionalProperties"] is False


def test_intent_schema_action_enum() -> None:
    enum = INTENT_JSON_SCHEMA["properties"]["action"]["enum"]
    assert set(enum) == {
        "create",
        "update",
        "complete",
        "archive",
        "query",
        "add_event",
        "add_habit",
        "check_habit",
        "uncheck_habit",
        "remove_habit",
        "undo",
        "timer",
    }


def test_intent_schema_habit_block_required() -> None:
    habit_schema = INTENT_JSON_SCHEMA["properties"]["habit"]
    assert habit_schema["additionalProperties"] is False
    assert set(habit_schema["required"]) == {"title", "match_title"}


def test_intent_schema_timer_block_required() -> None:
    timer_schema = INTENT_JSON_SCHEMA["properties"]["timer"]
    assert timer_schema["additionalProperties"] is False
    assert set(timer_schema["required"]) == {"op", "minutes", "label"}
    op_enum = set(timer_schema["properties"]["op"]["enum"])
    # `None` participates in strict-mode `["string", "null"]` enums.
    assert {"start", "stop", "pause", "resume", "break", "extend"} <= op_enum


@pytest.mark.asyncio
async def test_parse_intent_uses_response_format(monkeypatch) -> None:
    """`parse_intent` must POST a json_schema response_format to OpenRouter."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "create",
                                    "task_id": None,
                                    "task_match_title": None,
                                    "fields": {
                                        "title": "buy milk",
                                        "description": None,
                                        "deadline": None,
                                        "priority": "low",
                                        "urgency": "non_urgent",
                                        "status": None,
                                    },
                                    "event": {
                                        "title": None,
                                        "start": None,
                                        "end": None,
                                        "all_day": False,
                                        "notes": None,
                                        "location": None,
                                    },
                                    "habit": {
                                        "title": None,
                                        "match_title": None,
                                    },
                                    "timer": {
                                        "op": None,
                                        "minutes": None,
                                        "label": None,
                                    },
                                    "query": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890abcdef")
    # Reset cached settings so the test env var takes effect.
    from lifemax import config as cfg

    cfg._settings = None  # type: ignore[attr-defined]

    async with httpx.AsyncClient(transport=transport) as client:
        router = LLMRouter(http_client=client)
        intent = await router.parse_intent("buy milk", [])
    assert intent.action == "create"
    assert intent.fields.title == "buy milk"

    body = captured["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "intent"
    assert captured["auth"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_parse_intent_raises_on_http_error(monkeypatch) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890abcdef")
    from lifemax import config as cfg

    cfg._settings = None  # type: ignore[attr-defined]

    async with httpx.AsyncClient(transport=transport) as client:
        router = LLMRouter(http_client=client)
        with pytest.raises(OpenRouterError):
            await router.parse_intent("hello", [Task(title="x")])


# ---------------------------------------------------------------------------
# News URL grounding guardrails. The reply system prompt forbids the model
# from returning any URL not in the provided news JSON; these tests lock
# that contract in so a future agent can't quietly water it down.
# ---------------------------------------------------------------------------


def test_reply_system_prompt_forbids_invented_urls() -> None:
    """The reply system prompt must explicitly forbid hallucinated URLs."""
    from lifemax.llm import _REPLY_SYSTEM_PROMPT

    text = _REPLY_SYSTEM_PROMPT.lower()
    # Anchor the guardrail to specific phrases. If any of these go missing
    # the prompt has been weakened and the model could start hallucinating.
    assert "url" in text
    assert "only return urls that appear verbatim" in text
    assert "never invent" in text
    assert "training data" in text
    assert "i don't see that article" in text


def test_compact_news_keeps_only_grounding_fields() -> None:
    """`_compact_news` must produce the exact shape passed to the LLM."""
    from lifemax.llm import _compact_news

    raw = [
        {
            "title": "  The 12-Month Window  ",
            "link": "https://example.com/12-month-window",
            "source": "Example Mag",
            "description": "leverage piece",
            "image": "https://example.com/og.png",  # stripped (image not relevant for URL grounding)
            "score": 1.5,                            # stripped
            "published_ts": 1700000000,              # stripped
        },
        {
            # Items missing title or link are dropped entirely so the model
            # never sees half-formed grounding entries.
            "title": "",
            "link": "https://example.com/empty",
        },
        {
            "title": "no link here",
            "link": "",
        },
    ]
    rows = _compact_news(raw)
    assert len(rows) == 1
    assert rows[0] == {
        "title": "The 12-Month Window",
        "source": "Example Mag",
        "description": "leverage piece",
        "url": "https://example.com/12-month-window",
    }


def test_compact_news_handles_none_and_non_dicts() -> None:
    from lifemax.llm import _compact_news

    assert _compact_news(None) == []
    assert _compact_news([]) == []
    # Non-dict entries are silently dropped, never crashed on.
    assert _compact_news(["junk", 123, None]) == []


def test_compact_news_truncates_to_limit() -> None:
    """Limit prevents the prompt from ballooning if the feed has 100+ items."""
    from lifemax.llm import _compact_news

    raw = [
        {"title": f"item {i}", "link": f"https://example.com/{i}"} for i in range(50)
    ]
    rows = _compact_news(raw, limit=5)
    assert len(rows) == 5
    assert rows[-1]["url"] == "https://example.com/4"


@pytest.mark.asyncio
async def test_answer_query_includes_news_block_in_prompt(monkeypatch) -> None:
    """`answer_query(news_items=...)` must serialize a News JSON block."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "https://example.com/article"}},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890abcdef")
    from lifemax import config as cfg

    cfg._settings = None  # type: ignore[attr-defined]

    news = [
        {
            "title": "The 12-Month Window",
            "link": "https://example.com/article",
            "source": "Example",
            "description": "x",
            "image": "https://example.com/og.png",
            "score": 1.0,
            "published_ts": 1700000000,
        }
    ]

    async with httpx.AsyncClient(transport=transport) as client:
        router = LLMRouter(http_client=client)
        reply = await router.answer_query(
            "send me the link",
            [Task(title="any task")],
            news_items=news,
        )

    assert "https://example.com/article" in reply
    user_msg = captured["body"]["messages"][1]["content"]
    # The prompt must contain the News JSON block AND the URL of the item.
    assert "News JSON" in user_msg
    assert "https://example.com/article" in user_msg
    # Excluded fields must NOT appear in the prompt (keeps it small + focused).
    assert "image" not in user_msg.lower()
    assert "published_ts" not in user_msg


@pytest.mark.asyncio
async def test_answer_query_without_news_says_so_in_prompt(monkeypatch) -> None:
    """When no news is available, the prompt must explicitly tell the model
    to NOT return any URL — closes the hallucination loophole."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "no link available"}}]},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567890abcdef")
    from lifemax import config as cfg

    cfg._settings = None  # type: ignore[attr-defined]

    async with httpx.AsyncClient(transport=transport) as client:
        router = LLMRouter(http_client=client)
        await router.answer_query(
            "send me a link",
            [Task(title="x")],
            news_items=None,
        )

    user_msg = captured["body"]["messages"][1]["content"]
    assert "no news items available" in user_msg
    assert "do NOT return any URL" in user_msg
