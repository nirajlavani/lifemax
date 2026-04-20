# health vitals badge strip

> Branding rules: see `docs/improvements_log.md`.

## Why

A wall-mounted dashboard that fails silently is worse than one that
doesn't exist — you trust it for days before noticing the calendar hasn't
refreshed since the EventKit permission was revoked, or that OpenRouter
has been timing out for hours. The dashboard has plenty of side-channels
that tell the whole story (logs, exceptions, fallback cards), but none of
them speak in a single glance.

The vitals badge strip answers one question: *is everything still alive?*
Five tiny coloured dots in the top-right corner — one each for the task
store, the habit store, the weather feed, Apple Calendar, and the LLM —
let the user read the system's pulse without leaving the page. Worst-tier
wins the strip's outline tint, so a degraded or down state shows up
across the room without reading text.

## Scope shipped

- `src/lifemax/widgets/health.py` — pure composition module. Defines
  `HealthMonitor`, the only stateful piece (it owns the LLM probe). The
  rest is functions that turn duck-typed subsystem signals into badge
  dicts. No new background polling: every badge piggybacks on work the
  snapshot ticker already does.
- `src/lifemax/store.py` and `src/lifemax/habits_store.py`
  - Three new attributes per store: `last_io_at` (monotonic clock),
    `last_load_error`, `last_save_error`. Set inside the existing
    `_load_locked` / `_save_locked` paths; the swallow-and-continue
    contract on bad reads is preserved (the dashboard still renders).
  - Failed writes now stash the exception text on `last_save_error` and
    re-raise so callers see the failure, while the badge composes from
    the same field on the next snapshot.
- `src/lifemax/widgets/weather.py`
  - Same pattern: `last_fetch_at` and `last_fetch_error`. Set on both
    success and the existing fallback path so the badge can distinguish
    "never fetched" (`unknown`) from "fetched but Open-Meteo errored"
    (`degraded` / `down` once stale).
- `src/lifemax/server/api.py`
  - New imports for `HealthMonitor`.
  - `build_snapshot` accepts `health: HealthMonitor | None`, builds the
    `health` payload after the weather/calendar reads, and ships it on
    every SSE frame.
  - `create_app` default-constructs a `HealthMonitor` and threads it
    through every callsite (ticker, change-publish, `/api/state`,
    `/api/stream`, post-dispatch) so the page sees the same monitor the
    bot writes to.
  - `/api/dispatch` records the LLM round-trip outcome on the monitor
    (`record_llm_ok` on success, `record_llm_error` on failure) before
    raising the existing 502. Both calls are best-effort — observability
    never breaks dispatch.
  - New read-only `GET /api/health` endpoint returns the same payload
    `snap.health` carries. Useful for monitoring scripts (launchd
    keep-alive, command-line probes).
- `src/lifemax/main.py` instantiates one shared `HealthMonitor` and
  passes it to both `create_app` and `build_bot` so the API and the
  Telegram bot feed the same probe.
- `src/lifemax/bot/telegram_bot.py` records the LLM round-trip outcome
  on the shared monitor inside `_route_text`. Same best-effort semantics
  as the API path.
- Frontend
  - `index.html` — new `<aside id="health" class="health" hidden>` next
    to the existing overlays, with a `VITALS · 08` eyebrow and a
    `<ul id="health-row">` for the badges.
  - `styles.css` — `.health` is a slim pill anchored top-right of
    `#stage` (`top: 12px; right: 14px`) with the same glass aesthetic
    as the other overlays. Badges use only palette tokens:
    `--cream` for `ok`, `--blue` for `degraded`, `--red` for `down`,
    desaturated cream for `unknown`. The strip's outline picks up the
    worst tier (`.health--down` or `.health--degraded`) so the whole
    pill blushes when something needs attention.
  - `app.js` — `renderHealth(snap.health)` shows / hides the strip,
    rebuilds the badges with `replaceChildren` + `setText` (no
    `innerHTML`, no XSS surface), and stores the full message + age
    in each badge's `title` for hover detail.

## Snapshot shape

```
{
  "health": {
    "tier": "down",                       // worst of the badges below
    "computed_at_wall": 1776623598.822,
    "badges": [
      { "key": "store",    "label": "tasks",    "tier": "ok",       "message": "writes ok",        "age_seconds": 28.6 },
      { "key": "habits",   "label": "habits",   "tier": "ok",       "message": "writes ok",        "age_seconds": 28.6 },
      { "key": "weather",  "label": "weather",  "tier": "ok",       "message": "open-meteo ok",    "age_seconds": 27.0 },
      { "key": "calendar", "label": "calendar", "tier": "down",     "message": "eventkit · …",     "age_seconds": null },
      { "key": "llm",      "label": "llm",      "tier": "ok",       "message": "openrouter · …",   "age_seconds": 7.3 }
    ]
  }
}
```

`badges == []` (or the whole block missing) ⇒ the UI hides the strip.

## Branding

- Eyebrow `VITALS · 08` matches the `IMPROVEMENT · NN` numbering
  established in `docs/improvements_log.md`.
- Strip is a fixed pill at the top-right of `#stage`, never inside a
  quadrant — the macro grid is unchanged.
- Tier colours are pulled from the existing palette tokens only:
  `--cream` (ok), `--blue` (degraded), `--red` (down), desaturated
  cream (unknown). No one-off hex colours; the same accents are
  already used elsewhere (kanban dots, deadline nudges).
- Each dot is 7 px with a faint inner ring and a soft glow when active,
  so the row reads as a heartbeat rather than a checkbox row.
- Voice on the badges stays display-grammar consistent: lowercase,
  short ("tasks", "habits", "weather", "calendar", "llm"). Full
  context lives in the tooltip.
- Audio: none. The badge is silent on purpose — a wall display that
  beeps every time OpenRouter sneezes would be intolerable.

## Assumptions / questions I answered for myself

1. **Where do the health signals live?** On the subsystems themselves.
   Stores already had load/save paths; weather already had a fetch
   path. Adding three small attributes (`last_io_at`,
   `last_load_error`, `last_save_error`) on each store and two
   (`last_fetch_at`, `last_fetch_error`) on the weather widget keeps
   the subsystems self-describing without a new global registry.
2. **Why no synthetic LLM heartbeat?** A periodic ping would burn
   tokens and wouldn't catch the real failure modes (rate limits,
   model-specific errors, token/key issues). Recording the *last real*
   round-trip on every dispatch is honest: if the user just used it
   successfully, the badge says so; if it failed, the badge tells
   them which way it failed.
3. **What about the news feed?** Skipped. The news widget already
   degrades gracefully (curated list shrinks to whatever feeds
   responded), and "today's headlines are stale" doesn't materially
   affect the trustworthiness of the dashboard the way a stale
   calendar or failing LLM does. If we ever want it, the same
   `last_fetch_at`/`last_fetch_error` pattern drops in.
4. **Why a top-right pill instead of inline in the topbar?** The
   topbar already carries date / time / weather / location and has
   no spare horizontal real estate at 1600 × 1066. The pill sits in
   the historically empty corner and matches the bottom-strip
   triptych (ribbon · retro · quote) so the four overlays read as
   one consistent layer of metadata.
5. **What's the freshness threshold?** Per-subsystem and intentionally
   generous: 5 min for stores, 30 min for weather, 15 min for
   calendar, 24 h for the LLM (which is on-demand, so a long quiet
   period isn't a fault). Any subsystem older than its window falls
   to `degraded` even on a successful last call — picks up the case
   where the probing loop has stalled silently.
6. **Why an `unknown` tier?** Weather and LLM are both lazy: they
   may not have been touched yet on a fresh boot. Painting them as
   `down` would be a lie; `unknown` is the right rendering until we
   have data.
7. **Why not show the calendar permission text inside the badge?**
   It's long and shouty. The dot + the word "calendar" is enough
   for the glance; the full instruction lives in the tooltip and
   the existing `calendar_status` notice in the calendar quadrant.
8. **Reversibility?** Read-only by construction — there's nothing to
   undo. The monitor is pure observability; no side effects.

## File touchpoints

- `src/lifemax/widgets/health.py`
- `src/lifemax/store.py`
- `src/lifemax/habits_store.py`
- `src/lifemax/widgets/weather.py`
- `src/lifemax/server/api.py`
- `src/lifemax/main.py`
- `src/lifemax/bot/telegram_bot.py`
- `src/lifemax/server/static/index.html`
- `src/lifemax/server/static/styles.css`
- `src/lifemax/server/static/app.js`
- `tests/test_health.py`
