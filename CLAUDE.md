# Agentic Kanban — Agent Guide

You are a Claude Code agent executing a task from the agentic-kanban queue. Your task ID and execution mode are at the top of your prompt.

## Execution modes

| Mode | Meaning |
|------|---------|
| `fresh` | First run. Execute the task described in the prompt. |
| `resume` | Follow-up run. A human reviewed your prior output and left new instructions in the prompt. Address them. |
| `watch` | Monitoring run. Check on whatever process you are watching and decide what to do next. |

## What to do when you finish

**fresh / resume:** Just exit. The worker captures your session output automatically and moves the task to `awaiting_review` — no action needed.

**watch:** You decide the outcome each tick:

- **Nothing to act on** — exit normally. The worker keeps the task in `watching` and resumes you on the next cron tick.

- **Needs human attention** — call `tasks.py update` before exiting:
  ```
  python3 /home/opc/agentic-kanban/tasks.py update <TASK_ID> \
    --state awaiting_review \
    --output-summary "what you found"
  ```

- **Needs a follow-up session** — call `tasks.py follow-up` before exiting:
  ```
  python3 /home/opc/agentic-kanban/tasks.py follow-up <TASK_ID> \
    --prompt "instructions for the next session"
  ```

Replace `<TASK_ID>` with the task ID from the top of your prompt.
