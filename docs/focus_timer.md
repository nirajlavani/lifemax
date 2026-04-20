# pomodoro / focus timer (CLI + on-screen)

> Branding rules: see `docs/improvements_log.md`.

## Why

The dashboard already nominates a "do this" focus task. What it has been
missing is a way to actually time-box the work behind that headline. Adding
a server-owned focus timer gives the user a single, low-friction loop:

```
./bin/lifemax "pomodoro"     # start a 25-minute focus block
./bin/lifemax "pause focus"  # need a quick break
./bin/lifemax "extend 5"     # almost there, give me five more
./bin/lifemax "stop pomodoro"# park it
```

Telegram is wired the same way, and the UI shows a quiet countdown band
inside the existing `FOCUS · 00` card so we don't burn another quadrant.

## Scope shipped

- `src/lifemax/widgets/focus_timer.py` — pure in-memory state machine
  (`idle / running / paused / break`) over phases (`focus / break_short /
   break_long`). All time tracking uses `time.monotonic()`; wall clock is
  only used when emitting `started_at` / `ends_at` for the UI.
  - Public verbs: `start_focus`, `start_break`, `pause`, `resume`,
    `extend`, `stop`, `snapshot`. All are async-safe via a single internal
    `asyncio.Lock`.
  - `snapshot(now=…)` is the single source of truth — auto-handles phase
    elapse on tick (idle once a focus block runs out, increments
    `completed_focus_blocks_today`), with a sane day-rollover using a
    local 3 AM cutoff (matches the daily-habit rules, `_local_habit_date`).
  - `last_event` carries a structured payload (`kind`, `at`, `phase`,
    `state`, `sequence`, `total_seconds`, `blocks_today`) so the UI can
    one-shot a chime when `kind == "elapsed"`.
- `src/lifemax/models.py`
  - New `TimerOp` literal (`start | stop | pause | resume | break | extend`)
    and `TimerFields` model (`op`, `minutes`, `label`).
  - `Intent.action` extended with `"timer"`; `Intent.timer` is required by
    strict JSON schema (every block in `INTENT_JSON_SCHEMA` is required to
    keep OpenRouter's `strict: true` happy).
- `src/lifemax/llm.py` — system prompt teaches the model to emit
  `action="timer"` with the right `op`/`minutes`/`label` and to leave
  the field empty (`op="start"`, `minutes=null`) when the user is not
  asking for the timer.
- `src/lifemax/dispatch_history.py`
  - `parse_literal_timer(text)` short-circuits common verbs ("pomodoro",
    "start timer", "break", "extend 5", "focus 50") so the API + bot can
    skip the LLM round-trip entirely. Tight allow-list of regexes; only
    minute counts in `1..240` are accepted.
- `src/lifemax/intents.py`
  - `IntentResult.timer: dict | None` carries the snapshot back so the
    history ribbon can quote a real countdown (`focus · 25:00`).
  - `apply_intent` accepts `focus_timer: FocusTimer | None`. The new
    branch dispatches `op` to the right verb on the timer and crafts a
    short user-facing message (e.g. `"Resumed · 24:48 left"`).
- `src/lifemax/server/api.py`
  - `build_snapshot` accepts `focus_timer`, includes a `timer` block in
    every snapshot.
  - `create_app` accepts (and default-constructs) one `FocusTimer`,
    stores it on `app.state.focus_timer`, and threads it through every
    `build_snapshot` callsite (ticker, change-publish, `/api/state`,
    `/api/stream`, post-dispatch).
  - `/api/dispatch` runs `parse_literal_timer` first; if it matches, the
    intent is built locally as `Intent(action="timer", timer=…)` and the
    LLM is skipped. The literal-undo short-circuit is preserved.
  - `_result_subject` learned to derive a "label · countdown" subject
    from a timer result so the ribbon shows the right context.
- `src/lifemax/bot/telegram_bot.py`
  - Same plumbing for the Telegram path: literal short-circuit, shared
    `FocusTimer`, timer subject in the dispatch-history push.
- `src/lifemax/main.py` — instantiates one `FocusTimer(timezone_name=…)`
  and passes it to both `create_app` and `build_bot` so the API, the SSE
  ticker, and the bot are looking at the same state.
- Frontend
  - `index.html` — new `<div class="timer" id="timer" hidden>` band
    inside `#q-focus`. Three lines: phase pill + label, big countdown,
    quiet meta line ("running · 3 focus today").
  - `styles.css`
    - `#q-focus` grid grows a third row, `timer`, kept at `auto` height
      so the focus body still owns the rest of the card.
    - `.timer` is a small glass-panel inset (cream-tint border, 4 %
      cream fill) so the band reads as part of the focus card without
      shouting.
    - `.timer__count` uses Times-italic-style numerals (tabular nums) so
      the number doesn't reflow as digits change. Phase pill flips to
      `--ink-dim` while on break; paused state desaturates the count.
  - `app.js`
    - `renderTimer(snap.timer)` shows / hides the band, syncs label,
      phase pill, meta line, and the integer countdown from
      `remaining_seconds`.
    - A 1-second local interval ticks the displayed countdown smoothly
      between SSE pulses; it always snaps back to the server's `ends_at`
      to avoid clock drift.
    - On `last_event.kind == "elapsed"` (deduped via `sequence`) the
      client plays a one-shot WebAudio chime. Autoplay restrictions
      keep it silent until the user has interacted with the page.
- `tests/test_focus_timer.py` — covers the state machine (start, pause,
  resume, extend, stop, break, auto-elapse) plus `parse_literal_timer`
  positive + negative cases.

## Snapshot shape

```
{
  "timer": {
    "state": "running",            // idle | running | paused | break
    "phase": "focus",              // focus | break_short | break_long
    "label": "",
    "total_seconds": 1500,
    "remaining_seconds": 1494,
    "started_at": "2026-04-19T18:09:14.868347+00:00",
    "ends_at":    "2026-04-19T18:34:14.868347+00:00",
    "completed_focus_blocks_today": 0,
    "long_break_every": 4,
    "countdown": "24:54",
    "last_event": {
      "kind": "started",           // started | paused | resumed | extended
      "at": "...",                 //          | stopped | elapsed | break-started
      "phase": "focus",
      "state": "running",
      "sequence": 1,
      "total_seconds": 1500
    },
    "sequence": 1
  }
}
```

`state == "idle"` ⇒ the UI hides the band.

## Branding

- Eyebrow remains `FOCUS · 00` — the focus card already owns it. The new
  band reads as a sub-component of that card, not a new quadrant.
- Only palette tokens are used: `--ink`, `--ink-dim`, `--ink-faint`,
  `--cream`, `--panel`, `--line`. No one-off colours.
- Numerals are tabular and Times-italic so they sit visually with
  `.focus__title`. Phase pill uses uppercase + 0.16em tracking, matching
  the eyebrow grammar everywhere else.
- Audio cue is intentionally short (~0.5 s, 880 Hz → 660 Hz sine,
  ~0.18 gain). Quiet enough not to startle, distinct enough to notice.

## Assumptions / questions I answered for myself

1. **Where does the timer state live?** Server-side, in-process. The
   dashboard, Telegram, and the CLI all read/write the same instance, so
   "what is the timer doing right now?" has exactly one answer regardless
   of surface. Process restarts clear it — that's intentional for a
   single-user Mac mini setup.
2. **Why `time.monotonic()` instead of wall clock?** Daylight-saving and
   manual clock changes can't fast-forward or rewind a focus block. Wall
   clock is only used when emitting `started_at` / `ends_at` for the UI.
3. **Default focus length?** 25 minutes (classic Pomodoro), with a 5 / 15
   short / long break and a long break every 4 focus blocks. All three
   are tunable from the call site (`start_focus(minutes=…)`).
4. **What counts as "elapsed"?** Auto-transition only happens when
   `snapshot()` ticks past the end time. We don't fire timers proactively
   — the snapshot ticker (every ~2 s) is enough resolution for a focus
   block, and it keeps the state machine purely deterministic.
5. **How do we make the UI tick smoothly?** A 1 s `setInterval` on the
   client interpolates between SSE snapshots. Every snapshot resets the
   reference `ends_at`, so drift can't accumulate.
6. **Why a chime instead of a full notification?** Mac notifications
   require permission, are noisy, and would compete with system focus
   modes. A short in-page sine cue is enough on a wall-mounted dashboard
   the user is glancing at.
7. **Where does the band live so we don't grow the macro grid?** Inside
   `#q-focus`. The existing focus card has spare vertical space below
   `.focus__body`; we just declared a third `auto` grid row for the band
   and let the body keep its 1fr.
8. **What about parallel timers (two focuses, focus + break in parallel)?**
   Out of scope. Personal dashboard, single user, single focus block —
   one in-flight phase at a time keeps the affordance honest.
9. **Reversibility?** Timer ops are not put on the dispatch-history
   undo stack (`reversible: false`). "Undo a stop" or "undo a pause" is
   ambiguous and rarely useful; just issue the inverse verb explicitly.

## File touchpoints

- `src/lifemax/widgets/focus_timer.py`
- `src/lifemax/models.py`
- `src/lifemax/llm.py`
- `src/lifemax/dispatch_history.py`
- `src/lifemax/intents.py`
- `src/lifemax/server/api.py`
- `src/lifemax/bot/telegram_bot.py`
- `src/lifemax/main.py`
- `src/lifemax/server/static/index.html`
- `src/lifemax/server/static/styles.css`
- `src/lifemax/server/static/app.js`
- `tests/test_focus_timer.py`
