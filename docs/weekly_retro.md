# weekly retro snapshot endpoint + Sunday card

> Branding rules: see `docs/improvements_log.md`.

## Why

The dashboard is excellent at *now* — what's next, what's overdue, the
current focus block — but it had no memory of the week behind. On a wall
display this matters: the Sunday glance should feel like a closing
chapter, not a generic Monday-morning state. A weekly retro card answers
"what did I actually do this week?" in one breath, without firing up a
notebook or a separate dashboard.

The card is intentionally Sunday-only. Six days a week the macro grid
stays untouched; on Sundays a third bottom-strip overlay slides in
between the dispatch ribbon (left) and the quote (right) so the page
reads as a triptych: *what just happened · what to remember · what
inspires me*.

## Scope shipped

- `src/lifemax/widgets/retro.py` — pure aggregation module (no I/O,
  no globals). Public surface:
  - `WEEK_LENGTH_DAYS = 7`
  - `local_habit_date_for(now, tz_name)` — same 3 AM cutoff as the
    daily-habit logic, but `now`-aware so tests stay deterministic.
  - `date_range_iso(end_iso, days=7)` — inclusive sliding window of
    ISO date strings ending on `end_iso`.
  - `compute_weekly_retro(tasks, habits, focus_blocks_per_day,
    timezone_name, now)` — returns the structured rollup shown
    below.
- `src/lifemax/widgets/focus_timer.py`
  - New `_blocks_per_day: dict[str, int]` keyed by local habit date.
  - `_tick_locked` increments the counter when a focus phase elapses
    (`elapsed` event). Capped at 60 entries via `_evict_block_history`
    so an always-on Mac mini doesn't grow unbounded.
  - New `focus_blocks_per_day(date_isos)` async accessor for the
    retro builder. Pure read; no state mutation.
- `src/lifemax/server/api.py`
  - New imports for `compute_weekly_retro`, `date_range_iso`,
    `local_habit_date_for` are hoisted to the top of the file.
  - `build_snapshot` builds a `retro` payload alongside the existing
    `timer` payload. It uses the *non-archived* task list the
    snapshot already loads, so SSE stays cheap.
  - New read-only `GET /api/retro/weekly` endpoint returns the same
    payload but loads tasks `include_archived=True`. Intentional split:
    the bottom-strip card is a "what shipped this week" headline, the
    endpoint is a complete record (useful for the Telegram bot or a
    future weekly digest).
- Frontend
  - `index.html` — new `<aside id="retro" class="retro" hidden>` next
    to the existing `#ribbon` and `#quote` overlays. Three regions:
    eyebrow + title, three big stats (tasks shipped, habit %, focus
    blocks), a 7-bar daily strip, and a single-line caption.
  - `styles.css` — `.retro` block matches the glass aesthetic of
    `.ribbon` / `.quote`: `rgba(20,20,20,0.92)` background,
    `--line` border, `--r-md` radius, `backdrop-filter: blur(6px)`.
    Anchored bottom-center via `left: 50%; transform: translateX(-50%)`
    so it slots between the two existing overlays. Width capped at
    `min(620px, 38vw)`. The 7 daily bars share a CSS custom property
    `--retro-fill` so JS only writes a single percentage per bar; the
    relative-to-peak heights make a single big day legible without
    saturating the strip.
  - `app.js` — `renderRetro(retro)` does the work:
    - Hides the card unless `retro.is_sunday === true`.
    - Sets the three stats with `safeText` (no `innerHTML`).
    - Builds 7 bars from `retro.daily`, marks today with
      `retro__bar--today`, and exposes a per-bar `title` tooltip
      (`tasks t · habits h · focus f`) for the curious.
    - Picks one of three caption forms based on what the data
      actually has (focus blocks → completed task title → top
      habit → "no notable runs").
- `tests/test_retro.py` — 8 cases covering the date helpers
  (3 AM cutoff, default + custom window length), the rollup
  (completed-in-window, created-in-window, archived counted in
  `created`), the habit math (rate, top habit, archived ignored),
  the focus best-day pick, the daily-strip cardinality, and the
  `is_sunday` flag flipping for Saturday.

## Snapshot shape

```
{
  "retro": {
    "window_start": "2026-04-13",
    "window_end":   "2026-04-19",
    "today_local_date": "2026-04-19",
    "is_sunday": true,
    "tasks": {
      "completed": 3,
      "created":   7,
      "archived":  1,
      "completed_titles": ["ship docs", "ship code", "plan q3"]
    },
    "habits": {
      "completions": 11,
      "possible": 14,
      "completion_rate": 0.786,
      "top_habit": "exercise",
      "top_habit_count": 5
    },
    "focus": {
      "blocks_total": 12,
      "best_day":     "2026-04-15",
      "best_day_blocks": 5
    },
    "daily": [
      { "date": "2026-04-13", "tasks_done": 0, "habits_done": 1, "focus_blocks": 1 },
      … 5 more …
      { "date": "2026-04-19", "tasks_done": 1, "habits_done": 2, "focus_blocks": 4 }
    ]
  }
}
```

`is_sunday === false` ⇒ the UI hides the card. The payload is still
emitted every snapshot so a future "Monday recap" or Telegram digest
can read it without adding more endpoints.

## Branding

- Eyebrow `RETRO · 07` matches the `IMPROVEMENT · NN` numbering
  established in `docs/improvements_log.md`.
- Title is the dashboard's serif voice (`"Times New Roman"`, slight
  negative tracking) with one italic word for emphasis: *this week*.
- Big numbers use Times-italic numerals with `font-variant-numeric:
  tabular-nums` so the card doesn't reflow when a digit changes.
- Stat labels keep the dashboard's uppercase + 0.18em tracking grammar.
- Bars are rendered with `--cream` at 78 % opacity over a faint cream
  fill — same palette as the focus countdown band, no one-off colours.
- Caption uses `--cream` + uppercase + 0.16em tracking, same energy as
  the deadline-nudge subtitles.

## Assumptions / questions I answered for myself

1. **Where does focus-block history live?** In-process, in `FocusTimer`,
   keyed by local habit date and capped at 60 entries (~2 months on a
   24/7 dashboard). Persistence felt over-engineered for a single-user
   Mac mini and would have required schema work; if I want true
   long-term retro later, I'll persist this map on shutdown. For now,
   "the week is what you remember since the last reboot" is honest and
   matches the rest of the timer's design (also non-persistent).
2. **Should the snapshot include archived tasks in the rollup?**
   No — the SSE snapshot keeps the same "active tasks only" view it
   already uses, so the bottom-strip card matches the kanban it sits
   under. The dedicated `/api/retro/weekly` endpoint loads everything
   so a Telegram digest can speak to the full record.
3. **What window?** Last 7 *local habit days* including today. Aligned
   with the daily-habit cutoff so Sunday's "this week" doesn't mean
   different things to the habit logic and the retro card.
4. **Why bars instead of sparklines?** Bars are legible at the
   dashboard's typography scale and they encode three sources at once
   (tasks + habits + focus blocks summed, with a tooltip breakdown).
   A sparkline would have looked great but lost the per-day breakdown.
5. **What if there's nothing to celebrate?** The caption gracefully
   degrades: focus blocks → last-shipped task → top habit → "no
   notable runs this week". The card never lies and never disappears
   on a Sunday — even an empty week is a useful reflection.
6. **Where does the card live so we don't grow the macro grid?**
   It's a `position: fixed` overlay anchored to `#stage` (same as the
   ribbon and quote) and only visible when the server says it's a
   Sunday. The macro grid stays exactly the same six days a week.
7. **Why only Sundays?** A weekly retro that lives on the screen all
   week stops being a moment. Sunday is the natural "close the book"
   day; Monday will read the same data as "previous week" if we ever
   want a dedicated card for that.
8. **Reversibility?** Read-only by construction — there's nothing to
   undo. The retro module is pure aggregation; no writes anywhere.

## File touchpoints

- `src/lifemax/widgets/retro.py`
- `src/lifemax/widgets/focus_timer.py`
- `src/lifemax/server/api.py`
- `src/lifemax/server/static/index.html`
- `src/lifemax/server/static/styles.css`
- `src/lifemax/server/static/app.js`
- `tests/test_retro.py`
