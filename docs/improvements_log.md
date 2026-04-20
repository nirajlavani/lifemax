# lifemax · improvements log

A running, ranked list of dashboard improvements. Each item that gets shipped
links to its own design note in this folder, where I record the assumptions I
made and the questions I answered for myself before writing code.

## Branding ground rules

These apply to every entry below so the dashboard keeps a single voice as it
grows. Linked here once and not repeated in each note.

- **Lowercase, italic-serif accents**. Big numbers and section titles use the
  italic-serif treatment already in `styles.css` (`--cream`, `Times New Roman`).
  Eyebrows are uppercase + tracked.
- **Palette tokens only**. New surfaces use `--panel`, `--panel-2`, `--line`,
  `--line-2`. Accents use `--red`, `--cream`, `--blue` from `:root`. No
  one-off hex colours.
- **Voice**. Display text is short, declarative, lower-case. No emoji. No
  exclamation marks.
- **Eyebrow numbering scheme**. Every quadrant has a tracking-uppercase
  eyebrow with a domain code and number, e.g. `FLOW · 01`, `SIGNAL · 02`,
  `DAILY · 03`, `SCHEDULE · 04`. New modules continue the sequence
  (`MOMENTUM · 05`, `INTEL · 06`, ...).
- **Display-only client**. The browser never POSTs back to the server; all
  mutation flows through the LLM via `./bin/lifemax` or Telegram.
- **Snapshot-first data flow**. Anything the UI needs ships inside the SSE
  snapshot from `build_snapshot`. No polling additional endpoints.

## Top 10 improvements (best → last)

1. [Streaks & momentum for the daily checklist](habit_streaks.md) — turn the
   one-day check-off into a multi-day streak ledger so users can see "12 days
   of meditation in a row". High pay-off because it transforms the current
   binary list into something that compounds.
2. [Daily focus block](daily_focus.md) — surface a single "do this now" card
   chosen from urgent + due-today + in-progress tasks. The dashboard becomes
   actionable the moment you glance at it.
3. [Dispatch undo & history feed](dispatch_history.md) — keep the last N
   dispatch outcomes in memory and show them in a faint corner ribbon so the
   user can confirm Telegram/CLI commands took effect, with an `undo` action.
4. [Smart deadline nudges](deadline_nudges.md) — colour-grade the dashboard's
   urgency band (overdue → today → soon → later) and roll a single "next due"
   countdown into the topbar.
5. [Quote of the day rotator](quote_of_day.md) — a small, branded
   typographic card under the topbar that surfaces a curated quote, rotating
   with the news feed cadence. Cheap to ship, big personality lift.
6. [Pomodoro / focus timer](focus_timer.md) — keyboard- and CLI-driven
   timer with audible end chime. Adds a true productivity primitive without
   breaking display-only browser rules.
7. [Weekly retro snapshot](weekly_retro.md) — a Sunday-evening LLM-generated
   recap of completed tasks, missed deadlines, and habit consistency, posted
   into the dashboard and Telegram.
8. [Health vitals badge](health_badge.md) (`VITALS · 08`, shipped) —
   five-dot strip in the top-right corner reports the live tier of the
   task store, habit store, weather feed, Apple Calendar, and the LLM
   round-trip. Worst tier tints the whole pill so a glance across the
   room tells you whether anything needs attention. Read-only, no new
   polling — every probe piggybacks on existing snapshot work, with a
   matching read-only `GET /api/health` for monitoring scripts.
9. Markets glance — a compact strip with the user's tracked tickers / crypto
   and 24h % change. (Not built in this pass; deferred because it requires a
   reliable keyless quote source.)
10. AI workspace inbox — pull last 5 unread emails (read-only) plus a
    one-line LLM summary. (Not built in this pass; deferred because it
    requires Google/Outlook OAuth, which is out of scope for a single-user
    Mac mini today.)

## Notes per improvement

- Each shipped improvement has its own `docs/<name>.md` file with assumptions
  and self-answered questions.
- Improvements 1–8 are implemented in this pass. 9 and 10 are intentionally
  parked with rationale.
