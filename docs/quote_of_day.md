# quote of the day rotator

> Branding rules: see `docs/improvements_log.md`.

## Why

The dashboard is the first thing the user sees in the morning and the last
thing it shows at night. A small, branded typographic line gives the screen
personality without spending real estate from the 1600 × 1066 grid. Quotes
are curated, deterministic, and offline — no external feed, no jitter on
reload, no LLM cost.

## Scope shipped

- `data/quotes.json` — 70+ curated entries, each `{text, attribution}`.
  Validated on load; bad records are skipped with a warning, and the loader
  falls back to a single embedded Will Durant quote so the ribbon never
  goes empty.
- `src/lifemax/widgets/quotes.py` — pure helper module:
  - `_load_quotes` reads + validates the file once, then memoises in-memory.
  - `_slot_index` deterministically picks an index from a SHA-256 hash of
    `(local_date, slot)`. Same date + slot ⇒ same quote across reloads.
  - `QuoteRotator.pick_for(local_date, slot=…)` returns a snapshot-shaped
    dict (`text`, `attribution`, `slot`, `slots_per_day`, `date_iso`,
    `total`).
- `src/lifemax/config.py`
  - `QUOTES_FILE = DATA_DIR / "quotes.json"`.
  - `QUOTES_PER_DAY = 4` so each local day has 4 distinct picks the rotator
    cycles through (one per ~6h slot).
- `src/lifemax/server/api.py`
  - `build_snapshot` accepts an optional `quotes: QuoteRotator | None`,
    derives `slot = now.hour // 6`, calls `pick_for`, and emits a `quote`
    block in the SSE snapshot.
  - `create_app` instantiates a `QuoteRotator` if none is passed and stores
    it on `app.state.quote_rotator`. All four `build_snapshot` callsites
    (ticker, change-publish, `/api/state`, `/api/stream`, post-dispatch)
    pass it through.
- `src/lifemax/main.py` — instantiates one shared `QuoteRotator` so the
  in-process cache is hit across the API, the SSE ticker, and the bot.
- Frontend
  - `index.html` — new `<aside id="quote">` next to the recent ribbon, with
    eyebrow `QUOTE · 05` and a 2-line `<blockquote>` body.
  - `styles.css` — bottom-right glass card mirroring the recent ribbon
    (same `var(--panel)` background, `var(--line)` border, blur),
    `--cream`-coloured curly quotes, italic body, all-caps cream
    attribution with an em-dash prefix.
  - `app.js` — `renderQuote(snap.quote)` swaps text/attribution via
    `setText` (no `innerHTML`); empty/missing payloads hide the card.

## Snapshot shape

```
{
  "quote": {
    "text": "We don't rise to the level of our goals; we fall to the level of our systems.",
    "attribution": "James Clear",
    "slot": 2,
    "slots_per_day": 4,
    "date_iso": "2026-04-19",
    "total": 77
  }
}
```

`text` and `attribution` are the only fields the UI consumes today. The
remaining keys are diagnostic and useful for tests + future "next quote"
affordances.

## Branding

- Eyebrow `QUOTE · 05` continues the eyebrow numbering (focus 00, flow 01,
  signal 02, daily 03, schedule 04). Cream curly quotes (`\201c` / `\201d`)
  flank the italic body, attribution renders uppercase + tracked + cream
  (`var(--cream)`). All colours come from `:root` tokens — no one-offs.
- The ribbon is non-interactive (`pointer-events: none`) and overlays the
  `#stage` layer, so the 1600 × 1066 macro grid is untouched.
- Two-line clamp keeps even very long quotes inside one consistent height
  band; the rotator naturally avoids dramatic layout shift between picks.

## Assumptions / questions I answered for myself

1. **Where does the quote live in the UI?** Topbar would push the date /
   weather group around, and the right column is already paying for its
   slot with the AI feed + daily list. Bottom-right corner mirrors the
   recent dispatch ribbon (bottom-left), so the two overlays form a quiet
   "footer band" without touching the grid.
2. **How many distinct quotes per day?** Four (`QUOTES_PER_DAY = 4`), one
   per six-hour slot. Enough to feel fresh through the day, few enough that
   you might actually re-encounter and remember a great one.
3. **Why deterministic picking?** A random pick per request would jitter on
   every SSE refresh (every 2 s). Hashing `(local_date, slot)` keeps the
   selection stable across reloads, ticker pulses, and even server
   restarts; only the wall clock advancing into a new slot rotates the
   pick.
4. **Why curated + offline?** The dashboard is meant to keep working when
   the network or OpenRouter is flapping. A static JSON file with embedded
   fallback means the card is always populated, costs nothing per refresh,
   and is easy to extend by hand.
5. **Should the LLM curate or rewrite quotes?** No. We trust hand-picked
   sources for tone / accuracy. The LLM stays focused on tasks, events,
   habits, and undo — the surfaces it can mutate.
6. **What if the file is missing or malformed?** `_load_quotes` returns the
   embedded fallback (one Will Durant line) and logs a warning. Same for
   files whose root isn't a list, or whose entries fail validation.
7. **Why not a fade animation?** The ribbon is meant to be quiet. Slot
   changes happen at 06:00 / 12:00 / 18:00 / 00:00 local — moments the user
   is unlikely to be staring at the screen — so a hard swap is fine and
   keeps the ribbon code minimal. We can layer a 250 ms cross-fade later if
   it ever feels too abrupt.
8. **Could it be cycled by the user?** Eventually yes (e.g. `./bin/lifemax
   "next quote"`), but that's a future improvement; the current rotator is
   purely time-driven and read-only, matching the display-only model.

## File touchpoints

- `data/quotes.json`
- `src/lifemax/config.py`
- `src/lifemax/widgets/quotes.py`
- `src/lifemax/server/api.py`
- `src/lifemax/main.py`
- `src/lifemax/server/static/index.html`
- `src/lifemax/server/static/styles.css`
- `src/lifemax/server/static/app.js`
