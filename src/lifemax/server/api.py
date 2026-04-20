"""FastAPI app: serves the static UI and a localhost-only dispatch endpoint.

Live updates are pushed to the browser over Server-Sent Events. The page is
display-only, so SSE is the right fit — there's no client-to-server traffic
beyond the connection itself.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import HABIT_DAY_CUTOFF_HOUR, STATIC_DIR, get_settings
from ..dispatch_history import (
    DispatchHistory,
    is_literal_undo,
    parse_literal_timer,
)
from ..habit_streaks import current_streak, done_last_7
from ..habits_store import HabitStore
from ..intents import IntentResult, apply_intent
from ..llm import LLMRouter, OpenRouterError
from ..models import Intent, TimerFields
from ..store import TaskStore
from ..widgets.calendar_apple import AppleCalendarWidget
from ..widgets.focus import pick_focus
from ..widgets.focus_timer import FocusTimer
from ..widgets.health import HealthMonitor
from ..widgets.news_x import NewsWidget
from ..widgets.nudges import compute_nudges
from ..widgets.quotes import QuoteRotator
from ..widgets.retro import (
    compute_weekly_retro,
    date_range_iso,
    local_habit_date_for,
)
from ..widgets.time_utils import (
    format_clock,
    format_today_summary,
    habit_day_in_tz,
    is_due_today,
    now_in_tz,
    today_in_tz,
)
from ..widgets.weather import WeatherWidget

logger = logging.getLogger(__name__)

_MAX_DISPATCH_BODY_BYTES = 4 * 1024  # 4 KB cap on request body
_DISPATCH_RATE_LIMIT_PER_MIN = 30


# ---------------------------------------------------------------------------
# In-process publish/subscribe for SSE
# ---------------------------------------------------------------------------
class _StateBus:
    """Tiny in-process pub/sub used by the SSE stream."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    async def publish(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest, keep the freshest.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass


# ---------------------------------------------------------------------------
# Rate limit (per-process, sliding 60s window)
# ---------------------------------------------------------------------------
class _SimpleRateLimit:
    def __init__(self, limit: int, window_s: float) -> None:
        self._limit = limit
        self._window = window_s
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def hit(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            while self._events and (now - self._events[0]) > self._window:
                self._events.popleft()
            if len(self._events) >= self._limit:
                return False
            self._events.append(now)
            return True


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class DispatchRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _result_subject(result: IntentResult) -> str | None:
    """Pick a short human label for the affected entity (task/event/habit/timer)."""
    if result.task is not None:
        return result.task.title
    if isinstance(result.event, dict):
        title = result.event.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    if result.habit is not None:
        return result.habit.title
    if isinstance(result.timer, dict):
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
        return " · ".join(bits) if bits else None
    return None


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------
async def build_snapshot(
    store: TaskStore,
    weather: WeatherWidget,
    news: NewsWidget,
    calendar: AppleCalendarWidget | None,
    habits: HabitStore | None,
    timezone_name: str,
    history: DispatchHistory | None = None,
    quotes: QuoteRotator | None = None,
    focus_timer: FocusTimer | None = None,
    health: HealthMonitor | None = None,
) -> dict[str, Any]:
    tasks = await store.all_tasks(include_archived=False)
    now = now_in_tz(timezone_name)
    today_count = sum(1 for t in tasks if is_due_today(t.deadline, timezone_name))
    events: list[dict[str, Any]] = []
    cal_status: dict[str, Any] | None = None
    if calendar is not None:
        events = await calendar.get_events()
        cal_status = await calendar.get_status()
    habits_payload: dict[str, Any] = {
        "items": [],
        "today_local_date": None,
        "cutoff_hour": HABIT_DAY_CUTOFF_HOUR,
        "done_today": 0,
        "total": 0,
    }
    if habits is not None:
        items = await habits.all_habits(include_archived=False)
        today_iso = habit_day_in_tz(
            timezone_name, cutoff_hour=HABIT_DAY_CUTOFF_HOUR
        ).isoformat()
        rows = []
        done_today = 0
        top_streak = 0
        top_streak_title: str | None = None
        for h in items:
            done = h.is_done_for(today_iso)
            if done:
                done_today += 1
            streak = current_streak(h.completed_dates, today_iso=today_iso)
            best = max(h.best_streak_cached, streak)
            if streak > top_streak:
                top_streak = streak
                top_streak_title = h.title
            data = h.model_dump(mode="json")
            data["done_today"] = done
            data["current_streak"] = streak
            data["best_streak"] = best
            data["done_last_7"] = done_last_7(
                h.completed_dates, today_iso=today_iso
            )
            rows.append(data)
        habits_payload = {
            "items": rows,
            "today_local_date": today_iso,
            "cutoff_hour": HABIT_DAY_CUTOFF_HOUR,
            "done_today": done_today,
            "total": len(rows),
            "top_streak": top_streak,
            "top_streak_title": top_streak_title,
        }
    focus = pick_focus(tasks, timezone_name=timezone_name, now=now)
    nudges = compute_nudges(tasks, timezone_name=timezone_name, now=now)
    task_tiers: dict[str, str] = nudges.get("task_tiers", {})
    task_payload: list[dict[str, Any]] = []
    for t in tasks:
        data = t.model_dump(mode="json")
        data["tier"] = task_tiers.get(t.id, "none")
        task_payload.append(data)
    history_items: list[dict[str, Any]] = []
    if history is not None:
        history_items = await history.snapshot()
    quote_payload: dict[str, Any] | None = None
    if quotes is not None:
        # Slot the day into 4 even windows (6h each) so the quote rotates a
        # few times through the day without ever feeling jittery.
        slot = now.hour // 6
        quote_payload = await quotes.pick_for(now.date(), slot=slot)
    timer_payload: dict[str, Any] | None = None
    if focus_timer is not None:
        timer_payload = await focus_timer.snapshot(now=now)
    retro_payload: dict[str, Any] | None = None
    if habits is not None:
        # We always have tasks; habits are the one optional store. Without
        # both, the weekly rollup is partial enough that we'd rather omit it
        # than render a misleading card.
        habit_models = await habits.all_habits(include_archived=False)
        retro_today_iso = local_habit_date_for(now, timezone_name)
        # 14 days covers the current week + a stable "previous best day" lookup.
        retro_window = date_range_iso(retro_today_iso, days=14)
        focus_per_day: dict[str, int] = {}
        if focus_timer is not None:
            focus_per_day = await focus_timer.focus_blocks_per_day(retro_window)
        retro_payload = compute_weekly_retro(
            tasks=tasks,
            habits=habit_models,
            focus_blocks_per_day=focus_per_day,
            timezone_name=timezone_name,
            now=now,
        )
    weather_payload = await weather.get()
    health_payload: dict[str, Any] | None = None
    if health is not None:
        # Health is composed *after* the weather/calendar reads above so the
        # badge picks up the freshness signals from this very snapshot.
        health_payload = await health.compose(
            tasks_store=store,
            habits_store=habits,
            weather=weather,
            calendar_status=cal_status,
        )
    return {
        "tasks": task_payload,
        "weather": weather_payload,
        "clock": format_clock(now),
        "today_count": today_count,
        "news": await news.get(),
        "events": events,
        "calendar_status": cal_status,
        "habits": habits_payload,
        "focus": focus,
        "nudges": nudges,
        "quote": quote_payload,
        "timer": timer_payload,
        "retro": retro_payload,
        "health": health_payload,
        "history": {"items": history_items},
        "generated_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(
    *,
    store: TaskStore,
    weather: WeatherWidget,
    news: NewsWidget,
    llm: LLMRouter,
    calendar: AppleCalendarWidget | None = None,
    habits: HabitStore | None = None,
    history: DispatchHistory | None = None,
    quotes: QuoteRotator | None = None,
    focus_timer: FocusTimer | None = None,
    health: HealthMonitor | None = None,
) -> FastAPI:
    settings = get_settings()
    bus = _StateBus()
    rate_limit = _SimpleRateLimit(_DISPATCH_RATE_LIMIT_PER_MIN, 60.0)
    if history is None:
        history = DispatchHistory()
    if quotes is None:
        quotes = QuoteRotator()
    if focus_timer is None:
        focus_timer = FocusTimer(timezone_name=settings.timezone)
    if health is None:
        health = HealthMonitor()

    app = FastAPI(title="Lifemax Dashboard", docs_url=None, redoc_url=None)
    app.state.dispatch_history = history
    app.state.quote_rotator = quotes
    app.state.focus_timer = focus_timer
    app.state.health = health

    # ------------------------------------------------------------------
    # Background ticker: republish snapshot every 2s for clock + widget refresh.
    # ------------------------------------------------------------------
    async def _ticker() -> None:
        while True:
            try:
                snap = await build_snapshot(
                    store,
                    weather,
                    news,
                    calendar,
                    habits,
                    settings.timezone,
                    history,
                    quotes,
                    focus_timer,
                    health,
                )
                await bus.publish(snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("snapshot ticker failed: %s", exc)
            await asyncio.sleep(2.0)

    @app.on_event("startup")
    async def _on_startup() -> None:  # noqa: D401
        await store.load()
        if habits is not None:
            await habits.load()

        async def _on_change() -> None:
            try:
                snap = await build_snapshot(
                    store,
                    weather,
                    news,
                    calendar,
                    habits,
                    settings.timezone,
                    history,
                    quotes,
                    focus_timer,
                    health,
                )
                await bus.publish(snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("change-publish failed: %s", exc)

        store.add_listener(_on_change)
        if habits is not None:
            habits.add_listener(_on_change)
        app.state.ticker_task = asyncio.create_task(_ticker())

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:  # noqa: D401
        task: asyncio.Task | None = getattr(app.state, "ticker_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Auth dependency for the dispatch endpoint
    # ------------------------------------------------------------------
    def _require_dispatch_token(
        request: Request,
        x_lifemax_token: str | None = Header(default=None, alias="X-Lifemax-Token"),
    ) -> None:
        # Defense in depth: server is bound to 127.0.0.1, but still demand the token.
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="dispatch is local-only",
            )
        expected = settings.dispatch_token
        if not expected or not x_lifemax_token or not hmac.compare_digest(
            expected, x_lifemax_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        snap = await build_snapshot(
            store,
            weather,
            news,
            calendar,
            habits,
            settings.timezone,
            history,
            quotes,
            focus_timer,
            health,
        )
        return JSONResponse(snap)

    @app.get("/api/retro/weekly")
    async def get_weekly_retro() -> JSONResponse:
        """Standalone weekly retro endpoint (also embedded in the SSE snapshot).

        Useful for the CLI bridge ('how was my week?'), test scripts, and
        future external integrations. Returns the same payload `snap.retro`
        carries — see `widgets/retro.compute_weekly_retro`.
        """

        if habits is None:
            return JSONResponse(
                {"error": "weekly retro requires habits to be enabled"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        all_tasks = await store.all_tasks(include_archived=True)
        all_habits = await habits.all_habits(include_archived=False)
        now = now_in_tz(settings.timezone)
        today_iso = local_habit_date_for(now, settings.timezone)
        window = date_range_iso(today_iso, days=14)
        focus_per_day: dict[str, int] = {}
        if focus_timer is not None:
            focus_per_day = await focus_timer.focus_blocks_per_day(window)
        retro = compute_weekly_retro(
            tasks=all_tasks,
            habits=all_habits,
            focus_blocks_per_day=focus_per_day,
            timezone_name=settings.timezone,
            now=now,
        )
        return JSONResponse(retro)

    @app.get("/api/health")
    async def get_health() -> JSONResponse:
        """Read-only health-vitals endpoint.

        Returns the same payload that `snap.health` carries — useful for
        monitoring scripts (launchd keep-alive, command-line probes).
        """

        cal_status = (
            await calendar.get_status() if calendar is not None else None
        )
        payload = await health.compose(
            tasks_store=store,
            habits_store=habits,
            weather=weather,
            calendar_status=cal_status,
        )
        return JSONResponse(payload)

    @app.get("/api/stream")
    async def stream(request: Request) -> EventSourceResponse:
        queue = await bus.subscribe()
        # Send an initial snapshot immediately so the page paints fast.
        try:
            initial = await build_snapshot(
                store,
                weather,
                news,
                calendar,
                habits,
                settings.timezone,
                history,
                quotes,
                focus_timer,
                health,
            )
            queue.put_nowait(initial)
        except asyncio.QueueFull:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("initial snapshot failed: %s", exc)

        async def _gen():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE keep-alive comment to keep the connection warm.
                        yield {"event": "ping", "data": "keepalive"}
                        continue
                    yield {"event": "snapshot", "data": json.dumps(payload)}
            finally:
                await bus.unsubscribe(queue)

        return EventSourceResponse(_gen())

    @app.post("/api/dispatch", dependencies=[Depends(_require_dispatch_token)])
    async def dispatch(request: Request) -> JSONResponse:
        if not await rate_limit.hit():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
            )

        # Enforce body-size cap before parsing.
        body = await request.body()
        if len(body) > _MAX_DISPATCH_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="payload too large",
            )
        try:
            payload = DispatchRequest.model_validate_json(body)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid payload: {exc}",
            ) from exc

        # Cheap path: if the user just typed "undo" / "oops" / "revert" / "rollback"
        # by itself, skip the LLM round-trip and run the undo directly.
        timer_literal = parse_literal_timer(payload.text)
        if is_literal_undo(payload.text):
            intent = Intent(action="undo")
        elif timer_literal is not None:
            intent = Intent(
                action="timer",
                timer=TimerFields(**timer_literal),
            )
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
                    payload.text,
                    open_tasks,
                    today_anchor=today_anchor,
                    habits=current_habits,
                )
                # Health vitals: every successful round-trip is recorded.
                # Best-effort — never let observability break the dispatch.
                try:
                    await health.record_llm_ok(model=settings.openrouter_model)
                except Exception:  # noqa: BLE001
                    pass
            except OpenRouterError as exc:
                try:
                    await health.record_llm_error(
                        str(exc), model=settings.openrouter_model
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("dispatch parse failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="llm parse failed",
                ) from exc

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

        # History ribbon: record a sanitized entry for every dispatch, so the
        # UI can show a "what just happened" strip and the next 'undo' has a
        # target. Successful undo calls are recorded too (without their own
        # undo_payload, because we don't redo).
        try:
            await history.push(
                {
                    "input_text": payload.text,
                    "action": str(intent.action),
                    "ok": result.ok,
                    "undid": result.undid,
                    "message": result.message,
                    "subject": _result_subject(result),
                    "undo_payload": (
                        result.undo_payload
                        if (result.ok and not result.undid and result.undo_payload)
                        else None
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dispatch history push failed: %s", exc)

        # Push an immediate snapshot so the ribbon updates without waiting for
        # the 2s ticker. Best-effort: any failure here just means the next tick
        # will catch up.
        try:
            snap = await build_snapshot(
                store,
                weather,
                news,
                calendar,
                habits,
                settings.timezone,
                history,
                quotes,
                focus_timer,
                health,
            )
            await bus.publish(snap)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-dispatch snapshot failed: %s", exc)

        return JSONResponse(
            {
                "ok": result.ok,
                "message": result.message,
                "task": result.task.model_dump(mode="json") if result.task else None,
                "event": result.event,
                "habit": result.habit.model_dump(mode="json") if result.habit else None,
                "undid": result.undid,
            }
        )

    return app
