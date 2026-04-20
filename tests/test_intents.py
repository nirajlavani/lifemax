"""Tests for `apply_intent`: applies parsed intents against a real store + fake LLM."""

from __future__ import annotations

import pytest

from lifemax.dispatch_history import DispatchHistory
from lifemax.intents import apply_intent
from lifemax.models import Intent, IntentFields, Priority, Status, Task, Urgency


class _FakeLLM:
    """Stand-in for `LLMRouter.answer_query` so we don't hit the network.

    Captures the kwargs it was called with so tests can assert the
    wiring (e.g. that `news_items` is forwarded from the query path).
    """

    def __init__(self, reply: str = "you have 0 things due today.") -> None:
        self._reply = reply
        self.last_question: str | None = None
        self.last_open_tasks: list | None = None
        self.last_extra_context: str = ""
        self.last_news_items: list | None = None

    async def answer_query(
        self,
        question,
        open_tasks,
        *,
        extra_context: str = "",
        news_items: list | None = None,
    ):
        self.last_question = question
        self.last_open_tasks = list(open_tasks)
        self.last_extra_context = extra_context
        self.last_news_items = list(news_items) if news_items is not None else None
        return self._reply


@pytest.mark.asyncio
async def test_create_intent(tmp_store) -> None:
    intent = Intent(
        action="create",
        fields=IntentFields(title="ship dashboard", priority=Priority.HIGH, urgency=Urgency.URGENT),
    )
    result = await apply_intent(intent, tmp_store, _FakeLLM(), timezone_name="America/New_York")
    assert result.ok is True
    items = await tmp_store.all_tasks()
    assert len(items) == 1
    assert items[0].title == "ship dashboard"
    assert items[0].priority == Priority.HIGH


@pytest.mark.asyncio
async def test_create_requires_title(tmp_store) -> None:
    intent = Intent(action="create", fields=IntentFields(title="   "))
    result = await apply_intent(intent, tmp_store, _FakeLLM(), timezone_name="America/New_York")
    assert result.ok is False


@pytest.mark.asyncio
async def test_update_by_match_title(tmp_store) -> None:
    base = await tmp_store.upsert(Task(title="hit the gym"))
    intent = Intent(
        action="update",
        task_match_title="gym",
        fields=IntentFields(priority=Priority.HIGH, urgency=Urgency.URGENT),
    )
    result = await apply_intent(intent, tmp_store, _FakeLLM(), timezone_name="America/New_York")
    assert result.ok is True
    refreshed = await tmp_store.get(base.id)
    assert refreshed is not None
    assert refreshed.priority == Priority.HIGH
    assert refreshed.urgency == Urgency.URGENT


@pytest.mark.asyncio
async def test_complete_and_archive_by_id(tmp_store) -> None:
    task = await tmp_store.upsert(Task(title="laundry"))

    done = await apply_intent(
        Intent(action="complete", task_id=task.id),
        tmp_store,
        _FakeLLM(),
        timezone_name="America/New_York",
    )
    assert done.ok is True
    refreshed = await tmp_store.get(task.id)
    assert refreshed is not None and refreshed.status == Status.DONE

    archived = await apply_intent(
        Intent(action="archive", task_id=task.id),
        tmp_store,
        _FakeLLM(),
        timezone_name="America/New_York",
    )
    assert archived.ok is True
    refreshed = await tmp_store.get(task.id)
    assert refreshed is not None and refreshed.archived is True


@pytest.mark.asyncio
async def test_query_uses_llm(tmp_store) -> None:
    await tmp_store.upsert(Task(title="email Q2 plan"))
    intent = Intent(action="query", query="what's on my plate today?")
    result = await apply_intent(
        intent, tmp_store, _FakeLLM(reply="just one thing: email Q2 plan."), timezone_name="America/New_York"
    )
    assert result.ok is True
    assert "email Q2 plan" in result.message


@pytest.mark.asyncio
async def test_unknown_target_is_a_polite_no_op(tmp_store) -> None:
    intent = Intent(action="complete", task_match_title="nothing")
    result = await apply_intent(intent, tmp_store, _FakeLLM(), timezone_name="America/New_York")
    assert result.ok is False


@pytest.mark.asyncio
async def test_undo_reverts_create(tmp_store) -> None:
    history = DispatchHistory()

    create_intent = Intent(action="create", fields=IntentFields(title="ship dashboard"))
    create_result = await apply_intent(
        create_intent,
        tmp_store,
        _FakeLLM(),
        timezone_name="America/New_York",
        history=history,
    )
    assert create_result.ok is True
    assert create_result.task is not None
    task_id = create_result.task.id

    # Mirror what the API/Telegram layer would push into history.
    await history.push(
        {
            "input_text": "add ship dashboard",
            "action": "create",
            "ok": True,
            "subject": create_result.task.title,
            "undo_payload": create_result.undo_payload,
        }
    )

    # Now run the literal undo.
    undo_result = await apply_intent(
        Intent(action="undo"),
        tmp_store,
        _FakeLLM(),
        timezone_name="America/New_York",
        history=history,
    )
    assert undo_result.ok is True
    assert undo_result.undid is True
    refreshed = await tmp_store.get(task_id)
    assert refreshed is not None and refreshed.archived is True


@pytest.mark.asyncio
async def test_undo_with_empty_history(tmp_store) -> None:
    history = DispatchHistory()
    result = await apply_intent(
        Intent(action="undo"),
        tmp_store,
        _FakeLLM(),
        timezone_name="America/New_York",
        history=history,
    )
    assert result.ok is False
    # Even no-op undos should be marked as `undid`-flow attempts so the UI
    # can keep them out of the "reversible" pile if the operator scrolls back.
    assert "Nothing to undo" in result.message


# ---------------------------------------------------------------------------
# URL grounding: the query path must forward the news widget's items to the
# LLM as the ONLY source of truth for URLs. This locks in the contract that
# `apply_intent(action='query', news=...)` will not let the model invent
# article URLs from its training data.
# ---------------------------------------------------------------------------


class _FakeNewsWidget:
    """Stub for the NewsWidget used by `apply_intent`'s query branch."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.calls = 0

    async def get(self) -> list[dict]:
        self.calls += 1
        return self._items


@pytest.mark.asyncio
async def test_query_forwards_news_items_to_llm(tmp_store) -> None:
    """`news=...` must be passed through to `LLMRouter.answer_query`."""
    fake_news = _FakeNewsWidget(
        [
            {
                "title": "The 12-Month Window",
                "link": "https://example.com/12-month-window",
                "source": "Example Mag",
                "description": "A piece about leverage.",
                "image": "https://example.com/og.png",
                "score": 1.0,
                "published_ts": 1700000000,
            }
        ]
    )
    llm = _FakeLLM(reply="here's the link: https://example.com/12-month-window")
    intent = Intent(action="query", query="Send me the article link on the 12 month window")
    result = await apply_intent(
        intent,
        tmp_store,
        llm,
        timezone_name="America/New_York",
        news=fake_news,
    )
    assert result.ok is True
    assert fake_news.calls == 1, "query path must fetch news items"
    assert llm.last_news_items is not None, "news_items must be forwarded to answer_query"
    assert len(llm.last_news_items) == 1
    assert llm.last_news_items[0]["link"] == "https://example.com/12-month-window"


@pytest.mark.asyncio
async def test_query_without_news_widget_passes_none(tmp_store) -> None:
    """If no NewsWidget is wired in, news_items stays None (no hallucination risk)."""
    llm = _FakeLLM()
    intent = Intent(action="query", query="what's on my plate?")
    result = await apply_intent(
        intent,
        tmp_store,
        llm,
        timezone_name="America/New_York",
    )
    assert result.ok is True
    assert llm.last_news_items is None


@pytest.mark.asyncio
async def test_query_survives_news_fetch_failure(tmp_store) -> None:
    """A flaky news feed must not break a non-news query."""

    class _BoomNews:
        async def get(self) -> list[dict]:
            raise RuntimeError("rss feed down")

    llm = _FakeLLM(reply="0 things due today.")
    intent = Intent(action="query", query="what's on my plate?")
    result = await apply_intent(
        intent,
        tmp_store,
        llm,
        timezone_name="America/New_York",
        news=_BoomNews(),
    )
    assert result.ok is True
    assert llm.last_news_items is None  # gracefully swallowed
