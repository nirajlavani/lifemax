# dispatch history ribbon + undo

> Branding rules: see `docs/improvements_log.md`.

## Why

The dashboard is display-only, but the user dispatches commands from
Telegram + the CLI. Without a feedback ribbon you have to glance up to
verify the kanban or daily list updated. Adding a small history strip and
an `undo` keyword makes "did that take?" instant, and lets you reverse a
typo in one tap.

## Scope shipped

- `src/lifemax/dispatch_history.py` — async-safe in-memory ring buffer
  (cap 20; UI snapshot trimmed to the latest 6).
- Each dispatch records: timestamp, monotonic clock for age, action verb,
  ok/undid flags, the cleaned input text, the affected entity's title
  (`subject`), the assistant's reply (`message`), and an `undo_payload`
  describing how to reverse the side effect — e.g.
  `{kind: "task_create", task_id}`, `{kind: "task_status",
  task_id, previous_status}`, `{kind: "event_create", event_id}`,
  `{kind: "habit_check", habit_id, local_date_iso}`.
- `Intent.action` gains `"undo"` and the LLM system prompt teaches the
  model to emit it for words like "undo", "oops", "revert", "rollback".
- New behaviour in `apply_intent`:
  - `undo` pops the most recent reversible, not-yet-undone entry from the
    history and runs the inverse mutation. Reads `IntentResult.undid` to
    flag undo-flow results so the UI / history can mark them clearly.
  - All non-undo successful actions return an `undo_payload`; the API +
    Telegram layers push the sanitized record into the history ring.
- Cheap path: when the dispatch text is *literally* `undo`, `revert`,
  `oops`, or `rollback` (case-insensitive), the API + Telegram bot
  short-circuit the LLM round-trip and dispatch `Intent(action="undo")`
  directly. Same helper (`is_literal_undo`) gates both.
- The snapshot exposes `history.items` (newest first, max 6) for the UI.
  Each item ships `action`, `ok`, `undid`, `age_seconds`, `subject`,
  `input_text`, `message`, `reversible`, and a stripped
  `undo_payload: {kind}` so the browser can show an "undo" affordance
  without leaking task/event IDs.
- After every dispatch the API publishes a snapshot immediately so the
  ribbon updates without waiting for the 2-second ticker.
- New corner ribbon (`#ribbon`) anchored bottom-left, mirroring the
  existing `#conn-pill`. Renders the latest 4 entries on a single line
  with a status pip (ok / err / undo), action verb, subject, and age.
  When the next dispatch is reversible we also show a "say 'undo' →"
  pill so it's obvious what literal text reverts the most recent change.
  Lives in the `#stage` overlay layer so the 1600 × 1066 grid stays
  untouched.
- `/undo` Telegram command for parity with the CLI bridge.

## Reversible actions

| original action | inverse |
| --- | --- |
| `create` task | archive (`store.archive(task_id)`); we don't hard-delete |
| `complete` task | restore previous status (typically `todo` / `in_progress`) |
| `archive` task | restore previous status |
| `add_event` | delete the just-created Apple Calendar event by EventKit identifier |
| `add_habit` | remove the new habit |
| `check_habit` | uncheck (matches the original `local_date_iso`) |
| `uncheck_habit` | re-check (matches the original `local_date_iso`) |
| `update` task fields | not reversible in this pass — recorded in history but not undoable; we'd need to snapshot the previous field values, and that's worth its own design pass |
| `remove_habit` | not reversible in this pass — re-adding would lose streak history; left out intentionally |
| `query` | not reversible (still recorded, marked `reversible=false`) |

If `undo` is invoked on something we can't reverse (or the buffer is
empty) we return a polite no-op.

## Assumptions I made and answered

- **Q: persistent history?**  
  A: No. In-process ring buffer only. The dashboard restarts often enough
  that persisting would imply distributed state and a migration story
  we don't need yet.
- **Q: how do we make `undo` robust against typos and against LLM
  hiccups (the OpenRouter "choices" outage we just patched)?**  
  A: Two paths: the LLM can emit `action: "undo"`, *and* the dispatch
  endpoint short-circuits when the cleaned text matches the
  `is_literal_undo` allow-list (`undo`, `revert`, `oops`, `rollback`,
  case-insensitive). Both call the same helper, so even if OpenRouter is
  down the user can still undo their last action.
- **Q: what about history when the user issues 5 things and then
  `undo`?**  
  A: Undo only reverses *the most recent reversible, not-yet-undone*
  entry. We push the resulting `undo` itself into the ring (with
  `undid=true` and no `undo_payload`), so the user sees a trail and can
  chain undos to walk back through the buffer.
- **Q: how is calendar event undo done?**  
  A: We capture the EventKit identifier from `add_event`'s saved payload
  and call a new `AppleCalendarWidget.delete_event(identifier)` helper.
  If the helper fails (permissions / missing) the undo reports the
  failure but doesn't break the rest of the buffer.
- **Q: do we leak anything sensitive in the snapshot?**  
  A: We strip the `undo_payload` down to `{kind}` before exposing it to
  the browser. Real task / event / habit IDs stay server-side. The
  ribbon only needs to know "this thing is reversible".
- **Q: how do we keep updates from `update` (multi-field) safe?**  
  A: We don't undo `update` yet — too easy to clobber concurrent edits
  from Telegram. The action still appears in the ribbon (with
  `reversible=false`) so the user sees that *something* happened. A
  follow-up note will spec field-snapshot undo when we want it.
- **Q: branding for the ribbon?**  
  A: Eyebrow reads `RECENT · 09`, continuing the `… · 05/06/07/08`
  numbering scheme called out in `improvements_log.md`. Pip + verb
  colours come straight from the palette tokens (`--cream` for undo,
  `#6ee7a7` for ok, `#f99` for error — matching existing alert pills).
  Ribbon sits behind `pointer-events: none` because the dashboard is
  display-only.

## File touchpoints

- `src/lifemax/dispatch_history.py` (new) — ring buffer + `is_literal_undo`.
- `src/lifemax/models.py` — `IntentAction` gains `"undo"`,
  `INTENT_JSON_SCHEMA` enum updated, `IntentResult` gains `undo_payload`
  and `undid`.
- `src/lifemax/llm.py` — system prompt mentions the new action.
- `src/lifemax/intents.py` — every reversible action now returns an
  `undo_payload`; new `_apply_undo` helper performs the inverse.
- `src/lifemax/widgets/calendar_apple.py` — async `delete_event` plus
  `_delete_event_sync` helper.
- `src/lifemax/server/api.py` — share a `DispatchHistory`, push entries
  after each dispatch, expose `history.items` in the snapshot, publish
  an immediate snapshot post-dispatch, and short-circuit literal `undo`.
- `src/lifemax/bot/telegram_bot.py` — `/undo` command + the same
  short-circuit + history push.
- `src/lifemax/main.py` — instantiate one `DispatchHistory` and inject
  it into both the FastAPI app and the Telegram bot.
- `src/lifemax/server/static/index.html|styles.css|app.js` — bottom-left
  `#ribbon` overlay with status pip / verb / subject / age, plus an
  "say 'undo' →" pill when the most recent action is reversible.
- `tests/test_dispatch_history.py` (new), `tests/test_intents.py` (new
  undo assertions), `tests/test_llm_schema.py` (enum updated).
