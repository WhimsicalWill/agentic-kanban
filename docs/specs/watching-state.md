# Spec: Watching State

**GitHub issue:** #4

## Problem

Some tasks don't end when Claude Code finishes — they begin a monitoring phase where the agent needs to periodically check in on a long-running external process (e.g. a training job, a deploy, a CI pipeline).

The current state machine has no fit for this:
- The task can't stay `in_progress` — once Claude Code exits, there's no mechanism to re-run it on a schedule
- The manual revision flow requires the user to trigger every check-in

**Concrete example:** the 05_09 experiment group. Claude Code launches 4 VastAI training jobs and exits. Those jobs run for 4–6 hours. During that time we want automated crash detection and escalation — without a human triggering each check.

---

## Proposed Solution

A new `watching` task state. A watching task is picked up by the existing `run_worker.py` cron job on every tick (alongside `ready` and `revision_queue` tasks). Claude Code resumes the session, checks in on whatever it's monitoring, and either stays in `watching` or self-transitions to `revision_queue` or `awaiting_review` depending on what it finds.

No separate poller process. No new cron job.

---

## State Machine Extension

New transitions:

| From | To | Triggered by |
|---|---|---|
| `in_progress` | `watching` | Claude Code calls `tasks.py update <id> --state watching` before exiting |
| `watching` | `watching` | run_worker.py default after each tick (Claude Code found nothing to act on) |
| `watching` | `revision_queue` | Claude Code calls `tasks.py follow-up` before exiting |
| `watching` | `awaiting_review` | Claude Code calls `tasks.py update <id> --state awaiting_review` before exiting |

```
in_progress → watching → watching → ... → awaiting_review
                                  ↘ revision_queue
```

---

## How Each Watch Tick Works

1. `run_worker.py` picks up the `watching` task (same priority ordering as other states)
2. Worker resumes the Claude Code session (`--resume <session_id>`) with the task description as the prompt
3. Claude Code checks in on whatever it's monitoring (SSH, API call, file check, etc.)
4. Claude Code decides the outcome using its own reasoning:
   - **Nothing to act on:** exits without touching the task → worker keeps state `watching`, updates `output_summary` and `session_id`
   - **Needs escalation:** calls `tasks.py update <id> --state awaiting_review --output-summary "..."` before exiting → worker detects the state changed and leaves it alone
   - **Needs a follow-up Claude Code session:** calls `tasks.py follow-up <id> --prompt "..."` before exiting → worker detects `revision_queue` and leaves it alone

run_worker.py re-reads the task state from the DB after Claude Code exits. If Claude Code already transitioned it, the worker skips its own update. If still `watching`, the worker refreshes `output_summary` and `session_id` and leaves state as `watching`.

---

## Example: VastAI Experiment Monitor

**Initial run (fresh task, state: `ready`):**

Claude Code launches 4 VastAI experiments, reports the launch summary (Step 6), then calls:
```
python3 /home/opc/agentic-kanban/tasks.py update task_5f912620 \
  --state watching \
  --output-summary "4 experiments launched: J (36698475), J2 (36699539), J3 (36698478), K (36700053)"
```

Task moves to `watching`. Claude Code exits.

**Each subsequent watch tick (state: `watching`, session resumed):**

Claude Code SSHes into each instance and checks:
- Is the training process still running?
- Current VRAM / GPU utilization
- Latest checkpoint epoch from `outputs/`

If all healthy → exits without calling tasks.py. Worker keeps task in `watching`.

If an instance has crashed → Claude Code calls:
```
python3 /home/opc/agentic-kanban/tasks.py update task_5f912620 \
  --state awaiting_review \
  --output-summary "Instance J (36698475) crashed at epoch 23. VRAM was 7.9GB/8GB. Logs: ..."
```

If all runs complete → Claude Code calls:
```
python3 /home/opc/agentic-kanban/tasks.py update task_5f912620 \
  --state awaiting_review \
  --output-summary "All experiments finished. Final metrics: ..."
```

---

## Schema Changes

### New `watching` state

Add `watching` to `VALID_STATES` in `tasks.py`. No other schema changes needed — `session_id` and `output_summary` already exist and are sufficient.

### run_worker.py changes

Add `watching` to the states the worker claims:
```python
for candidate_state, candidate_mode in [
    ("revision_queue", "resume"),
    ("ready", "fresh"),
    ("watching", "watch"),      # new
]:
```

In `process_one`, add a `watch` branch:
- Resume session (`--resume session_id`) with the task description as prompt
- After Claude Code exits, re-read the task state
- If state is still `watching`: update `output_summary` and `session_id`, keep state `watching`
- If state changed (Claude Code transitioned it): log and return — don't apply a second update

### WhatsApp report

Add a *Watching* section to the status report (between *In progress* and *Ready*), showing task title and latest `output_summary` preview.

---

## What Claude Code Needs to Know

When a task enters `watching`, the task description should already contain everything Claude Code needs for monitoring: what to check, how to connect, and what conditions trigger escalation. No separate config field is needed — the description doubles as the watch prompt.

The agent is responsible for writing a description that is self-contained for repeated execution.
