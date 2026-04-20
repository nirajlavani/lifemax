# Lifemax Dashboard

A continuously-running personal "life maxing" dashboard for a Mac mini.

- Display-only web UI tuned for **1600 x 1066** (no sidebars, no UI controls).
- 2x2 collage layout: Kanban board, Eisenhower matrix, ambient info (time / weather), AI news feed.
- Two input channels — both delegate parsing to a cheap **OpenRouter** model with strict JSON-schema output:
  - **Telegram bot** (allowlisted to your user id).
  - **Claude dispatch / CLI bridge** (`bin/lifemax "..."`) that POSTs to the local server.
- Tasks are stored as **JSON** at `data/tasks.json`. Tasks are never deleted; "delete" means `archived: true`.
- Weather via **Open-Meteo** (no key) at the IP-detected location; AI news via curated RSS.
- Process supervised by **launchd** so it boots with the Mac mini and restarts on crash.

## Layout

```
+-------------------+-------------------+
|     Kanban        |    Eisenhower     |
|  (todo / wip /    |  (urgent vs       |
|   done)           |   important)      |
+-------------------+-------------------+
|  Ambient          |   AI News Feed    |
|  (time, weather,  |   (X-link items   |
|   today count)    |   badged)         |
+-------------------+-------------------+
```

## Quick start

```bash
cd lifemax_dashboard
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, OPENROUTER_API_KEY,
# and generate a LIFEMAX_DISPATCH_TOKEN (instructions in the file).

python -m lifemax.main
```

Then open `http://127.0.0.1:8765/` in a browser. To use the CLI bridge from any
terminal:

```bash
./bin/lifemax "buy milk by tomorrow 5pm, urgent"
./bin/lifemax "what are today's goals?"
```

## Telegram setup

1. Talk to **@BotFather** on Telegram, create a bot, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Talk to **@userinfobot** to get your numeric user id, copy it into `TELEGRAM_USER_ID`.
3. Restart the service. Anyone not in the allowlist is silently ignored.

You can DM the bot in plain English:

- "Add: write the Q2 plan, due Friday, urgent and important"
- "Mark the gym task done"
- "What are today's goals?"
- `/today` shortcut for today's goals
- `/list` shortcut for all open tasks

## Claude dispatch

`bin/lifemax` is a tiny Python client that POSTs to `http://127.0.0.1:8765/api/dispatch`
with the shared `LIFEMAX_DISPATCH_TOKEN` header. Invoke it from any terminal,
including from inside a Claude Code session:

```bash
./bin/lifemax "schedule deep-work block for tomorrow morning, important"
```

If you want a `.claude/commands/task.md` slash-command in another project, drop in:

```md
---
description: Add or update a lifemax task
---
Run: `/Users/nirajlavani/Documents/small_proj/lifemax_dashboard/bin/lifemax "$ARGUMENTS"`
```

## Run continuously on a Mac mini

```bash
# Edit launchd/com.lifemax.dashboard.plist if your repo path differs.
ln -sf "$PWD/launchd/com.lifemax.dashboard.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lifemax.dashboard.plist
# To stop:
launchctl unload ~/Library/LaunchAgents/com.lifemax.dashboard.plist
```

Logs go to `data/logs/lifemax.out.log` and `data/logs/lifemax.err.log`.

## Security notes

- Server binds to `127.0.0.1` only.
- `POST /api/dispatch` requires the `X-Lifemax-Token` shared-secret header.
- Telegram updates outside the allowlist are dropped before any LLM call.
- All secrets live in `.env` (gitignored). Nothing is hardcoded.
- Logs redact token-shaped strings and use a hash of Telegram user ids.

## Tests

```bash
pytest -q
```
