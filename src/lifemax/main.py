"""Process entrypoint: launches FastAPI + Telegram bot in one event loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import uvicorn

from .bot.telegram_bot import build_bot
from .config import get_settings
from .dispatch_history import DispatchHistory
from .habits_store import HabitStore
from .llm import LLMRouter
from .logging_setup import configure_logging
from .server.api import create_app
from .store import TaskStore
from .widgets.calendar_apple import AppleCalendarWidget
from .widgets.focus_timer import FocusTimer
from .widgets.health import HealthMonitor
from .widgets.news_x import NewsWidget
from .widgets.quotes import QuoteRotator
from .widgets.weather import WeatherWidget

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.dispatch_token:
        logger.error("LIFEMAX_DISPATCH_TOKEN is empty; the CLI bridge will be rejected.")
    if not settings.openrouter_api_key:
        logger.error(
            "OPENROUTER_API_KEY is empty; intent parsing will fail until set."
        )

    store = TaskStore()
    weather = WeatherWidget()
    news = NewsWidget()
    llm = LLMRouter()
    calendar = AppleCalendarWidget(timezone_name=settings.timezone)
    habits = HabitStore()
    history = DispatchHistory()
    quotes = QuoteRotator()
    focus_timer = FocusTimer(timezone_name=settings.timezone)
    health = HealthMonitor()

    app = create_app(
        store=store,
        weather=weather,
        news=news,
        llm=llm,
        calendar=calendar,
        habits=habits,
        history=history,
        quotes=quotes,
        focus_timer=focus_timer,
        health=health,
    )

    config = uvicorn.Config(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    bot_task: asyncio.Task | None = None
    bot = None
    if settings.telegram_bot_token and settings.telegram_user_id:
        try:
            bot, dp = build_bot(
                store,
                llm,
                calendar=calendar,
                habits=habits,
                history=history,
                focus_timer=focus_timer,
                health=health,
                news=news,
            )
            bot_task = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=["message"]),
                name="telegram",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram bot failed to start: %s", exc)
            bot_task = None
    else:
        logger.warning(
            "telegram disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID is missing."
        )

    stop_event = asyncio.Event()

    def _request_stop(*_args) -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    tasks = [t for t in (server_task, bot_task) if t is not None]
    stop_waiter = asyncio.create_task(stop_event.wait(), name="stop_waiter")
    try:
        done, _pending = await asyncio.wait(
            [*tasks, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Trigger uvicorn graceful shutdown.
        server.should_exit = True
        if bot_task is not None:
            bot_task.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        await llm.aclose()
        await weather.aclose()
        if not stop_waiter.done():
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_waiter
        # Surface any task exceptions for the operator.
        for t in done:
            if t is stop_waiter:
                continue
            try:
                exc = t.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                continue
            if exc is not None:
                logger.error("%s task crashed: %s", t.get_name(), exc)


def main() -> int:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
