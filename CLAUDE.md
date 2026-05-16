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

- **Real work to do next** — call `tasks.py follow-up` before exiting:
  ```
  python3 /home/opc/agentic-kanban/tasks.py follow-up <TASK_ID> \
    --prompt "instructions for the next session"
  ```
  Use this when the monitoring phase is done and the next step is substantial work
  (analysis, reporting, cleanup) that deserves its own focused session. Don't use it
  just to continue work you could do right now in the current session.

Replace `<TASK_ID>` with the task ID from the top of your prompt.

## Creating new tasks

You can create new tasks at any time — for example, to scope out parallel work or
hand off something discovered during execution.

```
# Needs human review before running (default for agent-created tasks):
python3 /home/opc/agentic-kanban/tasks.py add "Task title" \
  --description "Details" \
  --state awaiting_review

# Trivial / safe to run immediately without human review:
python3 /home/opc/agentic-kanban/tasks.py add "Task title" \
  --description "Details" \
  --state ready
```

Use `awaiting_review` when the task needs human approval before it runs.
Use `ready` only when you are confident the task is safe to execute autonomously.
