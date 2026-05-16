# Task Queue Architecture

**Last updated:** 2026-05-09

## Goal

OpenClaw as an orchestrator managing Claude Code for task execution. You assign work via WhatsApp → I create tasks → Claude Code executes them → you review.

## Pipeline

```
You (WhatsApp) → Me (main agent, OpenRouter DeepSeek V4 Flash) 
  → SQLite task queue → Cron job → Isolated agent
  → worker.sh → claude -p (Claude Code) → awaiting_review → done
```

## Components

### 1. SQLite Task Store
- **File:** `/home/opc/task-store/tasks.db`
- **CLI:** `/home/opc/task-store/tasks.py`
- **Tables:**
  - `tasks` — id, title, description, state, tags, priority, executor, output_summary, session_id, etc.
  - `events` — audit log of all state changes
- **States:** `inbox → scoping → ready → in_progress → awaiting_review → done/blocked/cancelled`

### 2. OpenClaw Cron Job
- **Name:** `task-worker-claude-code`
- **Job ID:** `b6ce417a-1d39-4507-a7d5-7075ae44a470`
- **Schedule:** Every 2 minutes
- **Session target:** Isolated
- **Delivery:** None (no WhatsApp spam on empty polls)
- **Cost:** ~$0.002/run (OpenRouter)

### 3. Worker Script
- **File:** `/home/opc/task-store/worker.sh`
- Picks up `ready` tasks → marks `in_progress` → generates UUID session → runs `claude -p` with `--session-id`
- **Continuous prompting loop:** If Claude Code output matches patterns like "would you like…", "do you want…", "ask me if…", or is very short (<3 lines), worker re-prompts by resuming the same session (up to 5 continuations)
- Stores result → marks `awaiting_review`
- Session ID stored in DB for future resume with `claude -r <session-id>`

### 4. Voice Transcription
- **Method:** Google's free Speech API (public Chromium key)
- **Required tools:** ffmpeg (static binary at `/tmp/ffmpeg`), requests (Python)
- **Cost:** Free

## Costs Breakdown
| Layer | Model | Cost |
|-------|-------|------|
| Main agent (me) | DeepSeek V4 Flash via OpenRouter | Pay-as-you-go, ~few cents/day |
| Cron polling | OpenRouter model (isolated session) | ~$0.002/run, ~$1.44/day at 2min |
| Task execution | Claude Code (claude -p) | Anthropic Pro subscription |

## Changelog

### 2026-05-09 — Initial architecture + feedback-driven improvements
- Added `session_id` column to tasks table
- Worker now generates a UUID and passes `--session-id` to Claude Code
- Continuous prompting loop: if Claude asks for input or returns very short output, worker resumes the session (up to 5 continuations)
- Session ID stored in DB for future resume capability (`claude -r <session-id>`)
- Cron job switched to free OpenRouter model: `meta-llama/llama-3.3-70b-instruct:free` with fallbacks to Gemma 3 27B and Gemini Flash 1.5 8B
- Voice transcription added (Google free Speech API)
- Architecture documented in `memory/task-queue-architecture.md`

## Known Gaps / TODO
1. When resuming a task with human input, need UI/mechanism to provide that input (currently manual)
2. Worker doesn't write to `output_summary` column (only event notes)
3. No retry on Claude Code crash (stays in in_progress)
4. Could add free model rotation logic when rate-limited
