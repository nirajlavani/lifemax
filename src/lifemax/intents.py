"""Apply parsed intents (`Intent`) against the task store and Apple Calendar.

Returns a small `IntentResult` describing what happened, which the bot/CLI
can echo back to the user. For queries we ask the LLM to phrase the reply
using the latest task snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import HABIT_DAY_CUTOFF_HOUR
from .dispatch_history import DispatchHistory
from .habits_store import HabitStore
from .llm import LLMRouter, OpenRouterError
from .models import Habit, Intent, IntentFields, Status, Task
from .store import TaskStore
from .widgets.calendar_apple import AppleCalendarWidget, CalendarUnavailableError
from .widgets.focus_timer import FocusTimer
from .widgets.news_x import NewsWidget
from .widgets.time_utils import format_today_summary, habit_day_in_tz, today_in_tz

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntentResult:
    """Describes the outcome of applying an intent. Used for user-facing replies."""

    ok: bool
    message: str
    task: Task | None = None
    event: dict[str, Any] | None = None
    habit: Habit | None = None
    timer: dict[str, Any] | None = None
    undo_payload: dict[str, Any] | None = None
    undid: bool = False


def _apply_fields(task: Task, fields: IntentFields) -> Task:
    data = task.model_dump()
    for key, value in fields.model_dump().items():
        if value is None:
            continue
        data[key] = value
    return Task.model_validate(data)


async def _resolve_target(intent: Intent, store: TaskStore) -> Task | None:
    if intent.task_id:
        task = await store.get(intent.task_id)
        if task is not None:
            return task
    if intent.task_match_title:
        return await store.find_by_title(intent.task_match_title)
    return None


def _parse_event_datetime(
    value: str,
    *,
    timezone_name: str,
    end_of_day: bool = False,
) -> datetime:
    """Parse an LLM-provided ISO8601 string. Accepts date-only strings too.

    Date-only ('YYYY-MM-DD') becomes either midnight (start) or 23:59:59 (end_of_day),
    interpreted in the user's timezone.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("empty datetime string")
    tz = ZoneInfo(timezone_name)
    # Date-only path
    if len(text) == 10 and text.count("-") == 2:
        d = datetime.strptime(text, "%Y-%m-%d").date()
        t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        return datetime.combine(d, t, tzinfo=tz)
    # ISO 8601 with optional 'Z'
    iso = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _format_event_summary(
    *, title: str, start: datetime, end: datetime, all_day: bool, tz_name: str
) -> str:
    tz = ZoneInfo(tz_name)
    s = start.astimezone(tz)
    e = end.astimezone(tz)
    same_day = s.date() == e.date()
    if all_day:
        if same_day:
            return f"Added: {title} — {s.strftime('%a, %b %-d')} (all day)"
        return (
            f"Added: {title} — {s.strftime('%a, %b %-d')} → "
            f"{e.strftime('%a, %b %-d')} (all day)"
        )
    if same_day:
        return (
            f"Added: {title} — {s.strftime('%a, %b %-d, %-I:%M%p').lower()} → "
            f"{e.strftime('%-I:%M%p').lower()}"
        )
    return (
        f"Added: {title} — {s.strftime('%a, %b %-d %-I:%M%p').lower()} → "
        f"{e.strftime('%a, %b %-d %-I:%M%p').lower()}"
    )


async def _resolve_habit(
    intent: Intent, habits: HabitStore | None
) -> Habit | None:
    if habits is None:
        return None
    name = (intent.habit.match_title or intent.habit.title or "").strip()
    if not name:
        return None
    return await habits.find_by_title(name)


async def apply_intent(
    intent: Intent,
    store: TaskStore,
    llm: LLMRouter,
    *,
    timezone_name: str,
    calendar: AppleCalendarWidget | None = None,
    habits: HabitStore | None = None,
    history: DispatchHistory | None = None,
    focus_timer: FocusTimer | None = None,
    news: NewsWidget | None = None,
) -> IntentResult:
    """Apply a parsed intent. Pure side-effects + a small descriptive result."""
    if intent.action == "undo":
        return await _apply_undo(
            store=store,
            calendar=calendar,
            habits=habits,
            history=history,
        )

    if intent.action == "create":
        title = (intent.fields.title or "").strip()
        if not title:
            return IntentResult(False, "Sorry, I need a task title to create one.")
        task = Task(
            title=title,
            description=(intent.fields.description or "").strip(),
            deadline=intent.fields.deadline,
            priority=intent.fields.priority or Task.model_fields["priority"].default,
            urgency=intent.fields.urgency or Task.model_fields["urgency"].default,
            status=intent.fields.status or Status.TODO,
        )
        await store.upsert(task)
        return IntentResult(
            True,
            f"Added: {task.title}",
            task,
            undo_payload={"kind": "task_create", "task_id": task.id},
        )

    if intent.action == "update":
        target = await _resolve_target(intent, store)
        if target is None:
            return IntentResult(False, "I couldn't find that task to update.")
        merged = _apply_fields(target, intent.fields)
        updated = await store.upsert(merged)
        return IntentResult(True, f"Updated: {updated.title}", updated)

    if intent.action == "complete":
        target = await _resolve_target(intent, store)
        if target is None:
            return IntentResult(False, "I couldn't find that task to complete.")
        prev_status = target.status.value if hasattr(target.status, "value") else str(target.status)
        updated = await store.complete(target.id)
        if updated is None:
            return IntentResult(False, "That task vanished mid-update; try again.")
        return IntentResult(
            True,
            f"Completed: {updated.title}",
            updated,
            undo_payload={
                "kind": "task_status",
                "task_id": updated.id,
                "previous_status": prev_status,
            },
        )

    if intent.action == "archive":
        target = await _resolve_target(intent, store)
        if target is None:
            return IntentResult(False, "I couldn't find that task to archive.")
        prev_status = target.status.value if hasattr(target.status, "value") else str(target.status)
        updated = await store.archive(target.id)
        if updated is None:
            return IntentResult(False, "That task vanished mid-update; try again.")
        return IntentResult(
            True,
            f"Archived: {updated.title}",
            updated,
            undo_payload={
                "kind": "task_status",
                "task_id": updated.id,
                "previous_status": prev_status,
            },
        )

    if intent.action == "query":
        question = (intent.query or "").strip()
        if not question:
            return IntentResult(False, "What would you like to know?")
        open_tasks = await store.all_tasks(include_archived=False)
        # Pass the cached news items so the model can answer "send me the
        # link to that article" questions WITHOUT hallucinating URLs. The
        # reply system prompt restricts URL output to this snapshot only.
        # Best-effort: a failed feed fetch should never break a query that
        # has nothing to do with news (e.g. "what's on my plate today?").
        news_items: list[dict] | None = None
        if news is not None:
            try:
                news_items = await news.get()
            except Exception as exc:  # noqa: BLE001
                logger.warning("news fetch for query failed: %s", exc)
                news_items = None
        try:
            today_ctx = format_today_summary(today_in_tz(timezone_name))
            answer = await llm.answer_query(
                question,
                open_tasks,
                extra_context=f"Today is {today_ctx} ({timezone_name}).",
                news_items=news_items,
            )
        except OpenRouterError as exc:
            logger.warning("query reply failed: %s", exc)
            return IntentResult(False, "Couldn't reach the LLM right now.")
        return IntentResult(True, answer)

    if intent.action == "add_event":
        if calendar is None:
            return IntentResult(
                False, "Calendar isn't available on this machine."
            )
        ev = intent.event
        title = (ev.title or "").strip()
        if not title:
            return IntentResult(False, "I need a title for the calendar event.")
        if not ev.start:
            return IntentResult(False, "I need a start time for the calendar event.")
        all_day = bool(ev.all_day)
        try:
            start_dt = _parse_event_datetime(ev.start, timezone_name=timezone_name)
            if ev.end:
                end_dt = _parse_event_datetime(
                    ev.end, timezone_name=timezone_name, end_of_day=all_day
                )
            else:
                # Default duration: 1 hour for timed events, full day for all-day.
                end_dt = (
                    datetime.combine(
                        start_dt.date(), time(23, 59, 59), tzinfo=start_dt.tzinfo
                    )
                    if all_day
                    else start_dt + timedelta(hours=1)
                )
        except ValueError as exc:
            return IntentResult(False, f"Couldn't parse the event time: {exc}")
        if end_dt < start_dt:
            return IntentResult(False, "Event end must be on or after the start.")
        try:
            saved = await calendar.add_event(
                title=title,
                start=start_dt,
                end=end_dt,
                all_day=all_day,
                notes=(ev.notes or None),
                location=(ev.location or None),
            )
        except CalendarUnavailableError as exc:
            logger.warning("add_event failed: %s", exc)
            return IntentResult(False, f"Calendar error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_event unexpected failure: %s", exc)
            return IntentResult(False, "Couldn't save the calendar event.")
        event_id = saved.get("id") if isinstance(saved, dict) else None
        undo_payload: dict[str, Any] | None = None
        if event_id:
            undo_payload = {
                "kind": "event_create",
                "event_id": event_id,
                "title": title,
            }
        return IntentResult(
            True,
            _format_event_summary(
                title=title,
                start=start_dt,
                end=end_dt,
                all_day=all_day,
                tz_name=timezone_name,
            ),
            event=saved,
            undo_payload=undo_payload,
        )

    if intent.action in {
        "add_habit",
        "check_habit",
        "uncheck_habit",
        "remove_habit",
    }:
        if habits is None:
            return IntentResult(False, "Daily checklist isn't available.")
        today_iso = habit_day_in_tz(
            timezone_name, cutoff_hour=HABIT_DAY_CUTOFF_HOUR
        ).isoformat()

        if intent.action == "add_habit":
            title = (intent.habit.title or intent.fields.title or "").strip()
            if not title:
                return IntentResult(False, "I need a name for the daily item.")
            existing = await habits.find_by_title(title)
            if existing is not None:
                return IntentResult(
                    False,
                    f"'{existing.title}' is already on the daily list.",
                    habit=existing,
                )
            try:
                habit = await habits.add(title)
            except ValueError as exc:
                return IntentResult(False, str(exc))
            return IntentResult(
                True,
                f"Added daily: {habit.title}",
                habit=habit,
                undo_payload={"kind": "habit_add", "habit_id": habit.id},
            )

        habit = await _resolve_habit(intent, habits)
        if habit is None:
            name = (intent.habit.match_title or intent.habit.title or "").strip()
            return IntentResult(
                False,
                f"I couldn't find a daily item matching '{name}'."
                if name
                else "Which daily item?",
            )

        if intent.action == "check_habit":
            updated = await habits.mark_done(habit.id, local_date_iso=today_iso)
            if updated is None:
                return IntentResult(False, "That daily item vanished mid-update.")
            return IntentResult(
                True,
                f"Checked off: {updated.title}",
                habit=updated,
                undo_payload={
                    "kind": "habit_check",
                    "habit_id": updated.id,
                    "local_date_iso": today_iso,
                },
            )

        if intent.action == "uncheck_habit":
            updated = await habits.undo_done(habit.id, local_date_iso=today_iso)
            if updated is None:
                return IntentResult(False, "That daily item vanished mid-update.")
            return IntentResult(
                True,
                f"Undid today's check: {updated.title}",
                habit=updated,
                undo_payload={
                    "kind": "habit_uncheck",
                    "habit_id": updated.id,
                    "local_date_iso": today_iso,
                },
            )

        if intent.action == "remove_habit":
            removed = await habits.remove(habit.id)
            if removed is None:
                return IntentResult(False, "That daily item is already gone.")
            return IntentResult(
                True, f"Removed daily: {removed.title}", habit=removed
            )

    if intent.action == "timer":
        if focus_timer is None:
            return IntentResult(False, "Focus timer isn't available.")
        op = (intent.timer.op or "").strip().lower()
        minutes = intent.timer.minutes
        label = (intent.timer.label or "").strip()
        if op == "start":
            snap = await focus_timer.start_focus(minutes=minutes, label=label)
            mins = snap["total_seconds"] // 60
            tail = f" — {snap['label']}" if snap.get("label") else ""
            return IntentResult(
                True, f"Focus timer started · {mins} min{tail}", timer=snap
            )
        if op == "break":
            snap = await focus_timer.start_break(minutes=minutes)
            mins = snap["total_seconds"] // 60
            kind = "long break" if snap["phase"] == "break_long" else "break"
            return IntentResult(True, f"{kind.capitalize()} · {mins} min", timer=snap)
        if op == "pause":
            snap = await focus_timer.pause()
            return IntentResult(True, f"Paused · {snap['countdown']} left", timer=snap)
        if op == "resume":
            snap = await focus_timer.resume()
            return IntentResult(True, f"Resumed · {snap['countdown']} left", timer=snap)
        if op == "extend":
            snap = await focus_timer.extend(minutes=minutes)
            return IntentResult(
                True, f"Extended · {snap['countdown']} left", timer=snap
            )
        if op == "stop":
            snap = await focus_timer.stop()
            return IntentResult(True, "Timer stopped.", timer=snap)
        return IntentResult(False, f"Unknown timer op: {op or 'missing'}")

    return IntentResult(False, f"Unsupported action: {intent.action}")


async def _apply_undo(
    *,
    store: TaskStore,
    calendar: AppleCalendarWidget | None,
    habits: HabitStore | None,
    history: DispatchHistory | None,
) -> IntentResult:
    if history is None:
        return IntentResult(False, "Nothing to undo.")
    entry = await history.pop_reversible()
    if entry is None:
        return IntentResult(False, "Nothing to undo.")
    payload = entry.get("undo_payload") or {}
    kind = str(payload.get("kind") or "")

    if kind == "task_create":
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return IntentResult(False, "Couldn't undo: missing task id.", undid=True)
        archived = await store.archive(task_id)
        if archived is None:
            return IntentResult(False, "Couldn't undo: task already gone.", undid=True)
        return IntentResult(
            True, f"Reverted: archived '{archived.title}'.", task=archived, undid=True
        )

    if kind == "task_status":
        task_id = str(payload.get("task_id") or "")
        prev_status_raw = str(payload.get("previous_status") or "")
        try:
            prev_status = Status(prev_status_raw)
        except ValueError:
            prev_status = Status.TODO
        target = await store.get(task_id)
        if target is None:
            return IntentResult(False, "Couldn't undo: task no longer exists.", undid=True)
        target.status = prev_status
        target.touch()
        restored = await store.upsert(target)
        return IntentResult(
            True,
            f"Reverted: '{restored.title}' back to {prev_status.value}.",
            task=restored,
            undid=True,
        )

    if kind == "event_create":
        if calendar is None:
            return IntentResult(False, "Calendar isn't available to undo.", undid=True)
        event_id = str(payload.get("event_id") or "")
        title = str(payload.get("title") or "")
        if not event_id:
            return IntentResult(False, "Couldn't undo: missing event id.", undid=True)
        try:
            ok = await calendar.delete_event(event_id)
        except CalendarUnavailableError as exc:
            logger.warning("undo event delete failed: %s", exc)
            return IntentResult(False, f"Couldn't undo event: {exc}", undid=True)
        if not ok:
            return IntentResult(False, "Couldn't find that event in Calendar.", undid=True)
        nice = title or "the event"
        return IntentResult(True, f"Reverted: removed '{nice}' from Calendar.", undid=True)

    if kind == "habit_add":
        if habits is None:
            return IntentResult(False, "Daily checklist isn't available to undo.", undid=True)
        habit_id = str(payload.get("habit_id") or "")
        if not habit_id:
            return IntentResult(False, "Couldn't undo: missing habit id.", undid=True)
        removed = await habits.remove(habit_id)
        if removed is None:
            return IntentResult(False, "Daily item already gone.", undid=True)
        return IntentResult(True, f"Reverted: removed daily '{removed.title}'.", habit=removed, undid=True)

    if kind == "habit_check":
        if habits is None:
            return IntentResult(False, "Daily checklist isn't available to undo.", undid=True)
        habit_id = str(payload.get("habit_id") or "")
        date_iso = str(payload.get("local_date_iso") or "")
        if not habit_id or not date_iso:
            return IntentResult(False, "Couldn't undo: missing habit context.", undid=True)
        updated = await habits.undo_done(habit_id, local_date_iso=date_iso)
        if updated is None:
            return IntentResult(False, "Daily item vanished.", undid=True)
        return IntentResult(True, f"Reverted: unchecked '{updated.title}'.", habit=updated, undid=True)

    if kind == "habit_uncheck":
        if habits is None:
            return IntentResult(False, "Daily checklist isn't available to undo.", undid=True)
        habit_id = str(payload.get("habit_id") or "")
        date_iso = str(payload.get("local_date_iso") or "")
        if not habit_id or not date_iso:
            return IntentResult(False, "Couldn't undo: missing habit context.", undid=True)
        updated = await habits.mark_done(habit_id, local_date_iso=date_iso)
        if updated is None:
            return IntentResult(False, "Daily item vanished.", undid=True)
        return IntentResult(True, f"Reverted: re-checked '{updated.title}'.", habit=updated, undid=True)

    return IntentResult(False, "Couldn't undo: unknown action type.", undid=True)
