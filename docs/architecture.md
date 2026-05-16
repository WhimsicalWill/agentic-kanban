# Task Queue Architecture

## Goal

OpenClaw as orchestrator managing Claude Code for task execution. You assign work via WhatsApp → OpenClaw queues tasks → worker executes via Claude Code → you review.

## Pipeline

```
You (WhatsApp)
    ↓
OpenClaw main session (gpt-oss-120b free via OpenRouter, +3 fallbacks)
    ↓ reads AGENTS.md, runs tasks.py CLI
Task Store (SQLite — /home/opc/agentic-kanban/tasks.db)
    ↑
OpenClaw cron job — every 10 min (gpt-oss-120b free, +3 fallbacks)
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
- **File:** `/home/opc/agentic-kanban/tasks.db`
- **CLI:** `/home/opc/agentic-kanban/tasks.py`
- **Tables:**
  - `tasks` — id, title, description, state, tags, priority, executor, output_summary, session_id, etc.
  - `events` — audit log of all state changes and follow-up prompts
- **States:** `inbox → ready → in_progress → awaiting_review → done / blocked / cancelled`
  - Side paths: `revision_queue` (follow-up queued), `blocked` (≥5 iterations or manual)

### 2. OpenClaw Cron Job
- **Name:** `task-worker-claude-code`
- **Schedule:** Every 10 minutes
- **Session target:** Isolated (no shared state between ticks)
- **Model chain:**
  1. `openai/gpt-oss-120b:free` — primary (confirmed working)
  2. `minimax/minimax-m2.5:free` — confirmed working
  3. `meta-llama/llama-3.3-70b-instruct:free` — works when not rate-limited
  4. `nvidia/nemotron-3-super-120b-a12b:free` — last free resort
  5. `deepseek/deepseek-v4-flash` — paid safety net
- **Delivery:** `announce` to WhatsApp — sends final text output directly

### 3. Python Worker (`run_worker.py`)
- **File:** `/home/opc/agentic-kanban/run_worker.py`
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

## Dev / Prod Isolation

Both prod and dev live on the same Oracle ARM instance, in separate directories:

```
~/agentic-kanban/        ← prod clone (always on main); cron job runs from here
~/agentic-kanban-dev/    ← dev working tree; any branch, safe to break
```

The cron job config hardcodes `~/agentic-kanban/run_worker.py` and never changes.
To deploy a change: `git -C ~/agentic-kanban pull` (run manually after merging to main).

This means the feature branch checked out in `~/agentic-kanban-dev/` never affects the live cron job.

## Cost Model

| Layer | Model | Cost |
|---|---|---|
| Main agent (OpenClaw session) | gpt-oss-120b:free (OpenRouter) | $0/token |
| Cron polling (10-min interval, 144 calls/day) | gpt-oss-120b:free (OpenRouter) | $0/token |
| Task execution | Claude Code (`claude -p`) | Anthropic Pro subscription |
| Fallback safety net | deepseek/deepseek-v4-flash | ~$0.14/M input, $0.28/M output |

**OpenRouter free tier:** requires a $10 balance to unlock 1,000 req/day (up from the default 50). The balance is not consumed by free-model calls — it just gates rate limits.

**Working free models (as of 2026-05-16):**
- `openai/gpt-oss-120b:free` — primary, confirmed working
- `minimax/minimax-m2.5:free` — confirmed working
- `meta-llama/llama-3.3-70b-instruct:free` — works when not rate-limited (429 common)
- `nvidia/nemotron-3-super-120b-a12b:free` — last free resort

**Dead/removed free models:** `deepseek-r1:free`, `gemma-3-27b-it:free`, `gemini-flash-1.5-8b:free` — all return 402/404.

**Known OpenClaw behavior:** a 429 from one model may put the entire OpenRouter provider into cooldown, blocking all fallbacks for that tick. Mitigation: keep API calls per cron tick minimal (currently 2).

