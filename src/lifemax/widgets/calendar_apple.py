"""Apple Calendar (EventKit) integration.

Read all local + iCloud calendars and write new events to a dedicated
`lifemax` calendar (auto-created in iCloud, falling back to a local source
if iCloud isn't available).

EventKit is a blocking ObjC API, so all calls are dispatched to a thread.
A short TTL cache (60 s) keeps the dashboard ticker from hammering the
underlying store. macOS will prompt for Full Calendar access on first run;
that grant is remembered by the OS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Default name for the calendar where LLM/CLI/Telegram-created events land.
LIFEMAX_CALENDAR_NAME = "lifemax"

# Read-window cache: short enough to feel live, long enough to spare EventKit.
_EVENTS_TTL_SECONDS = 60.0

# Read at least this many days ahead (frontend currently shows 14).
_READ_WINDOW_DAYS = 14


@dataclass(slots=True)
class _EventsCache:
    items: list[dict[str, Any]]
    fetched_at: float
    days: int
    error: str | None = None


class CalendarUnavailableError(RuntimeError):
    """EventKit isn't usable (not on macOS, framework missing, or denied)."""


_PERMISSION_HINT = (
    "Calendar access not granted. Open System Settings → Privacy & Security → "
    "Calendars and enable access for the app running this server (your Terminal, "
    "iTerm, or Cursor)."
)


def _import_eventkit() -> Any:
    """Import EventKit on demand so non-macOS environments still load the module."""
    try:
        import EventKit  # type: ignore[import-not-found]
    except ImportError as exc:  # noqa: BLE001
        raise CalendarUnavailableError(
            "pyobjc-framework-EventKit is not installed (macOS only)."
        ) from exc
    return EventKit


def _ns_date(dt: datetime) -> Any:
    """Convert a tz-aware datetime to an NSDate."""
    from Foundation import NSDate  # type: ignore[import-not-found]

    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _from_ns_date(ns_date: Any, tz_name: str) -> datetime:
    """Convert an NSDate to a tz-aware datetime in the requested zone."""
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970(), tz=ZoneInfo(tz_name))


class AppleCalendarWidget:
    """Read events from all calendars and write to the `lifemax` calendar."""

    def __init__(
        self,
        *,
        timezone_name: str,
        write_calendar_name: str = LIFEMAX_CALENDAR_NAME,
    ) -> None:
        self._tz_name = timezone_name
        self._write_calendar_name = write_calendar_name
        self._cache: _EventsCache | None = None
        self._lock = asyncio.Lock()
        # Lazily initialized in the worker thread; never touched from asyncio code.
        self._store: Any = None
        self._access_granted: bool | None = None
        self._eventkit_module: Any = None

    # ------------------------------------------------------------------
    # Public API (async)
    # ------------------------------------------------------------------
    async def get_events(self, *, days: int = _READ_WINDOW_DAYS) -> list[dict[str, Any]]:
        """Return events in the next `days` days, refreshing if cache is stale."""
        async with self._lock:
            now = time.time()
            if (
                self._cache is not None
                and self._cache.days >= days
                and (now - self._cache.fetched_at) < _EVENTS_TTL_SECONDS
            ):
                return self._cache.items
            err: str | None = None
            try:
                items = await asyncio.to_thread(self._fetch_events_sync, days)
            except CalendarUnavailableError as exc:
                err = str(exc)
                logger.warning("apple calendar unavailable: %s", exc)
                items = []
            except Exception as exc:  # noqa: BLE001
                err = str(exc) or "calendar read failed"
                logger.warning("apple calendar read failed: %s", exc)
                items = []
            self._cache = _EventsCache(
                items=items, fetched_at=now, days=days, error=err
            )
            return items

    async def get_status(self) -> dict[str, Any]:
        """Return a small status object: granted/denied + last error if any."""
        async with self._lock:
            cache = self._cache
        return {
            "available": cache is not None and cache.error is None,
            "error": cache.error if cache is not None else None,
            "write_calendar": self._write_calendar_name,
        }

    async def delete_event(self, event_id: str) -> bool:
        """Delete a previously created event by EventKit identifier.

        Returns True if EventKit confirmed the delete; False if the event
        couldn't be found. Raises CalendarUnavailableError on permission /
        framework problems.
        """
        if not event_id:
            return False
        ok = await asyncio.to_thread(self._delete_event_sync, event_id)
        async with self._lock:
            self._cache = None
        return ok

    async def add_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool = False,
        notes: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Create a new event on the configured write calendar.

        For multi-day events, pass a `start`/`end` that span multiple days
        (or set `all_day=True` and choose midnight-aligned bounds).
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        if end < start:
            raise ValueError("event end must be >= start")
        # EventKit will reject 0-duration timed events on some macOS versions.
        if not all_day and end == start:
            end = start + timedelta(minutes=30)

        result = await asyncio.to_thread(
            self._create_event_sync,
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            notes=notes,
            location=location,
        )
        # Invalidate cache so the new event shows up in the next snapshot.
        async with self._lock:
            self._cache = None
        return result

    # ------------------------------------------------------------------
    # Sync helpers (run in worker threads)
    # ------------------------------------------------------------------
    def _ensure_store(self) -> tuple[Any, Any]:
        """Lazy-init EKEventStore + request authorization. Returns (EventKit, store)."""
        if self._store is not None and self._access_granted:
            return self._eventkit_module, self._store
        EventKit = _import_eventkit()
        self._eventkit_module = EventKit
        if self._store is None:
            self._store = EventKit.EKEventStore.alloc().init()

        # Use the modern API on macOS 14+ when available; fall back otherwise.
        granted_holder: dict[str, bool] = {"value": False}
        done = asyncio.Event() if False else None  # placeholder — using a Condition isn't needed here
        import threading

        gate = threading.Event()

        def _completion(granted: bool, error: Any) -> None:
            granted_holder["value"] = bool(granted)
            if error is not None:
                logger.warning("eventkit authorization error: %s", error)
            gate.set()

        request_full = getattr(
            self._store, "requestFullAccessToEventsWithCompletion_", None
        )
        if request_full is not None:
            request_full(_completion)
        else:
            self._store.requestAccessToEntityType_completion_(
                EventKit.EKEntityTypeEvent, _completion
            )

        # The completion handler runs on a background runloop; wait briefly.
        if not gate.wait(timeout=15.0):
            raise CalendarUnavailableError("timed out waiting for calendar permission")

        self._access_granted = granted_holder["value"]
        if not self._access_granted:
            raise CalendarUnavailableError(_PERMISSION_HINT)
        return EventKit, self._store

    def _fetch_events_sync(self, days: int) -> list[dict[str, Any]]:
        EventKit, store = self._ensure_store()
        tz = ZoneInfo(self._tz_name)
        # Anchor the window at midnight today (local), through end-of-day +days.
        local_now = datetime.now(tz)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = (start_local + timedelta(days=days)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        start_ns = _ns_date(start_local)
        end_ns = _ns_date(end_local)

        calendars = list(store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or [])
        if not calendars:
            return []

        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            start_ns, end_ns, calendars
        )
        ek_events = list(store.eventsMatchingPredicate_(predicate) or [])
        items: list[dict[str, Any]] = []
        for ev in ek_events:
            try:
                items.append(self._serialize_event(ev))
            except Exception as exc:  # noqa: BLE001
                logger.debug("skipping unserializable event: %s", exc)
        items.sort(key=lambda e: (e["start"], e["title"]))
        return items

    def _serialize_event(self, ev: Any) -> dict[str, Any]:
        cal = ev.calendar() if hasattr(ev, "calendar") else None
        cal_title = str(cal.title()) if cal is not None else ""
        cal_color = self._calendar_color_hex(cal)
        start_dt = _from_ns_date(ev.startDate(), self._tz_name)
        end_dt = _from_ns_date(ev.endDate(), self._tz_name)
        is_all_day = bool(ev.isAllDay()) if hasattr(ev, "isAllDay") else False
        location = ev.location()
        notes = ev.notes() if hasattr(ev, "notes") else None
        # eventIdentifier is the stable id for non-recurring events; for recurrences
        # it's still useful for de-dup within a single fetch window.
        ek_id = str(ev.eventIdentifier()) if hasattr(ev, "eventIdentifier") else ""
        return {
            "id": ek_id,
            "title": str(ev.title() or "").strip(),
            "calendar": cal_title,
            "calendar_color": cal_color,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "all_day": is_all_day,
            "location": str(location) if location else "",
            "notes": str(notes) if notes else "",
            "is_lifemax": cal_title == self._write_calendar_name,
        }

    def _calendar_color_hex(self, cal: Any) -> str | None:
        """Best-effort conversion of an EKCalendar's CGColor to a #rrggbb string."""
        if cal is None or not hasattr(cal, "CGColor"):
            return None
        try:
            from Quartz import CoreGraphics as CG  # type: ignore[import-not-found]
        except ImportError:  # noqa: BLE001
            return None
        cg = cal.CGColor()
        if cg is None:
            return None
        try:
            comps_ptr = CG.CGColorGetComponents(cg)
            n = int(CG.CGColorGetNumberOfComponents(cg))
            if not comps_ptr or n < 3:
                return None
            r = max(0.0, min(1.0, float(comps_ptr[0])))
            g = max(0.0, min(1.0, float(comps_ptr[1])))
            b = max(0.0, min(1.0, float(comps_ptr[2])))
            return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
        except Exception:  # noqa: BLE001
            return None

    def _resolve_write_calendar(self, EventKit: Any, store: Any) -> Any:
        """Find the write calendar by name; create it if it doesn't exist yet."""
        target = self._write_calendar_name
        for cal in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []:
            if str(cal.title()) == target and bool(cal.allowsContentModifications()):
                return cal
        # Pick a writable source — prefer iCloud, then Local, then anything else.
        sources = list(store.sources() or [])
        preferred_types = (
            EventKit.EKSourceTypeCalDAV,  # iCloud surfaces here
            EventKit.EKSourceTypeLocal,
            EventKit.EKSourceTypeMobileMe,
            EventKit.EKSourceTypeExchange,
            EventKit.EKSourceTypeSubscribed,
            EventKit.EKSourceTypeBirthdays,
        )
        chosen = None
        for src_type in preferred_types:
            for src in sources:
                if int(src.sourceType()) == int(src_type):
                    # Skip read-only sources (Birthdays, Subscribed).
                    cal_test = EventKit.EKCalendar.calendarForEntityType_eventStore_(
                        EventKit.EKEntityTypeEvent, store
                    )
                    cal_test.setSource_(src)
                    if cal_test.allowsContentModifications():
                        chosen = src
                        break
            if chosen is not None:
                break
        if chosen is None:
            raise CalendarUnavailableError(
                "no writable calendar source available for the lifemax calendar."
            )
        new_cal = EventKit.EKCalendar.calendarForEntityType_eventStore_(
            EventKit.EKEntityTypeEvent, store
        )
        new_cal.setTitle_(target)
        new_cal.setSource_(chosen)
        ok, error = store.saveCalendar_commit_error_(new_cal, True, None)
        if not ok:
            raise CalendarUnavailableError(
                f"failed to create '{target}' calendar: {error}"
            )
        logger.info("created '%s' calendar in source '%s'", target, chosen.title())
        return new_cal

    def _create_event_sync(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool,
        notes: str | None,
        location: str | None,
    ) -> dict[str, Any]:
        EventKit, store = self._ensure_store()
        cal = self._resolve_write_calendar(EventKit, store)

        ev = EventKit.EKEvent.eventWithEventStore_(store)
        ev.setCalendar_(cal)
        ev.setTitle_(title)
        ev.setStartDate_(_ns_date(start.astimezone(ZoneInfo(self._tz_name))))
        ev.setEndDate_(_ns_date(end.astimezone(ZoneInfo(self._tz_name))))
        ev.setAllDay_(bool(all_day))
        if notes:
            ev.setNotes_(notes)
        if location:
            ev.setLocation_(location)

        span = EventKit.EKSpanThisEvent
        ok, error = store.saveEvent_span_commit_error_(ev, span, True, None)
        if not ok:
            raise CalendarUnavailableError(f"failed to save event: {error}")
        return self._serialize_event(ev)

    def _delete_event_sync(self, event_id: str) -> bool:
        EventKit, store = self._ensure_store()
        ev = store.eventWithIdentifier_(event_id)
        if ev is None:
            return False
        span = EventKit.EKSpanThisEvent
        ok, error = store.removeEvent_span_commit_error_(ev, span, True, None)
        if not ok:
            raise CalendarUnavailableError(f"failed to delete event: {error}")
        return True


# Convenience: a small helper for "today is …" prompts so the LLM has a date anchor.
def today_iso(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
