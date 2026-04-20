"""Pydantic models for tasks and parsed user intents."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Urgency(str, Enum):
    URGENT = "urgent"
    NON_URGENT = "non_urgent"


class Status(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Task(BaseModel):
    """A single life-maxing task."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    title: str
    description: str = ""
    deadline: str | None = None  # ISO 8601 in the configured timezone, or None
    priority: Priority = Priority.MEDIUM
    urgency: Urgency = Urgency.NON_URGENT
    status: Status = Status.TODO
    archived: bool = False
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def touch(self) -> None:
        self.updated_at = _now_iso()


class Habit(BaseModel):
    """A single recurring daily checklist item.

    Daily checklist semantics: each habit auto-resets every "habit day" (local
    midnight, with a 3am cutoff so late-night check-offs still count for the
    previous day). We persist:
      * `last_done_local_date` for the legacy "is it ticked today?" check.
      * `completed_dates` — append-only, deduped, length-capped history used to
        compute streaks and the 7-day strip.
      * `best_streak_cached` — the highest consecutive-day run we've ever seen,
        preserved even when older dates fall out of `completed_dates`.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    title: str
    sort_order: int = 0
    last_done_local_date: str | None = None
    completed_dates: list[str] = Field(default_factory=list)
    best_streak_cached: int = 0
    archived: bool = False
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def is_done_for(self, local_date_iso: str) -> bool:
        return bool(self.last_done_local_date) and self.last_done_local_date == local_date_iso


# ---------------------------------------------------------------------------
# Intent: what the LLM extracts from a user message.
# ---------------------------------------------------------------------------
IntentAction = Literal[
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
]


class IntentFields(BaseModel):
    """Partial fields the LLM may set when creating or updating a task."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    description: str | None = None
    deadline: str | None = None
    priority: Priority | None = None
    urgency: Urgency | None = None
    status: Status | None = None


class CalendarEventFields(BaseModel):
    """Calendar event payload extracted by the LLM for `add_event` actions."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    # ISO 8601 datetimes. For all_day events these may be date-only ("YYYY-MM-DD").
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    notes: str | None = None
    location: str | None = None


class HabitFields(BaseModel):
    """Habit payload extracted by the LLM for habit-related actions."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    match_title: str | None = None  # for check/uncheck/remove by name


TimerOp = Literal[
    "start",
    "stop",
    "pause",
    "resume",
    "break",
    "extend",
]


class TimerFields(BaseModel):
    """Pomodoro / focus timer payload extracted by the LLM for `timer` actions.

    The shape is intentionally tiny: an `op` verb plus optional `minutes`
    (used for `start`, `break`, and `extend`) and a `label` (used for `start`
    so the active block can carry the user's intent — e.g. "deep work").
    """

    model_config = ConfigDict(extra="ignore")

    op: TimerOp | None = None
    minutes: int | None = None
    label: str | None = None


class Intent(BaseModel):
    """Structured representation of a single user message."""

    model_config = ConfigDict(extra="ignore")

    action: IntentAction
    task_id: str | None = None
    # When the user references a task by name (e.g. "the gym task"),
    # the LLM puts the resolved match here.
    task_match_title: str | None = None
    fields: IntentFields = Field(default_factory=IntentFields)
    event: CalendarEventFields = Field(default_factory=CalendarEventFields)
    habit: HabitFields = Field(default_factory=HabitFields)
    timer: TimerFields = Field(default_factory=TimerFields)
    query: str | None = None  # Natural-language question for action == "query".


# JSON Schema sent to the LLM via OpenRouter `response_format`.
# Strict mode requires every property to be required and additionalProperties=false.
INTENT_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "task_id",
        "task_match_title",
        "fields",
        "event",
        "habit",
        "timer",
        "query",
    ],
    "properties": {
        "action": {
            "type": "string",
            "enum": [
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
            ],
            "description": (
                "What the user wants to do. 'create' = new task. 'update' = change a field. "
                "'complete' = mark a task done. 'archive' = soft-delete (user said delete or archive). "
                "'query' = the user asked a question (e.g. 'what are today's goals?'). "
                "'add_event' = create a NEW Apple Calendar event (single-day or multi-day). "
                "'add_habit' = add a recurring DAILY checklist item that auto-resets each day. "
                "'check_habit' = mark a daily item done for today. "
                "'uncheck_habit' = undo today's check. "
                "'remove_habit' = remove a daily item from the list. "
                "'undo' = reverse the most recent reversible dispatch (use when the user "
                "says 'undo', 'oops', 'revert', or 'never mind'). "
                "'timer' = pomodoro / focus timer: start, stop, pause, resume, take a break, "
                "or extend the current block. Always populate the 'timer' object with op + "
                "optional minutes/label."
            ),
        },
        "task_id": {
            "type": ["string", "null"],
            "description": "Existing task id from the snapshot, if the user referenced one directly.",
        },
        "task_match_title": {
            "type": ["string", "null"],
            "description": (
                "If the user referenced a task by name (e.g. 'the gym task'), put the best matching "
                "title from the snapshot here. Null if unknown or for create/query/add_event."
            ),
        },
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "description", "deadline", "priority", "urgency", "status"],
            "properties": {
                "title": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "deadline": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 datetime in the user's timezone, or null.",
                },
                "priority": {
                    "type": ["string", "null"],
                    "enum": ["high", "medium", "low", None],
                },
                "urgency": {
                    "type": ["string", "null"],
                    "enum": ["urgent", "non_urgent", None],
                },
                "status": {
                    "type": ["string", "null"],
                    "enum": ["todo", "in_progress", "done", None],
                },
            },
        },
        "event": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "start", "end", "all_day", "notes", "location"],
            "description": (
                "Populate ONLY when action == 'add_event'. Otherwise set every field to null/false."
            ),
            "properties": {
                "title": {"type": ["string", "null"]},
                "start": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO 8601 datetime in the user's timezone "
                        "(e.g. '2026-04-19T15:00:00-04:00'). For all_day events you may use "
                        "a date-only string 'YYYY-MM-DD'."
                    ),
                },
                "end": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO 8601 datetime >= start. For multi-day events, set end to the LAST day. "
                        "For all_day events the date-only form is fine."
                    ),
                },
                "all_day": {"type": ["boolean", "null"]},
                "notes": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
            },
        },
        "habit": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "match_title"],
            "description": (
                "Populate ONLY when action is one of add_habit/check_habit/uncheck_habit/remove_habit. "
                "Use 'title' for add_habit (the new daily item's wording). Use 'match_title' for "
                "check/uncheck/remove (the user-spoken name of the existing item, e.g. 'gym')."
            ),
            "properties": {
                "title": {"type": ["string", "null"]},
                "match_title": {"type": ["string", "null"]},
            },
        },
        "timer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "minutes", "label"],
            "description": (
                "Populate ONLY when action == 'timer'. 'op' is the verb. "
                "'minutes' is the duration to use for start/break/extend (positive integer). "
                "'label' is what the user is focusing on for this block (e.g. 'deep work')."
            ),
            "properties": {
                "op": {
                    "type": ["string", "null"],
                    "enum": ["start", "stop", "pause", "resume", "break", "extend", None],
                },
                "minutes": {"type": ["integer", "null"], "minimum": 1, "maximum": 240},
                "label": {"type": ["string", "null"]},
            },
        },
        "query": {
            "type": ["string", "null"],
            "description": "The natural-language question for action == 'query', else null.",
        },
    },
}
