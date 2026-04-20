# streaks & momentum for the daily checklist

> Branding: see `docs/improvements_log.md`. This note records the assumptions
> and self-answered questions that shaped the implementation.

## Why

The daily checklist already auto-resets at 3am, but each day is treated as
independent. Compounding behaviour ("12 days of meditation in a row") is the
single biggest reason people stick with a daily list, so we promote streaks
to a first-class concept.

## Scope shipped

- `Habit` gains a small append-only history (`completed_dates: list[str]`)
  capped at the last 60 entries.
- `HabitStore.mark_done` / `undo_done` keep the legacy
  `last_done_local_date` for backwards compatibility but also maintain the
  history list.
- A new `compute_streaks(local_today_iso, cutoff_hour)` helper derives
  `current_streak`, `best_streak`, and `done_in_last_7` per habit from the
  history (no new fields persisted that can drift).
- The API snapshot exposes `current_streak`, `best_streak`,
  `done_last_7` per item, plus `top_streak` summary on the habits payload.
- The UI shows a small streak badge on each daily tile (e.g. `12d`) and a
  faint 7-dot history strip.

## Assumptions I made and answered

- **Q: store full history forever?**  
  A: No. 60 entries is enough to render any reasonable streak (current +
  best + 7-day strip) without bloating the JSON file. If a user really hits
  a 60+ day streak, we still preserve `best_streak` by computing it before
  truncation and persisting it as `best_streak_cached`.
- **Q: what counts as "today" for a streak?**  
  A: Same `habit_day_in_tz` rule used for the existing checkbox. A streak
  remains "alive" if the most recent completion is `today_iso` *or*
  `yesterday_iso`. If today isn't yet checked but yesterday was, we still
  show the current streak (with no warning); the moment we cross
  `today_iso + 1 day` without a check, the current streak resets to 0.
- **Q: where does best-streak live?**  
  A: Computed from history *and* compared against a cached
  `best_streak_cached` field, so old completions that fall out of the
  60-entry window can never silently lower the displayed best.
- **Q: client- or server-derived?**  
  A: Server. Keeps the wire format trivial and avoids the client and the
  server disagreeing about today.

## File touchpoints

- `src/lifemax/models.py` — `Habit` adds `completed_dates`,
  `best_streak_cached`. `is_done_for` unchanged.
- `src/lifemax/habits_store.py` — write paths append/dedupe history and
  refresh `best_streak_cached`.
- `src/lifemax/habit_streaks.py` (new) — pure helpers that compute streak
  numbers from a list of ISO dates.
- `src/lifemax/server/api.py` — snapshot includes per-habit streak figures
  and a habits-level `top_streak` summary.
- `src/lifemax/server/static/index.html|styles.css|app.js` — render badge
  + 7-day history strip on each daily tile.
- `tests/test_habits.py` — new cases for streak math and history pruning.
