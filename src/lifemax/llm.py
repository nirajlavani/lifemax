"""OpenRouter client used to parse user messages into structured intents.

We send the user's free-form text plus a compact snapshot of currently open
tasks. The model returns a strict JSON-schema-conformant `Intent` object.
A second small call is used for natural-language replies to queries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

import httpx
from pydantic import ValidationError

from .config import get_settings
from .models import INTENT_JSON_SCHEMA, Habit, Intent, Task

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CompactTask:
    id: str
    title: str
    status: str
    priority: str
    urgency: str
    deadline: str | None


def _compact(tasks: Iterable[Task]) -> list[dict]:
    rows: list[dict] = []
    for t in tasks:
        rows.append(
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value if hasattr(t.status, "value") else t.status,
                "priority": t.priority.value if hasattr(t.priority, "value") else t.priority,
                "urgency": t.urgency.value if hasattr(t.urgency, "value") else t.urgency,
                "deadline": t.deadline,
            }
        )
    return rows


def _compact_news(items: Iterable[dict] | None, *, limit: int = 30) -> list[dict]:
    """Compact news items down to just the fields the reply LLM needs.

    Crucial for the URL grounding guardrail: the model can only echo back
    URLs that appear in this snapshot. We deliberately strip image, score,
    and timestamp fields the model doesn't need so the prompt stays small
    and focused.
    """
    if not items:
        return []
    rows: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        url = raw.get("link") or raw.get("url")
        title = raw.get("title")
        if not url or not title:
            continue
        rows.append(
            {
                "title": str(title).strip(),
                "source": str(raw.get("source") or "").strip() or None,
                "description": (str(raw.get("description") or "").strip() or None),
                "url": str(url).strip(),
            }
        )
        if len(rows) >= limit:
            break
    return rows


_INTENT_SYSTEM_PROMPT = (
    "You are the input router for a personal task + calendar + daily-checklist dashboard. The user "
    "sends one short message; convert it into ONE of these actions: create, update, complete, "
    "archive, query, add_event, add_habit, check_habit, uncheck_habit, remove_habit, undo, timer. Rules:\n"
    "- 'query' = the user is asking a question (e.g. 'what are today's goals?'). Put the question in "
    "  'query'. Do not invent a task, event, or habit.\n"
    "- 'create' = brand-new task. Fill 'fields' with what they said. Leave 'task_id' and "
    "  'task_match_title' null.\n"
    "- 'add_event' = put a NEW Apple Calendar event on the schedule (single-day or multi-day). "
    "  Use this when the user says things like 'add to calendar', 'schedule', 'put on my calendar', "
    "  'block out', or 'I have <event> on <date>'. Fill 'event' (title/start/end/all_day/notes/location). "
    "  Leave 'fields' fully null.\n"
    "  - If the user gives a single time, set end = start + 1 hour.\n"
    "  - For multi-day events, set 'start' to the first day and 'end' to the last day; if the user "
    "    says 'all day', set all_day=true. For all_day multi-day events, dates may be 'YYYY-MM-DD'.\n"
    "  - Resolve relative dates ('tomorrow', 'next Friday') against the 'Today is …' anchor below.\n"
    "  - Always include the user's timezone offset in datetimes (e.g. '-04:00').\n"
    "- DAILY CHECKLIST (separate from kanban tasks!): items recur every day and reset overnight. Use\n"
    "  these when the user says 'daily', 'every day', 'habit', 'routine', 'check off X', 'mark X done\n"
    "  for today', 'undo X', 'remove X from my daily list'.\n"
    "  - 'add_habit' for new daily items. Put the wording in 'habit.title'. Leave 'fields' null.\n"
    "  - 'check_habit' to mark an existing daily item done for today.\n"
    "  - 'uncheck_habit' to undo today's check.\n"
    "  - 'remove_habit' to drop the item from the daily list entirely.\n"
    "  - For check/uncheck/remove, put the user's spoken name of the item in 'habit.match_title'.\n"
    "  - Do NOT use kanban actions (create/complete/archive) for daily items, and do NOT use\n"
    "    habit actions for one-off tasks.\n"
    "- If the user references an existing task by name, find the best match in the snapshot and "
    "  put its id in 'task_id'. If you can only guess by title, also fill 'task_match_title'.\n"
    "- 'complete' = the user said done/finished/completed (for a kanban task). 'archive' = delete/remove/cancel.\n"
    "- 'undo' = the user wants to reverse the most recent dispatch. Use this when they say "
    "  'undo', 'oops', 'never mind', 'revert', 'rollback', or 'put it back'. Leave 'fields', "
    "  'event', 'habit', and 'timer' fully null. Set 'task_id' and 'task_match_title' to null too.\n"
    "- 'timer' = pomodoro / focus block control. Use when the user says 'pomodoro', 'focus', "
    "  'start a timer', 'start a 50 minute focus block', 'pause the timer', 'resume', "
    "  'take a 5 minute break', 'stop the timer', 'extend by 10 minutes'. Fill 'timer.op' with "
    "  start | stop | pause | resume | break | extend. If a duration is given, set "
    "  'timer.minutes' (positive integer). Optional 'timer.label' for the focus block topic "
    "  (e.g. 'deep work'). Leave fields/event/habit fully null.\n"
    "- 'priority' is high/medium/low. 'urgency' is urgent/non_urgent.\n"
    "- 'deadline' must be ISO 8601 in the user's local time, or null. Do not invent deadlines.\n"
    "- All fields are required by the schema; use null/false for anything unspecified.\n"
)


_REPLY_SYSTEM_PROMPT = (
    "You are the assistant for a personal task + news dashboard. Answer the user's question "
    "briefly and concretely using ONLY the JSON snapshots provided (tasks and, if present, "
    "news items). Use plain text (no markdown, no headings). Keep it under 6 short lines.\n\n"
    "URL / LINK RULES (strict — no exceptions):\n"
    "- You may ONLY return URLs that appear verbatim in the provided news JSON's 'url' field.\n"
    "- NEVER invent, guess, complete, paraphrase, or 'reconstruct' a URL.\n"
    "- NEVER return a URL from your training data, your memory, or a hypothetical web search.\n"
    "- If the user asks for a link / article / source / 'send me the URL' and no news item in "
    "  the snapshot matches their request, reply: 'I don't see that article in the dashboard "
    "  feed.' Then optionally list 1-3 of the closest titles from the snapshot so the user can "
    "  pick one. Do not output any URL in that case.\n"
    "- When you do return a URL, copy it character-for-character from the news JSON."
)


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter returns an unexpected response."""


class LLMRouter:
    """Thin async client around the OpenRouter Chat Completions API."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=30.0))
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        if not self._settings.openrouter_api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured.")
        return {
            "Authorization": f"Bearer {self._settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/nirajlavani/lifemax",
            "X-Title": "Lifemax Dashboard",
        }

    async def parse_intent(
        self,
        user_text: str,
        open_tasks: list[Task],
        *,
        today_anchor: str | None = None,
        habits: list[Habit] | None = None,
    ) -> Intent:
        """Parse a single user message into a structured `Intent`."""
        snapshot = _compact(open_tasks)
        habit_snapshot = [
            {"id": h.id, "title": h.title} for h in (habits or []) if not h.archived
        ]
        anchor_line = (
            f"Today is {today_anchor}. Resolve relative dates against this.\n\n"
            if today_anchor
            else ""
        )
        body = {
            "model": self._settings.openrouter_model,
            "messages": [
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"{anchor_line}"
                        f"Open tasks JSON snapshot:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n"
                        f"Daily checklist JSON snapshot:\n{json.dumps(habit_snapshot, ensure_ascii=False)}\n\n"
                        f"User message:\n{user_text}"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "intent",
                    "strict": True,
                    "schema": INTENT_JSON_SCHEMA,
                },
            },
            "temperature": 0.0,
        }
        url = f"{self._settings.openrouter_base_url.rstrip('/')}/chat/completions"
        try:
            resp = await self._client.post(url, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenRouterError(f"non-JSON response: {resp.text[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise OpenRouterError(f"OpenRouter error: {err_msg}")
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, ValueError) as exc:
            raise OpenRouterError(
                f"unexpected payload (no content): {json.dumps(data)[:300]}"
            ) from exc
        try:
            return Intent.model_validate(parsed)
        except ValidationError as exc:
            raise OpenRouterError(f"intent failed validation: {exc}") from exc

    async def answer_query(
        self,
        question: str,
        open_tasks: list[Task],
        *,
        extra_context: str = "",
        news_items: list[dict] | None = None,
    ) -> str:
        """Generate a short natural-language reply to a query.

        ``news_items`` (optional) is the only source of truth for URLs the
        model is allowed to return. The reply system prompt forbids any
        URL that isn't in this list. Pass the cached news widget output
        here so the model can resolve "send me the link to that article"
        questions without hallucinating URLs.
        """
        snapshot = _compact(open_tasks)
        news_snapshot = _compact_news(news_items)
        news_block = (
            f"News JSON (the ONLY URLs you may return):\n"
            f"{json.dumps(news_snapshot, ensure_ascii=False)}\n\n"
            if news_snapshot
            else "News JSON: [] (no news items available — do NOT return any URL)\n\n"
        )
        prompt = (
            f"Tasks JSON:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n"
            f"{news_block}"
            f"{extra_context}\n\nQuestion: {question}".strip()
        )
        body = {
            "model": self._settings.openrouter_model,
            "messages": [
                {"role": "system", "content": _REPLY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        url = f"{self._settings.openrouter_base_url.rstrip('/')}/chat/completions"
        try:
            resp = await self._client.post(url, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenRouterError(f"non-JSON reply: {resp.text[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise OpenRouterError(f"OpenRouter error: {err_msg}")
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise OpenRouterError(
                f"unexpected reply payload: {json.dumps(data)[:300]}"
            ) from exc
