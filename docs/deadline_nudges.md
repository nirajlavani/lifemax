# smart deadline nudges + next-due countdown

> Branding rules: see `docs/improvements_log.md`.

## Why

The dashboard already shows individual task pills, but the user has no
single place to glance at and answer "what's the most pressing thing right
now?" The kanban can have 25+ cards and the focus card only ever surfaces
one. Nudges fill the in-between: a colour-coded urgency band on every card
plus one always-visible countdown in the topbar.

It also gives `./bin/lifemax` and Telegram a visible payoff for
high-quality deadlines — every newly created task immediately changes the
tier counts and (often) the headline.

## Scope shipped

- `src/lifemax/widgets/nudges.py` — pure, async-free `compute_nudges`.
  Output shape (mirrors `pick_focus` for predictability):
  ```json
  {
    "next_due": {
      "task_id": "abc",
      "title": "ship the deck",
      "tier": "today",
      "countdown_label": "in 2h",
      "deadline": "2026-04-18T17:00:00-04:00"
    },
    "tier_counts": {"overdue": 2, "today": 4, "soon": 1, "later": 7},
    "task_tiers": {"abc": "today", ...},
    "computed_at": "2026-04-18T15:00:00-04:00"
  }
  ```
- `build_snapshot` calls `compute_nudges` and:
  - emits a new `nudges` block on the snapshot, and
  - decorates every task dict with `tier` so the kanban can colour the
    card edge without re-parsing deadlines on the client.
- Topbar: the date group hosts a new `NEXT · 10` cluster — a coloured
  countdown pill (`nudge--overdue/today/soon/later`), a one-line target
  title underneath it, and an upgraded `date__chip` that breaks down
  "N overdue · M due today" instead of just "M due today".
  - Overdue tier triggers a soft `nudge-pulse` keyframe (~1.6s, low
    opacity halo) so it's noticeable without flashing.
- Kanban cards get a 3px left-edge stripe in the appropriate palette
  token (`--cream` for soon, `--red` for today, `--red-deep` for
  overdue). `later` and `none` keep the existing flat card.
- `widgets/focus.py` and `widgets/nudges.py` use the same
  `_humanize_delta` shape so the focus card and the topbar use the
  same micro-language ("in 2h", "overdue 1d").

## Tier definition

Tasks that are `archived` or `status == done` are forced to `none` and do
not count toward any tier. Everything else is classified by its parsed
deadline in the dashboard's local timezone:

| tier      | rule                                                  |
| --------- | ----------------------------------------------------- |
| `overdue` | `deadline < now`                                      |
| `today`   | `now <= deadline <= 23:59:59 today`                   |
| `soon`    | `today < deadline <= now + 24h` (next-day morning)    |
| `later`   | further than 24h out, but still has a deadline        |
| `none`    | no deadline, or task is archived/done                 |

The headline ("next due") is picked in this order:
1. oldest overdue task (most pain wins),
2. earliest deadline today,
3. earliest deadline within the soon window.

`later` is intentionally excluded from the headline — the topbar should
only nudge when something actually demands attention.

## Branding

- Eyebrow `NEXT · 10`, continuing the per-quadrant numbering scheme
  (`RECENT · 09` is the most recent before this).
- Pill colours come from existing palette tokens:
  - `nudge--later`   → `--ink-dim` on a low-opacity surface
  - `nudge--soon`    → `--cream`
  - `nudge--today`   → `--red` background, `--red-ink` text
  - `nudge--overdue` → `--red-deep` background, `--red-ink` text + halo pulse
- Lower-case copy ("in 2h", "overdue 1d", "all clear today",
  "nothing due"). No emoji, no exclamation marks.
- The accompanying target line uses `--ink-dim` so the pill stays the
  loudest element in the topbar.

## Assumptions I made and answered

- **Q: should the client compute tiers from `task.deadline`?**
  A: No. The server already has the timezone, so it computes tiers and
  ships them per-task. Clock drift on the Mac mini → Mac dashboard is
  mostly invisible (one process), but having the server be the source
  of truth keeps the kanban stripes and the topbar pill perfectly in
  sync.
- **Q: where exactly does the countdown sit?**
  A: In the topbar's date group, immediately under the eyebrow line.
  That's the only spot already in the visual hierarchy where "today"
  data lives, and adding a fourth `topbar__group` would have forced a
  full grid-template rework (we just shipped the consolidated
  clock/date/weather strip).
- **Q: do we ever pulse the kanban cards too?**
  A: No. The card stripe is enough at-a-glance signal; pulsing rows in
  a 25-card list would be visual noise. The pulse is reserved for the
  single topbar pill so it stays meaningful.
- **Q: what about tasks with weird deadlines like
  "2023-10-05T00:00:00"?**
  A: They legitimately classify as `overdue` (we have one in the seed
  data). The countdown label uses `overdue 927d` and that's exactly
  the cue the user needs to either complete it or archive it.
- **Q: per-tier counts vs per-tier task lists?**
  A: Counts only. The kanban already renders the per-tier tasks (just
  filter by `tier`), so duplicating the list in the snapshot adds bytes
  for no UI win.
- **Q: should nudges be reversible?**
  A: They're a *view* of existing tasks, not a side effect, so there's
  nothing to undo. The dispatch history ribbon (improvement 03) handles
  the reversible side of the world.

## File touchpoints

- `src/lifemax/widgets/nudges.py` (new) — pure helper.
- `src/lifemax/server/api.py` — call `compute_nudges`, decorate task
  dicts with `tier`, expose `snap.nudges`.
- `src/lifemax/server/static/index.html` — topbar `nudge-pill`,
  `date__nudge-eyebrow`, `date__nudge-target`.
- `src/lifemax/server/static/styles.css` — `.nudge`, `.nudge--*`,
  `nudge-pulse` keyframe, `.task::before` tier stripe, breakdown chip
  layout.
- `src/lifemax/server/static/app.js` — `renderNudges`, tier-aware
  `makeTaskNode`, `renderToday` upgrade.
- `tests/test_nudges.py` (new) — covers tier classification, headline
  ordering, empty input, naive-now handling.

## Verified end-to-end

- `./bin/lifemax "add nudge smoke test, due today at 11pm, high priority"`
  → snapshot `nudges.tier_counts.today` flips from 0 to 1, the new task
  carries `tier: "today"`.
- `./bin/lifemax "undo"` → tier count returns to 0 immediately (the
  ribbon + countdown both react on the next snapshot tick).
- All 56 unit tests pass; new module covered by 7 dedicated cases.
