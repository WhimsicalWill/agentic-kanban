#!/usr/bin/env python3
"""Task worker — claims the next task and executes it via Claude Code."""

import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

TASKS_CLI = "/home/opc/agentic-kanban/tasks.py"
WORKER_ID = f"worker-{os.getpid()}"
WORK_DIR = "/home/opc/agentic-kanban"
MAX_PARALLEL_WORKERS = 3


def tasks_cmd(*args):
    result = subprocess.run(
        [sys.executable, TASKS_CLI] + list(args),
        capture_output=True, text=True,
    )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def run_claude(prompt, session_id=None):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    if session_id:
        cmd += ["--resume", session_id]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=WORK_DIR, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"is_error": True, "result": "claude timed out after 30 minutes", "session_id": None}

    stdout = proc.stdout.strip()

    if not stdout:
        return {"is_error": True, "result": proc.stderr.strip() or "no output", "session_id": None}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "result": stdout[:800], "session_id": None}


def build_prompt(task_id, mode, task):
    header = f"Task ID: {task_id}\nMode: {mode}\n---"
    if mode == "fresh":
        body = task["title"]
        if task.get("description"):
            body += f"\n\n{task['description']}"
    else:
        body = task.get("description", "")
    return f"{header}\n{body}"


def process_one(run_id):
    """Claim and execute one task. Returns True if a task was processed, False if queue empty."""
    task = tasks_cmd("next", "--run-id", run_id)

    if task.get("status") == "empty":
        return False

    task_id = task["id"]
    mode = task.get("mode", "fresh")
    print(f"[{mode.upper()}] {task_id}: {task['title']}", file=sys.stderr)

    if mode == "fresh":
        result = run_claude(build_prompt(task_id, mode, task))

    elif mode == "resume":
        session_id = task.get("session_id")
        if not session_id:
            tasks_cmd("update", task_id,
                      "--state", "awaiting_review",
                      "--note", "Cannot resume: no session_id stored")
            print(f"ERROR: {task_id} has no session_id — moved to awaiting_review", file=sys.stderr)
            return True

        result = run_claude(build_prompt(task_id, mode, task), session_id=session_id)

    else:  # watch
        session_id = task.get("session_id")
        if not session_id:
            tasks_cmd("update", task_id,
                      "--state", "awaiting_review",
                      "--note", "Cannot watch: no session_id stored")
            print(f"ERROR: {task_id} has no session_id — moved to awaiting_review", file=sys.stderr)
            return True

        result = run_claude(build_prompt(task_id, mode, task), session_id=session_id)

    output = (result.get("result") or "")[:800]
    new_session_id = result.get("session_id")
    is_error = result.get("is_error", False)

    if mode == "watch":
        # Check if Claude Code already self-transitioned the task.
        current = tasks_cmd("get", task_id)
        if current.get("state") != "in_progress":
            print(f"→ {current.get('state')} [self-transitioned]", file=sys.stderr)
            return True
        # No self-transition: default back to watching.
        update_args = ["update", task_id, "--state", "watching", "--output-summary", output]
        if new_session_id:
            update_args += ["--session-id", new_session_id]
        tasks_cmd(*update_args)
        print(f"→ watching [{'ERROR' if is_error else 'OK'}]", file=sys.stderr)
    else:
        update_args = ["update", task_id, "--state", "awaiting_review", "--output-summary", output]
        if new_session_id:
            update_args += ["--session-id", new_session_id]
        if is_error:
            update_args += ["--note", "claude returned is_error=true"]
        tasks_cmd(*update_args)
        status = "ERROR" if is_error else "OK"
        print(f"→ awaiting_review [{status}]", file=sys.stderr)

    if output:
        print(f"Preview: {output[:200]}", file=sys.stderr)

    return True


def queue_summary():
    """Return a compact status string for the WhatsApp report."""
    import time as _time
    from datetime import datetime, timedelta, timezone
    utc_now = datetime.now(timezone.utc)
    # DST: second Sunday in March through first Sunday in November
    year = utc_now.year
    dst_start = datetime(year, 3, 8, 2, tzinfo=timezone.utc)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)  # next Sunday
    dst_end = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    if dst_start <= utc_now < dst_end:
        tz_offset, tz_label = -7, "PDT"
    else:
        tz_offset, tz_label = -8, "PST"
    local_now = utc_now + timedelta(hours=tz_offset)
    time_str = local_now.strftime("%H:%M") + f" {tz_label}"

    all_tasks = tasks_cmd("list")
    if not isinstance(all_tasks, list):
        all_tasks = []

    needs_review = [t for t in all_tasks if t.get("state") == "awaiting_review"]
    in_progress = [t for t in all_tasks if t.get("state") == "in_progress"]
    watching = [t for t in all_tasks if t.get("state") == "watching"]
    queued = [t for t in all_tasks if t.get("state") in ("ready", "revision_queue", "inbox")]

    return {
        "time": time_str,
        "needs_review": needs_review,
        "in_progress": in_progress,
        "watching": watching,
        "queued": queued,
    }


def fetch_usage_stats():
    """Return a compact usage line string, or None if both fetches fail."""
    parts = []

    try:
        with open("/home/opc/.claude/.credentials.json") as f:
            creds = json.load(f)
        token = creds["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.0.31",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        five_h = data["five_hour"]["utilization"]
        seven_d = data["seven_day"]["utilization"]
        parts.append(f"CC 5h: {five_h}% · 7d: {seven_d}%")
    except Exception:
        pass

    try:
        with open("/home/opc/.openclaw/agents/main/agent/auth-profiles.json") as f:
            profiles = json.load(f)
        key = profiles["profiles"]["openrouter:default"]["key"]
        auth_req = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(auth_req, timeout=10) as resp:
            auth_data = json.loads(resp.read())
        daily = auth_data["data"].get("usage_daily") or 0
        credits_req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(credits_req, timeout=10) as resp:
            credits_data = json.loads(resp.read())
        total_credits = credits_data["data"].get("total_credits") or 0
        total_usage = credits_data["data"].get("total_usage") or 0
        balance = total_credits - total_usage
        parts.append(f"OR ${daily:.3f}/day (${balance:.2f} bal)")
    except Exception:
        pass

    if not parts:
        return None
    return "*Usage:* " + " | ".join(parts)


def format_whatsapp_report(processed, summary, errors, usage_line=None):
    time_str = summary["time"]
    needs_review = summary["needs_review"]
    in_progress = summary.get("in_progress", [])
    watching = summary.get("watching", [])
    queued = summary["queued"]

    if processed == 0 and not needs_review and not in_progress and not watching and not queued:
        idle_line = f"*Task Queue* · {time_str} — idle, nothing pending"
        if usage_line:
            idle_line += f"\n{usage_line}"
        return idle_line

    lines = [f"*Task Queue* · {time_str}"]
    lines.append(f"*Processed this run:* {processed} task(s)" if processed else "*Processed this run:* idle")

    if needs_review:
        lines.append("*Needs your review:*")
        for t in needs_review[:3]:
            summary_preview = (t.get("output_summary") or "")[:60]
            lines.append(f"· [{t['id']}] {t['title']}: {summary_preview}")
    else:
        lines.append("*Needs your review:* none")

    if in_progress:
        lines.append("*In progress:*")
        for t in in_progress[:3]:
            lines.append(f"· {t['title']}")

    if watching:
        lines.append("*Watching:*")
        for t in watching[:3]:
            summary_preview = (t.get("output_summary") or "")[:60]
            lines.append(f"· {t['title']}: {summary_preview}")

    if queued:
        lines.append("*Queued:*")
        for t in queued[:3]:
            lines.append(f"· [{t['state']}] {t['title']}")
    else:
        lines.append("*Queued:* none")

    if errors:
        lines.append(f"*Issues:* {'; '.join(errors)}")

    if usage_line:
        lines.append(usage_line)

    return "\n".join(lines)


def run_worker_thread(thread_idx):
    """Drain the task queue from one thread. Returns (processed_count, errors)."""
    processed = 0
    errors = []
    while True:
        run_id = f"{WORKER_ID}-t{thread_idx}-{processed}"
        try:
            did_work = process_one(run_id)
        except Exception as e:
            errors.append(str(e)[:80])
            break
        if not did_work:
            break
        processed += 1
    return processed, errors


def main():
    processed = 0
    errors = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as executor:
        futures = [executor.submit(run_worker_thread, i) for i in range(MAX_PARALLEL_WORKERS)]
        for future in as_completed(futures):
            p, e = future.result()
            processed += p
            errors.extend(e)

    summary = queue_summary()
    usage_line = fetch_usage_stats()
    report = format_whatsapp_report(processed, summary, errors, usage_line)

    # Print the formatted report — the cron job's announce delivery sends this to WhatsApp
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
