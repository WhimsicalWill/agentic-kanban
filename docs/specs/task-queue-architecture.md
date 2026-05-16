# Task Queue Architecture

**Last updated:** 2026-05-16

## Goal

OpenClaw as orchestrator managing Claude Code for task execution. You assign work via WhatsApp → OpenClaw queues tasks → worker executes via Claude Code → you review.

## Pipeline

```
You (WhatsApp)
    ↓
OpenClaw main session (Llama 3.3 70B free via OpenRouter)
    ↓ reads AGENTS.md, runs tasks.py CLI
Task Store (SQLite — /home/opc/task-store/tasks.db)
    ↑
OpenClaw cron job — every 10 min (Llama 3.3 70B free)
    │  makes 2 API calls per tick:
    │  1. exec → runs run_worker.py
    │  2. final text turn → passes output through unchanged
    ↓
run_worker.py (up to 3 parallel threads)
    ↓ loops until queue empty
claude -p (Claude Code) --output-format json --permission-mode bypassPermissions
    ↓ --resume <session_id>  [for revisions]
Task store updated → WhatsApp report delivered via cron announce
```

## Components

### 1. SQLite Task Store
- **File:** `/home/opc/task-store/tasks.db`
- **CLI:** `/home/opc/task-store/tasks.py`
- **Tables:**
  - `tasks` — id, title, description, state, tags, priority, executor, output_summary, session_id, etc.
  - `events` — audit log of all state changes and follow-up prompts
- **States:** `inbox → ready → in_progress → awaiting_review → done / blocked / cancelled`
  - Side paths: `revision_queue` (follow-up queued), `blocked` (≥5 iterations or manual)

### 2. OpenClaw Cron Job
- **Name:** `task-worker-claude-code`
- **Schedule:** Every 10 minutes
- **Session target:** Isolated (no shared state between ticks)
- **Model:** `openrouter/meta-llama/llama-3.3-70b-instruct:free`
  - Fallbacks: `deepseek/deepseek-r1:free` → `google/gemma-3-27b-it:free`
- **Delivery:** `announce` to WhatsApp — sends final text output directly

### 3. Python Worker (`run_worker.py`)
- **File:** `/home/opc/task-store/run_worker.py`
- Spawns up to 3 parallel threads, each draining the queue via `tasks.py next`
- CAS guard in `tasks.py next` prevents double-claiming across threads
- Handles both fresh tasks (first run) and revision tasks (`--resume <session_id>`)
- Stores `session_id` and `output_summary` back to the task after execution
- Moves task to `awaiting_review`; prints a WhatsApp-formatted status report

### 4. Claude Code Executor
- **Command:** `claude -p "<prompt>" --output-format json --permission-mode bypassPermissions`
- **Resume:** `claude -p "<follow_up>" --resume <session_id> --output-format json ...`
- Returns JSON with `result`, `session_id`, `is_error`

## Task State Machine

```
inbox → ready → in_progress → awaiting_review → done
                    ↑                ↓
                    ←← revision_queue ←←

Any state → blocked    (iteration_count >= 5, or manual)
Any state → cancelled  (manual)
```

| State | Who acts | Description |
|---|---|---|
| `inbox` | You | Added but not yet approved to run |
| `ready` | Worker | Approved, waiting for next cron tick |
| `in_progress` | Worker | Claimed by run_worker.py, Claude Code executing |
| `awaiting_review` | You | Claude Code finished; output ready for sign-off |
| `revision_queue` | Worker | Follow-up queued; worker will resume the session |
| `blocked` | You | Hit 5-iteration limit, or blocked manually |
| `cancelled` | — | Manually killed |

**Stale lock protection:** if `in_progress` with `claimed_at` older than 30 minutes, the next worker run reclaims it.

## Cost Model

| Layer | Model | Cost |
|---|---|---|
| Main agent (OpenClaw session) | Llama 3.3 70B free via OpenRouter | $0/token |
| Cron polling (10-min interval) | Llama 3.3 70B free via OpenRouter | $0/token — 144 calls/day |
| Task execution | Claude Code (`claude -p`) | Anthropic Pro subscription |

**OpenRouter free tier:** by default limited to 50 req/day. Adding $10 to the OpenRouter account (balance never depletes on free models) unlocks 1,000 req/day — enough for the 10-min cron plus main-session usage.

**Known OpenClaw bug:** a 429 from one model puts the entire OpenRouter provider into cooldown, blocking all fallbacks. Mitigation: keep API calls per cron tick minimal (currently 2).

## Changelog

### 2026-05-16 — Repo renamed to agentic-kanban; docs reorganized
- Moved architecture doc to `docs/specs/`
- Corrected cost model: orchestration layer uses free OpenRouter models ($0/token)
- Removed hash-based WhatsApp silencing from run_worker.py
- Removed unused worker.sh (superseded by run_worker.py)
- Added pyproject.toml with Ruff linter config
- Added unit tests for tasks.py CLI

### 2026-05-09 — Initial architecture + feedback-driven improvements
- `session_id` column added to tasks table; stored after first Claude Code run
- Worker uses `--resume <session_id>` for revision tasks
- Cron job switched to free OpenRouter model with fallbacks
- Python parallel worker (`run_worker.py`) replaces bash worker
- Voice transcription support (Google free Speech API)

## Known Gaps / TODO
1. Worker doesn't re-check task state mid-execution (external cancellations can be overwritten)
2. No automatic retry on Claude Code crash — requires manual state reset to `ready`
3. No UI for mid-task human input beyond the WhatsApp follow-up flow
4. Long-running / event-driven tasks need a `watching` state and lightweight poller
