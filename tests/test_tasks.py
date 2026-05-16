"""Unit tests for tasks.py CLI."""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TASKS_PY = Path(__file__).parent.parent / "tasks.py"


def make_db(path: str) -> None:
    """Create the tasks + events schema at the given path."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            state TEXT NOT NULL DEFAULT 'inbox',
            tags TEXT DEFAULT '[]',
            priority INTEGER DEFAULT 5,
            created_at TEXT,
            state_changed_at TEXT,
            iteration_count INTEGER DEFAULT 0,
            blocked_reason TEXT,
            agent_confidence TEXT,
            parent_task TEXT,
            executor TEXT DEFAULT 'claude-code',
            claimed_by TEXT,
            claimed_at TEXT,
            output_summary TEXT,
            session_id TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            note TEXT,
            created_at TEXT
        );
    """)
    conn.close()


def run(*args, db_path: str):
    """Run tasks.py with a patched DB_PATH env and return (returncode, parsed_output)."""
    env_patch = {"DB_PATH_OVERRIDE": db_path}
    result = subprocess.run(
        [sys.executable, str(TASKS_PY)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env={**__import__("os").environ, **env_patch},
    )
    stdout = result.stdout.strip()
    if stdout:
        try:
            return result.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            return result.returncode, stdout
    return result.returncode, {}


@pytest.fixture
def db(tmp_path):
    """Provide a fresh tasks DB for each test."""
    db_file = str(tmp_path / "tasks.db")
    make_db(db_file)
    return db_file


# ── helpers ──────────────────────────────────────────────────────────────────

def add_task(db, title="Test task", description="", tags=None, priority=5):
    args = ["add", title]
    if description:
        args += ["--description", description]
    if tags:
        args += ["--tags"] + tags
    args += ["--priority", str(priority)]
    code, out = run(*args, db_path=db)
    assert code == 0
    return out["id"]


# ── add ───────────────────────────────────────────────────────────────────────

def test_add_creates_inbox_task(db):
    code, out = run("add", "My task", db_path=db)
    assert code == 0
    assert out["state"] == "inbox"
    assert out["id"].startswith("task_")


def test_add_with_tags_and_priority(db):
    task_id = add_task(db, title="Tagged task", tags=["auth", "backend"], priority=2)
    _, out = run("get", task_id, db_path=db)
    assert out["tags"] == ["auth", "backend"]
    assert out["priority"] == 2


# ── list ──────────────────────────────────────────────────────────────────────

def test_list_excludes_done_and_cancelled(db):
    t1 = add_task(db, "Active task")
    t2 = add_task(db, "Done task")
    run("update", t2, "--state", "done", db_path=db)
    _, tasks = run("list", db_path=db)
    ids = [t["id"] for t in tasks]
    assert t1 in ids
    assert t2 not in ids


def test_list_state_filter(db):
    t1 = add_task(db, "Ready task")
    t2 = add_task(db, "Inbox task")
    run("update", t1, "--state", "ready", db_path=db)
    _, tasks = run("list", "--state", "ready", db_path=db)
    ids = [t["id"] for t in tasks]
    assert t1 in ids
    assert t2 not in ids


# ── get ───────────────────────────────────────────────────────────────────────

def test_get_returns_task(db):
    task_id = add_task(db, "Fetch me")
    _, out = run("get", task_id, db_path=db)
    assert out["id"] == task_id
    assert out["title"] == "Fetch me"


def test_get_missing_task_exits_nonzero(db):
    code, out = run("get", "task_does_not_exist", db_path=db)
    assert code != 0
    assert "error" in out


# ── update ────────────────────────────────────────────────────────────────────

def test_update_transitions_state(db):
    task_id = add_task(db)
    code, out = run("update", task_id, "--state", "ready", db_path=db)
    assert code == 0
    assert out["state"] == "ready"


def test_update_stores_output_summary(db):
    task_id = add_task(db)
    run("update", task_id, "--state", "awaiting_review", "--output-summary", "done!", db_path=db)
    _, out = run("get", task_id, db_path=db)
    assert out["output_summary"] == "done!"


# ── next ──────────────────────────────────────────────────────────────────────

def test_next_returns_empty_when_no_tasks(db):
    _, out = run("next", db_path=db)
    assert out.get("status") == "empty"


def test_next_prefers_revision_queue_over_ready(db):
    ready_id = add_task(db, "Ready task")
    revision_id = add_task(db, "Revision task")
    run("update", ready_id, "--state", "ready", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET state = 'revision_queue', session_id = 'sess-rev' WHERE id = ?",
        (revision_id,),
    )
    conn.commit()
    conn.close()
    _, out = run("next", db_path=db)
    assert out["id"] == revision_id
    assert out["mode"] == "resume"


def test_next_prefers_watching_over_ready(db):
    ready_id = add_task(db, "Ready task")
    watch_id = add_task(db, "Watching task")
    run("update", ready_id, "--state", "ready", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET state = 'watching', session_id = 'sess-watch' WHERE id = ?",
        (watch_id,),
    )
    conn.commit()
    conn.close()
    _, out = run("next", db_path=db)
    assert out["id"] == watch_id
    assert out["mode"] == "watch"


def test_next_claims_ready_task(db):
    task_id = add_task(db, "Fresh task")
    run("update", task_id, "--state", "ready", db_path=db)
    _, out = run("next", "--run-id", "worker-test", db_path=db)
    assert out["id"] == task_id
    assert out["state"] == "in_progress"
    assert out["mode"] == "fresh"


def test_next_skips_needs_human_tagged_tasks(db):
    task_id = add_task(db, "Blocked task", tags=["needs_human"])
    run("update", task_id, "--state", "ready", db_path=db)
    _, out = run("next", db_path=db)
    assert out.get("status") == "empty"


# ── follow-up ─────────────────────────────────────────────────────────────────

def test_followup_requires_session_id(db):
    task_id = add_task(db)
    run("update", task_id, "--state", "awaiting_review", db_path=db)
    code, out = run("follow-up", task_id, "--prompt", "fix it", db_path=db)
    assert code != 0
    assert "session_id" in str(out.get("error", ""))


def test_followup_sets_revision_queue(db):
    task_id = add_task(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET state = 'awaiting_review', session_id = 'sess-abc' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    code, out = run("follow-up", task_id, "--prompt", "make it better", db_path=db)
    assert code == 0
    assert out["state"] == "revision_queue"
    assert out["session_id"] == "sess-abc"


def test_followup_overwrites_description(db):
    task_id = add_task(db, description="original instructions")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET state = 'awaiting_review', session_id = 'sess-abc' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    run("follow-up", task_id, "--prompt", "new instructions", db_path=db)
    _, out = run("get", task_id, db_path=db)
    assert out["description"] == "new instructions"
