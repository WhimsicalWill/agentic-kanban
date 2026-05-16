#!/usr/bin/env python3
"""Task store CLI — manages the SQLite task queue."""

import sys
import json
import os
import sqlite3
import uuid
import argparse
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH_OVERRIDE", "/home/opc/agentic-kanban/tasks.db")
VALID_STATES = ["inbox", "scoping", "ready", "in_progress", "awaiting_review",
                "awaiting_client", "revision_queue", "done", "blocked", "cancelled"]
FOLLOW_UP_STATES = ["awaiting_review", "revision_queue"]  # states that can receive follow-ups
MAX_ITERATIONS = 5


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_task(row):
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


def cmd_add(args):
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    tags = json.dumps(args.tags or [])
    ts = now()
    with db() as conn:
        conn.execute(
            """INSERT INTO tasks (id, title, description, state, tags, priority,
               created_at, state_changed_at, agent_confidence, executor)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, args.title, args.description or "", "inbox", tags,
             args.priority, ts, ts, args.confidence or "high", "claude-code")
        )
        conn.execute(
            "INSERT INTO events (task_id, event_type, to_state, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "created", "inbox", ts)
        )
    print(json.dumps({"id": task_id, "state": "inbox"}))


def cmd_list(args):
    state_filter = args.state
    with db() as conn:
        if state_filter:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state = ? ORDER BY priority ASC, created_at ASC",
                (state_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state NOT IN ('done', 'cancelled') "
                "ORDER BY priority ASC, created_at ASC"
            ).fetchall()
    print(json.dumps([fmt_task(r) for r in rows], indent=2))


def cmd_get(args):
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(json.dumps({"error": f"Task {args.id} not found"}))
        sys.exit(1)
    print(json.dumps(fmt_task(row), indent=2))


def cmd_update(args):
    """Transition task state, with guard on double-claim."""
    task_id = args.id
    new_state = args.state
    ts = now()

    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            print(json.dumps({"error": f"Task {task_id} not found"}))
            sys.exit(1)
        task = dict(row)

        # Guard: don't claim an already-claimed task
        if new_state == "in_progress" and task["claimed_by"] and task["claimed_by"] != args.claimed_by:
            print(json.dumps({"error": "already_claimed", "claimed_by": task["claimed_by"]}))
            sys.exit(1)

        updates = {"state": new_state, "state_changed_at": ts}
        if args.claimed_by:
            updates["claimed_by"] = args.claimed_by
            updates["claimed_at"] = ts
        if new_state in ("done", "cancelled", "awaiting_review"):
            updates["claimed_by"] = None
            updates["claimed_at"] = None
        if args.blocked_reason:
            updates["blocked_reason"] = args.blocked_reason
        if args.output_summary:
            updates["output_summary"] = args.output_summary
        if args.confidence:
            updates["agent_confidence"] = args.confidence
        if args.session_id:
            updates["session_id"] = args.session_id

        if new_state == "revision_queue":
            updates["iteration_count"] = task["iteration_count"] + 1
            if updates["iteration_count"] >= MAX_ITERATIONS:
                updates["state"] = "blocked"
                updates["blocked_reason"] = updates.get("blocked_reason") or f"Exceeded {MAX_ITERATIONS} iterations"

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [task_id]
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", vals)
        conn.execute(
            "INSERT INTO events (task_id, event_type, from_state, to_state, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "state_change", task["state"], updates["state"], args.note, ts)
        )

    print(json.dumps({"id": task_id, "state": updates["state"]}))


def cmd_follow_up(args):
    """Queue a follow-up prompt for a task that has a Claude Code session.

    Sets state to 'revision_queue' so the worker can resume the session
    via --resume <session-id>.  The prompt is stored as the event note.
    """
    task_id = args.id
    ts = now()

    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            print(json.dumps({"error": f"Task {task_id} not found"}))
            sys.exit(1)
        task = dict(row)

        session_id = args.session_id or task.get("session_id") or ""
        if not session_id:
            print(json.dumps({
                "error": f"Task {task_id} has no session_id. Use --session-id to set one."
            }))
            sys.exit(1)

        iteration = task["iteration_count"] + 1
        if iteration >= MAX_ITERATIONS:
            print(json.dumps({
                "error": f"Task {task_id} has exceeded {MAX_ITERATIONS} iterations"
            }))
            sys.exit(1)

        updates = {
            "state": "revision_queue",
            "state_changed_at": ts,
            "iteration_count": iteration,
            "session_id": session_id,
            "blocked_reason": None,
            "claimed_by": None,
            "claimed_at": None,
        }
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?",
                     list(updates.values()) + [task_id])
        conn.execute(
            "INSERT INTO events (task_id, event_type, from_state, to_state, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "follow-up", task["state"], "revision_queue", args.prompt, ts)
        )

    print(json.dumps({
        "id": task_id,
        "state": "revision_queue",
        "session_id": session_id,
        "iteration": iteration,
    }))


def cmd_next(args):
    """Claim the next actionable task. revision_queue (resume) takes priority over ready (fresh)."""
    run_id = args.run_id or f"run_{uuid.uuid4().hex[:8]}"
    ts = now()
    with db() as conn:
        row = None
        mode = None
        for candidate_state, candidate_mode in [("revision_queue", "resume"), ("ready", "fresh")]:
            row = conn.execute(
                """SELECT * FROM tasks
                   WHERE state = ?
                   AND (claimed_by IS NULL OR datetime(claimed_at) < datetime('now', '-30 minutes'))
                   AND tags NOT LIKE '%needs_human%'
                   ORDER BY priority ASC, state_changed_at ASC
                   LIMIT 1""",
                (candidate_state,)
            ).fetchone()
            if row:
                mode = candidate_mode
                break

        if not row:
            print(json.dumps({"status": "empty"}))
            return

        task_id = row["id"]
        # Atomic CAS: only claim if state hasn't changed since the SELECT.
        # Concurrent threads may have selected the same row; rowcount=0 means we lost the race.
        claimed = conn.execute(
            """UPDATE tasks SET state = 'in_progress', claimed_by = ?, claimed_at = ?, state_changed_at = ?
               WHERE id = ? AND state = ?""",
            (run_id, ts, ts, task_id, candidate_state)
        )
        if claimed.rowcount == 0:
            print(json.dumps({"status": "empty"}))
            return

        conn.execute(
            "INSERT INTO events (task_id, event_type, from_state, to_state, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "claimed", candidate_state, "in_progress", f"claimed by {run_id} mode={mode}", ts)
        )

        result = {**fmt_task(row), "state": "in_progress", "claimed_by": run_id, "mode": mode}

        if mode == "resume":
            follow_up = conn.execute(
                "SELECT note FROM events WHERE task_id = ? AND event_type = 'follow-up' ORDER BY created_at DESC LIMIT 1",
                (task_id,)
            ).fetchone()
            result["follow_up_prompt"] = follow_up["note"] if follow_up else None

    print(json.dumps(result))


def cmd_needs_review(args):
    """List tasks awaiting your attention."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE state IN ('awaiting_review', 'blocked') ORDER BY state_changed_at ASC"
        ).fetchall()
    tasks = [fmt_task(r) for r in rows]
    if not tasks:
        print("No tasks need your attention.")
    else:
        for t in tasks:
            print(f"[{t['state'].upper()}] {t['id']}: {t['title']}")
            if t.get("output_summary"):
                print(f"  Output: {t['output_summary']}")
            if t.get("blocked_reason"):
                print(f"  Reason: {t['blocked_reason']}")


def main():
    parser = argparse.ArgumentParser(description="Task store CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="Add a task to inbox")
    p_add.add_argument("title")
    p_add.add_argument("--description", "-d")
    p_add.add_argument("--tags", nargs="*")
    p_add.add_argument("--priority", type=int, default=5)
    p_add.add_argument("--confidence", choices=["high", "medium", "low"])

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--state", "-s")

    p_get = sub.add_parser("get", help="Get a task by ID")
    p_get.add_argument("id")

    p_update = sub.add_parser("update", help="Update task state")
    p_update.add_argument("id")
    p_update.add_argument("--state", "-s", choices=VALID_STATES, required=True)
    p_update.add_argument("--claimed-by")
    p_update.add_argument("--blocked-reason")
    p_update.add_argument("--output-summary")
    p_update.add_argument("--confidence", choices=["high", "medium", "low"])
    p_update.add_argument("--session-id")
    p_update.add_argument("--note")

    p_followup = sub.add_parser("follow-up", help="Send a follow-up prompt to a task with a session")
    p_followup.add_argument("id")
    p_followup.add_argument("--prompt", "-p", required=True, help="Follow-up prompt for Claude Code")
    p_followup.add_argument("--session-id", help="Override session ID (auto-reads from DB by default)")

    p_next = sub.add_parser("next", help="Claim next ready task")
    p_next.add_argument("--run-id")

    sub.add_parser("needs-review", help="Show tasks awaiting your attention")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    {"add": cmd_add, "list": cmd_list, "get": cmd_get,
     "update": cmd_update, "next": cmd_next,
     "follow-up": cmd_follow_up,
     "needs-review": cmd_needs_review}[args.cmd](args)


if __name__ == "__main__":
    main()
