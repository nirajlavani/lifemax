# daily focus block

> Branding rules: see `docs/improvements_log.md`.

## Why

The kanban shows everything; a glance has to scan three lanes to figure out
"what should I actually do *now*?". A single "focus" card removes that
question.

## Scope shipped

- `src/lifemax/widgets/focus.py` — pure ranking helper
  `pick_focus(tasks, *, timezone_name, now)` returns the single best
  candidate from open tasks, plus a short reason string ("urgent · due in
  2h", "high priority · overdue 1d", etc.).
- `build_snapshot` adds a `focus` block to the SSE payload that includes the
  chosen task plus the next 2 runner-up titles, so the UI can show
  "after this: …".
- New top-of-topbar pill on the date column ("focus · <title>") to keep the
  user pointed at one thing.
- A new full-width section embedded above the kanban + calendar
  in the left column, keeping the existing 1600x1066 grid intact (we steal
  ~88px from the kanban quadrant by dropping its grid weight from 1.1fr to
  1fr, and giving the focus card its own row above).

## Ranking rules

Lower score wins. Ties broken by deadline ascending, then `created_at`.

1. Status `done` or `archived` → excluded.
2. Overdue tasks: score = `−1000 + days_overdue * −10`. (i.e. extremely
   negative, so they always rise.)
3. Due-today tasks: `0` if urgent, `+5` if high, `+10` otherwise.
4. Future tasks within 24h: `+15` if urgent, else `+25`.
5. In-progress tasks (any deadline): `+30`. We slightly prefer to keep
   momentum on existing in-progress work.
6. Everything else: `+100 + priority_offset` (high=0, medium=10, low=20).
7. No deadline + low priority: `+999`.

## Assumptions I made and answered

- **Q: do we filter to only `todo` + `in_progress`?**  
  A: Yes. Done/archived tasks are excluded.
- **Q: where does it live visually?**  
  A: Compact strip above kanban. Big italic title, small reason chip,
  small "after this: …" line. Mirrors the typographic feel of the topbar.
- **Q: what if there are no open tasks?**  
  A: Empty state: "no open tasks · enjoy the silence". Same lower-case
  voice as `daily__empty`.
- **Q: should the user be able to override?**  
  A: Out of scope for now. The display-only contract means any override
  would have to flow back through dispatch. For v1 we trust the ranker.

## File touchpoints

- `src/lifemax/widgets/focus.py` (new)
- `src/lifemax/server/api.py` — call `pick_focus` and embed in snapshot.
- `src/lifemax/server/static/index.html|styles.css|app.js`
- `tests/test_focus.py` (new)
