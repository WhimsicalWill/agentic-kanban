# Spec: Watching State + Lightweight Poller

**GitHub issue:** #4

## Problem

Some tasks don't end when Claude Code finishes — they begin a monitoring phase. The current state machine has no place for this:

- Keeping Claude Code running in `in_progress` is wasteful (burns usage limits, blocks 1 of 3 worker threads for hours)
- Using the manual revision flow requires the user to trigger every check-in
- Letting the task finish and re-queuing it on a timer loses session context and produces noisy WhatsApp reports

**Concrete example:** the 05_09 experiment group. Claude Code launches 4 VastAI training jobs and exits. Those jobs run for 4–6 hours. During that time we want automated crash detection, VRAM monitoring, and escalation — without a human triggering each check.

## Proposed Solution

A new `watching` task state and a separate lightweight poller (`watcher.py`) that runs on a schedule independently of the main Claude Code worker.

### Key design principles
- The watcher is **not** Claude Code. It runs cheap, deterministic checks (SSH, API calls, DB reads). Claude Code is only invoked if the watcher decides human-level reasoning is needed.
- The watcher is **stateless between ticks** — all state lives in the task's `watch_config` JSON column.
- A watching task stays in `watching` indefinitely until the poller transitions it out.

---

## State Machine Extension

```
inbox → ready → in_progress → awaiting_review → done
                    ↑    ↓           ↓
                    ←← revision_queue ←←
                         ↓
                      watching
                         ↓ (poller)
          ┌──────────────┼──────────────┐
     watching        revision_queue  awaiting_review
   (no action)      (Claude Code    (escalate to
                      follow-up)       human)
```

New transitions:

| From | To | Triggered by |
|---|---|---|
| `in_progress` | `watching` | Claude Code calls `tasks.py watch <id> --config '{...}'` |
| `watching` | `watching` | Poller runs, no action required |
| `watching` | `revision_queue` | Poller detects condition requiring Claude Code reasoning |
| `watching` | `awaiting_review` | Poller detects terminal condition (crash, completion, escalation) |

---

## Example: VastAI Experiment Monitor

### Current flow (broken)
1. Claude Code launches experiments, reports status (Step 6), exits → `awaiting_review`
2. User manually sends follow-up prompts to check in
3. Each revision consumes a Claude Code session and a worker thread slot

### New flow
1. Claude Code launches experiments, stores instance metadata, calls:
   ```
   python3 tasks.py watch task_5f912620 --config '{"check_type": "vastai_experiments", ...}'
   ```
2. Task transitions `in_progress` → `watching`. Claude Code exits. Worker thread is free.
3. Every 5 minutes (separate cron), `watcher.py` picks up all `watching` tasks and runs their checks.
4. For each VastAI instance the watcher SSH's in and checks:
   - Is the tmux/screen session alive? (`tmux ls` or `pgrep -f train.py`)
   - Current VRAM usage (`nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader`)
   - Latest checkpoint epoch (check `outputs/` for newest `.pt` file mtime)
5. Based on results:
   - **All healthy** → update `watch_config.last_checked`, stay in `watching`
   - **One instance crashed, restarts < max_restarts** → SSH in, re-run `vastctl.py train`, increment `restart_count`, stay in `watching`
   - **One instance crashed, restarts exhausted** → move to `awaiting_review` with summary
   - **All instances finished (epoch >= target)** → move to `awaiting_review` with final metrics

---

## Schema Changes

### New column: `watch_config`

Add a `watch_config TEXT` column to the `tasks` table. Stored as JSON. Schema varies by `check_type`.

**`vastai_experiments` example:**
```json
{
  "check_type": "vastai_experiments",
  "poll_interval_seconds": 300,
  "last_checked_at": null,
  "target_epochs": 50,
  "auto_restart": true,
  "max_restarts": 2,
  "instances": [
    {
      "exp": "J",
      "vastai_id": "36698475",
      "ssh_host": "192.168.1.10",
      "ssh_port": 22222,
      "wandb_run": "eager-sound-189",
      "restart_count": 0,
      "last_status": "running"
    }
  ]
}
```

### New column: `next_poll_at`

Add a `next_poll_at TEXT` column so the poller can skip tasks that aren't due yet without loading `watch_config`.

---

## New CLI Command: `tasks.py watch`

```
python3 tasks.py watch <task_id> --config '<json>'
```

- Validates `task_id` is `in_progress`
- Stores `watch_config` JSON to the new column
- Sets `state = watching`, `next_poll_at = now + poll_interval_seconds`
- Clears `claimed_by` / `claimed_at`
- Logs a `state_change` event

---

## New Component: `watcher.py`

Runs on a separate schedule (e.g. every 5 minutes via system cron or a second OpenClaw job). Does **not** use Claude Code.

```
watcher.py
  → tasks.py list --state watching
  → for each task where next_poll_at <= now:
      load watch_config
      dispatch to check function by check_type
      apply result action (stay / revision / review)
      update next_poll_at
```

Check functions are pure Python — SSH calls, HTTP requests, file checks. No LLM involved.

**Check function interface:**
```python
def check_vastai_experiments(task_id, config) -> WatchResult:
    ...

@dataclass
class WatchResult:
    action: Literal["stay", "revision", "review"]
    updated_config: dict       # written back to watch_config
    summary: str               # stored as output_summary if escalating
    follow_up_prompt: str | None  # used if action == "revision"
```

---

## Poller Scheduling

Two options:

**Option A — Second OpenClaw cron job (5-min interval)**
- Pros: consistent with existing infra, delivers results to WhatsApp
- Cons: uses 2 more OpenRouter API calls per tick (one exec + one text turn)

**Option B — System cron (`crontab -e`)**
- Pros: zero API cost, runs even if OpenClaw is down
- Cons: no WhatsApp delivery; output goes to system cron mail or a log file

Recommendation: **Option B** for the polling work itself; promote to WhatsApp only when the poller transitions a task (i.e. write a small announce script that `run_worker.py` already handles via the normal report).

---

## Open Questions

1. **SSH key management** — the watcher needs SSH access to VastAI instances. Store key path in `watch_config`? Use a dedicated key at a fixed path (`~/.ssh/vastai_watcher`)?
2. **Check type registry** — hard-code check types in `watcher.py`, or make them pluggable (e.g. each task stores a script path)?
3. **Follow-up prompt content** — when the watcher queues a revision, what does it tell Claude Code? A templated message ("instance J crashed after epoch 34, restart_count=2, last VRAM: 7.9GB") or a full diagnostic dump?
4. **Watching tasks in WhatsApp report** — should `run_worker.py` include a *Watching* section in the status report? Would add useful visibility but increase message length.
