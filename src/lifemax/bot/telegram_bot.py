"""Telegram bot (aiogram v3, long polling) with strict allowlist.

Any message from a non-allowlisted user is dropped silently. Allowed messages
are routed through the OpenRouter intent parser and applied to the task store.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from ..config import get_settings
from ..dispatch_history import DispatchHistory, is_literal_undo, parse_literal_timer
from ..habits_store import HabitStore
from ..intents import IntentResult, apply_intent
from ..llm import LLMRouter, OpenRouterError
from ..models import Intent, Status, TimerFields
from ..store import TaskStore
from ..widgets.calendar_apple import AppleCalendarWidget
from ..widgets.focus_timer import FocusTimer
from ..widgets.health import HealthMonitor
from ..widgets.news_x import NewsWidget
from ..widgets.time_utils import format_today_summary, today_in_tz

logger = logging.getLogger(__name__)


def _hash_user_id(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:10]


class _AllowlistMiddleware(BaseMiddleware):
    """Drop any update whose `from_user.id` is not the configured user."""

    def __init__(self, allowed_user_id: int) -> None:
        self._allowed = allowed_user_id

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        user = data.get("event_from_user")
        if user is None and isinstance(event, Message):
            user = event.from_user
        if user is None or user.id != self._allowed:
            if user is not None:
                logger.info(
                    "telegram update dropped (user_hash=%s)", _hash_user_id(user.id)
                )
            return None
        return await handler(event, data)


def _format_tasks_brief(tasks) -> str:
    if not tasks:
        return "Nothing on the board."
    lines = []
    for t in tasks[:20]:
        prio = (t.priority.value if hasattr(t.priority, "value") else t.priority).upper()
        urg = "!" if (t.urgency.value if hasattr(t.urgency, "value") else t.urgency) == "urgent" else "·"
        deadline = f" — {t.deadline}" if t.deadline else ""
        lines.append(f"{urg} [{prio}] {t.title}{deadline}")
    return "\n".join(lines)


def build_bot(
    store: TaskStore,
    llm: LLMRouter,
    *,
    calendar: AppleCalendarWidget | None = None,
    habits: HabitStore | None = None,
    history: DispatchHistory | None = None,
    focus_timer: FocusTimer | None = None,
    health: HealthMonitor | None = None,
    news: NewsWidget | None = None,
) -> tuple[Bot, Dispatcher]:
    """Construct an aiogram Bot + Dispatcher wired to our store and LLM."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    if not settings.telegram_user_id:
        raise RuntimeError("TELEGRAM_USER_ID is not configured.")

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    middleware = _AllowlistMiddleware(settings.telegram_user_id)
    dp.message.middleware(middleware)

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "lifemax bot online.\n"
            "Send any message to add/update/complete/archive a task or ask a question.\n"
            "/today · today's tasks\n/list · all open tasks\n/help · this message"
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "Examples:\n"
            "  add deep-work block tomorrow morning, important\n"
            "  mark gym task done\n"
            "  archive groceries\n"
            "  what are today's goals?\n\n"
            "/today · today's tasks\n/list · all open tasks"
        )

    @dp.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        await _route_text(message, "what are today's goals?")

    @dp.message(Command("undo"))
    async def cmd_undo(message: Message) -> None:
        await _route_text(message, "undo")

    @dp.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        tasks = await store.all_tasks(include_archived=False)
        open_tasks = [t for t in tasks if t.status != Status.DONE]
        await message.answer(_format_tasks_brief(open_tasks))

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        await _route_text(message, message.text or "")

    async def _route_text(message: Message, text: str) -> None:
        text = (text or "").strip()
        if not text:
            await message.answer("Send some text — e.g. 'add gym at 5pm urgent'.")
            return
        if len(text) > 1500:
            await message.answer("That's too long. Keep it under 1500 characters.")
            return
        timer_literal = parse_literal_timer(text)
        if is_literal_undo(text):
            intent = Intent(action="undo")
        elif timer_literal is not None:
            intent = Intent(action="timer", timer=TimerFields(**timer_literal))
        else:
            try:
                open_tasks = await store.all_tasks(include_archived=False)
                current_habits = (
                    await habits.all_habits(include_archived=False)
                    if habits is not None
                    else None
                )
                today_anchor = (
                    f"{format_today_summary(today_in_tz(settings.timezone))} "
                    f"({settings.timezone})"
                )
                intent = await llm.parse_intent(
                    text,
                    open_tasks,
                    today_anchor=today_anchor,
                    habits=current_habits,
                )
                if health is not None:
                    try:
                        await health.record_llm_ok(model=settings.openrouter_model)
                    except Exception:  # noqa: BLE001
                        pass
            except OpenRouterError as exc:
                if health is not None:
                    try:
                        await health.record_llm_error(
                            str(exc), model=settings.openrouter_model
                        )
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning("telegram parse failed: %s", exc)
                await message.answer("LLM hiccup; try again in a moment.")
                return
        try:
            result: IntentResult = await apply_intent(
                intent,
                store,
                llm,
                timezone_name=settings.timezone,
                calendar=calendar,
                habits=habits,
                history=history,
                focus_timer=focus_timer,
                news=news,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram apply failed: %s", exc)
            await message.answer("Something blew up while applying that.")
            return
        if history is not None:
            try:
                subject: str | None = None
                if result.task is not None:
                    subject = result.task.title
                elif isinstance(result.event, dict):
                    title = result.event.get("title")
                    if isinstance(title, str) and title.strip():
                        subject = title.strip()
                elif result.habit is not None:
                    subject = result.habit.title
                elif isinstance(result.timer, dict):
                    label = result.timer.get("label")
                    phase = result.timer.get("phase")
                    countdown = result.timer.get("countdown")
                    bits: list[str] = []
                    if isinstance(label, str) and label.strip():
                        bits.append(label.strip())
                    elif isinstance(phase, str) and phase:
                        bits.append(phase.replace("_", " "))
                    if isinstance(countdown, str) and countdown:
                        bits.append(countdown)
                    if bits:
                        subject = " · ".join(bits)
                await history.push(
                    {
                        "input_text": text,
                        "action": str(intent.action),
                        "ok": result.ok,
                        "undid": result.undid,
                        "message": result.message,
                        "subject": subject,
                        "undo_payload": (
                            result.undo_payload
                            if (result.ok and not result.undid and result.undo_payload)
                            else None
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram history push failed: %s", exc)
        await message.answer(result.message or ("done" if result.ok else "no-op"))

    return bot, dp


async def run_bot(store: TaskStore, llm: LLMRouter) -> None:
    """Run the bot in long-polling mode until cancelled."""
    bot, dp = build_bot(store, llm)
    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await bot.session.close()
