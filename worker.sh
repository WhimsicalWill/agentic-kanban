#!/usr/bin/env bash
set -euo pipefail

# Task Worker — picks up ready tasks and follow-ups, executes via Claude Code
# Called by the OpenClaw cron worker agent (isolated session, free OpenRouter model)

TASK_STORE="/home/opc/task-store/tasks.py"
WORKDIR="/home/opc/.openclaw/workspace"

# Max API spend per task (USD)
MAX_SPEND="${MAX_SPEND:-0.50}"
# Max loop iterations when Claude returns early/incomplete
MAX_CONTINUATIONS=5

export HOME=/home/opc
cd "$WORKDIR"

# ── Phase 1: Process follow-ups (revision_queue tasks with session_id) ──────
# These are tasks that already ran once and have a stored session to resume.

followup_tasks=$(python3 -c "
import json, sys, sqlite3
conn = sqlite3.connect('/home/opc/task-store/tasks.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    \"SELECT * FROM tasks WHERE state = 'revision_queue' AND session_id IS NOT NULL AND session_id != ''\"
).fetchall()
print(json.dumps([dict(r) for r in rows]))
" 2>/dev/null)

if [ -n "$followup_tasks" ] && [ "$followup_tasks" != "[]" ]; then
  echo "=== Processing follow-up tasks ==="
  echo "$followup_tasks" | python3 -m json.tool

  for task_id in $(python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    print(t['id'])
" <<< "$followup_tasks"); do

    task_info=$(python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    if t['id'] == '$task_id':
        print(json.dumps(t))
        break
" <<< "$followup_tasks")

    session_id=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" <<< "$task_info")
    title=$(python3 -c "import json,sys; print(json.load(sys.stdin)['title'])" <<< "$task_info")

    # Get the latest follow-up prompt from the events table
    followup_prompt=$(python3 -c "
import json, sys, sqlite3
conn = sqlite3.connect('/home/opc/task-store/tasks.db')
row = conn.execute(
    \"SELECT note FROM events WHERE task_id = '$task_id' AND event_type = 'follow-up' ORDER BY id DESC LIMIT 1\"
).fetchone()
print(row[0] if row else '')
")

    echo "=== Follow-up: $task_id (session: $session_id) ==="
    python3 "$TASK_STORE" update "$task_id" --state in_progress

    prompt="$followup_prompt

Address the above feedback by modifying the codebase. Work autonomously — do not ask for input."

    continuation=0
    full_output=""

    while [ $continuation -lt $MAX_CONTINUATIONS ]; do
      echo "Resuming session $session_id (attempt $continuation)..."
      chunk=$(claude -p "$prompt" \
        --resume "$session_id" \
        --permission-mode bypassPermissions \
        --max-budget-usd "$MAX_SPEND" \
        --output-format text \
        --tools "Bash,Edit,Read,Write" \
        2>/dev/null || echo "CLAUDECODE_ERROR")

      full_output="$full_output
=== Continuation $continuation ===
$chunk"

      if echo "$chunk" | grep -qiE '(would you like|do you want|shall I|should I|please (confirm|review|check|let me know)|waiting for (input|feedback)|ask me if|need (your|human) (input|feedback))' 2>/dev/null; then
        echo "Claude asked for input — continuing..."
        continuation=$((continuation + 1))
        continue
      fi

      line_count=$(echo "$chunk" | wc -l)
      if [ "$line_count" -lt 3 ] && [ $continuation -gt 0 ]; then
        echo "Very short output — may be incomplete, continuing..."
        continuation=$((continuation + 1))
        continue
      fi
      break
    done

    if [ $continuation -ge $MAX_CONTINUATIONS ]; then
      full_output="$full_output
⚠️ Reached max continuations ($MAX_CONTINUATIONS). Task may be incomplete."
    fi

    python3 "$TASK_STORE" update "$task_id" --state awaiting_review --note "$(echo "$full_output" | head -2000)"
    echo "=== Follow-up $task_id moved to awaiting_review ==="
  done
fi

# ── Phase 2: Process fresh ready tasks (no session yet) ─────────────────────

ready_tasks=$(python3 "$TASK_STORE" list --state ready 2>/dev/null)

if [ -z "$ready_tasks" ] || [ "$ready_tasks" = "[]" ]; then
  exit 0
fi

echo "Found ready tasks:"
python3 "$TASK_STORE" list --state ready

for task_id in $(python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    print(t['id'])
" <<< "$ready_tasks"); do

  echo "=== Executing task: $task_id ==="
  python3 "$TASK_STORE" update "$task_id" --state in_progress

  task_info=$(python3 -c "
import json, sys
tasks = json.load(sys.stdin)
for t in tasks:
    if t['id'] == '$task_id':
        print(json.dumps(t))
        break
" <<< "$ready_tasks")

  title=$(python3 -c "import json,sys; print(json.load(sys.stdin)['title'])" <<< "$task_info")
  description=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('description',''))" <<< "$task_info")
  tags=$(python3 -c "import json,sys; print(','.join(json.load(sys.stdin).get('tags',[])))" <<< "$task_info")

  prompt="Task: $title"
  if [ -n "$description" ]; then
    prompt="$prompt

$description"
  fi
  if [ -n "$tags" ]; then
    prompt="$prompt

Tags: $tags"
  fi
  prompt="$prompt

Execute this task. Do not ask for input — work autonomously. If you hit a wall, explain what's blocking you and stop."

  continuation=0
  full_output=""
  session_id=""

  while [ $continuation -lt $MAX_CONTINUATIONS ]; do
    if [ $continuation -eq 0 ]; then
      session_id=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())")
      iter_prompt="$prompt"
      echo "Starting Claude Code session $session_id..."
    else
      iter_prompt="Continue where you left off. Complete the task. Do not ask for input."
      echo "Resuming Claude Code session $session_id (continuation $continuation)..."
    fi

    chunk=$(claude -p "$iter_prompt" \
      --permission-mode bypassPermissions \
      --max-budget-usd "$MAX_SPEND" \
      --output-format text \
      --session-id "$session_id" \
      --tools "Bash,Edit,Read,Write" \
      2>/dev/null || echo "CLAUDECODE_ERROR")

    full_output="$full_output
=== Continuation $continuation ===
$chunk"

    if echo "$chunk" | grep -qiE '(would you like|do you want|shall I|should I|please (confirm|review|check|let me know)|waiting for (input|feedback)|ask me if|need (your|human) (input|feedback))' 2>/dev/null; then
      echo "Claude asked for input — continuing..."
      continuation=$((continuation + 1))
      continue
    fi

    line_count=$(echo "$chunk" | wc -l)
    if [ "$line_count" -lt 3 ] && [ $continuation -gt 0 ]; then
      echo "Very short output — may be incomplete, continuing..."
      continuation=$((continuation + 1))
      continue
    fi
    break
  done

  if [ $continuation -ge $MAX_CONTINUATIONS ]; then
    full_output="$full_output
⚠️ Reached max continuations ($MAX_CONTINUATIONS). Task may be incomplete."
  fi

  # Store session_id in the DB
  sqlite3 /home/opc/task-store/tasks.db "UPDATE tasks SET session_id='$session_id' WHERE id='$task_id';"

  python3 "$TASK_STORE" update "$task_id" --state awaiting_review --note "$(echo "$full_output" | head -2000)"

  echo "=== Task $task_id moved to awaiting_review (session: $session_id) ==="
  echo "---RESULT---"
  echo "$(echo "$full_output" | head -500)"
  echo "---END_RESULT---"

done

echo "Worker cycle complete."