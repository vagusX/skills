"""Read one manager's registered Codex workers without loading native threads."""

import json
import time

from .approvals import answer_receipt, live_request
from .codex import process_alive
from .store import decode


def execution(chat, run, queued, pending_count, server_alive):
    state, reason = "idle", None
    if chat["binding"] in {"creating", "unknown"}:
        state, reason = "unknown", "native thread creation has not been reconciled"
    elif run and run["state"] == "unknown":
        state, reason = "unknown", run["error"] or "native execution has not been reconciled"
    elif run and run["state"] != "exited":
        if server_alive is False or (run["state"] == "running" and
                (not run["thread_id"] or not run["turn_id"] or server_alive is not True)):
            state, reason = "unknown", "owning Codex process or native turn is not confirmed"
        elif pending_count:
            state = "awaiting_answer"
        else:
            state = "running" if run["state"] == "running" else "starting"
    elif chat["closed"]:
        state = "closed"
    elif chat["hold"]:
        state = "paused"
    elif queued:
        state = "queued"
    elif run:
        outcome = run["outcome"]
        if outcome in {"failed", "interrupted"}:
            state, reason = outcome, run["error"]
        elif outcome != "completed":
            state, reason = "unknown", run["error"] or "native outcome has not been confirmed"
    return {"state": state, "reason": reason,
            "run_id": run["id"] if run else None,
            "turn_id": run["turn_id"] if run else None,
            "run_state": run["state"] if run else None,
            "native_outcome": run["outcome"] if run else None,
            "started_at": run["started_at"] if run else None,
            "ended_at": run["ended_at"] if run else None,
            "server_alive": server_alive}


def list_work(store):
    """One SQLite read snapshot; no RPC, queue changes, recovery or model calls."""
    observed_at = time.time()
    processes = {}

    def alive(pid, started):
        key = (pid, started)
        if key not in processes:
            processes[key] = process_alive(pid, started)
        return processes[key]

    try:
        daemon = json.loads((store.directory / "daemon.json").read_text())
        scheduler_alive = alive(daemon["pid"], daemon["started"])
    except (OSError, ValueError, KeyError):
        scheduler_alive = False
    # A deferred read transaction avoids claiming the scheduler's write lock.
    store.db.execute("SAVEPOINT work_list_snapshot")
    try:
        main = store.main()
        works = []
        for chat in store.all("SELECT * FROM chats WHERE role='worker' ORDER BY created_at,id"):
            run = store.one("""SELECT * FROM runs WHERE chat_id=?
                ORDER BY (state!='exited') DESC,started_at DESC,id DESC LIMIT 1""", (chat["id"],))
            queued = store.all("""SELECT id FROM jobs WHERE chat_id=? AND status='pending'
                AND kind IN ('task','followup','recover') ORDER BY created_at,id""", (chat["id"],))
            questions = []
            for job in store.all("""SELECT * FROM jobs WHERE chat_id=? AND kind='question'
                    AND status!='done' ORDER BY created_at,id""", (chat["id"],)):
                request = decode(job["payload"], {})
                live = live_request(store, job)
                phase = answer_receipt(store, job["id"])["phase"]
                questions.append({"question_id": job["id"], "method": request.get("method"),
                                  "phase": phase, "live": live,
                                  "needs_answer": live and phase == "pending"})
            pending_count = sum(q["needs_answer"] for q in questions)
            server_alive = (alive(run["server_pid"], run["server_start"])
                            if run and run["state"] != "exited" and run["server_pid"] else None)
            report = decode(chat["report_ref"])
            current = (bool(run and run["turn_id"] and report.get("turn_id") == run["turn_id"]
                            and report.get("thread_id") == chat["thread_id"]) if report else None)
            works.append({"work_id": chat["id"], "title": chat["title"],
                          "thread_id": chat["thread_id"], "cwd": chat["cwd"],
                          "execution": execution(chat, run, queued, pending_count, server_alive),
                          "queued_job_ids": [job["id"] for job in queued],
                          "hold": decode(chat["hold"]), "closed": bool(chat["closed"]),
                          "report_state": chat["report_state"], "report_ref": report,
                          "report_current": current,
                          "pending_question_count": pending_count, "questions": questions})
        return {"manager_id": main["id"], "main_thread_id": main["thread_id"],
                "cwd": main["cwd"], "snapshot_at": observed_at,
                "scheduler_alive": scheduler_alive, "works": works}
    finally:
        store.db.execute("RELEASE work_list_snapshot")
