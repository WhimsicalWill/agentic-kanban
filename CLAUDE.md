# Agentic Kanban — Agent Guide

You are a Claude Code agent executing a task from the agentic-kanban queue. Your task ID and execution mode are at the top of your prompt.

## Execution modes

| Mode | Meaning |
|------|---------|
| `fresh` | First run. Execute the task described in the prompt. |
| `resume` | Follow-up run. A human reviewed your prior output and left new instructions in the prompt. Address them. |
| `watch` | Monitoring run. Check on whatever process you are watching. Self-transition when you find something worth acting on. |

## State machine

```
fresh/resume → awaiting_review   (worker handles this automatically when you exit)

watch → watching                 (default — just exit normally, worker keeps it watching)
watch → awaiting_review          (you call tasks.py — something needs human attention)
watch → ready (resume)           (you call tasks.py follow-up — queues a new session)
```

For `fresh` and `resume` tasks you do not need to touch the task state — the worker moves it to `awaiting_review` after you exit.

For `watch` tasks you are responsible for deciding the outcome each tick:
- **Nothing to act on** — exit normally. Worker keeps the task in `watching` and resumes you next tick.
- **Needs human attention** — call `tasks.py update` with `--state awaiting_review` before exiting.
- **Needs a follow-up session** — call `tasks.py follow-up` with a new prompt before exiting.

## Tasks CLI

```
python3 /home/opc/agentic-kanban/tasks.py update <TASK_ID> \
  --state awaiting_review \
  --output-summary "what you found"

python3 /home/opc/agentic-kanban/tasks.py follow-up <TASK_ID> \
  --prompt "instructions for the next session"
```

Replace `<TASK_ID>` with the task ID from the top of your prompt.
