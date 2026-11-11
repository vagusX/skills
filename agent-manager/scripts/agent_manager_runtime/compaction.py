"""Native maintenance uses separate jobs, never a worker's delivery state."""

import time

from .store import encode


def queue_compaction(store, after_run=None):
    main = store.main()
    if not main["entry_thread_id"] or main["hold"] or not main["thread_id"]:
        return {"queued": False, "reason": "manager is inactive, held or not yet bound"}
    if store.one("SELECT id FROM jobs WHERE kind='question' AND status!='done'"):
        return {"queued": False, "reason": "unresolved native request"}
    existing = store.one("SELECT id,status FROM jobs WHERE chat_id=? AND kind='compact' AND status IN ('pending','inflight','unknown')", (main["id"],))
    if existing:
        return {"queued": existing["status"] == "pending", "job_id": existing["id"], "status": existing["status"]}
    job, created = store.enqueue("compact", main["id"], {"reason": "context_budget" if after_run else "explicit_request"},
        request_key="compact:" + after_run["id"] if after_run else None,
        parent_id=after_run["job_id"] if after_run else None)
    return {"queued": created or job["status"] == "pending", "job_id": job["id"], "thread_id": main["thread_id"]}


def context_needs_compaction(usage, turn_id):
    if not usage or usage.get("turnId") != turn_id:
        return False
    info = usage.get("tokenUsage", {})
    window, last = info.get("modelContextWindow"), info.get("last", {})
    incoming, outgoing = last.get("inputTokens"), last.get("outputTokens")
    values = (window, incoming, outgoing)
    if any(type(v) is not int or v < 0 for v in values) or window == 0:
        return False
    # Reserve a quarter of the reported window for the next input and response.
    # The cumulative cost counter (tokenUsage.total) is not context occupancy.
    return (incoming + outgoing) * 4 >= window * 3


def finish_compaction(store, run, turn, seen):
    outcome = turn.get("status", "unknown")
    confirmed = outcome == "completed" and seen
    ref = {"thread_id": run["thread_id"], "turn_id": turn["id"]}
    if outcome == "completed" and not seen:
        store.db.execute("UPDATE runs SET state='unknown',error='compaction completion item was not observed' WHERE id=?", (run["id"],))
        store.db.execute("UPDATE jobs SET status='unknown',native_ref=?,error='native compaction is unconfirmed' WHERE id=?", (encode(ref), run["job_id"]))
        return
    store.db.execute("UPDATE runs SET state='exited',outcome=?,turn_id=?,ended_at=?,error=? WHERE id=?",
        (outcome, turn["id"], time.time(), encode(turn["error"]) if turn.get("error") else None, run["id"]))
    store.db.execute("UPDATE jobs SET status='done',payload=NULL,native_ref=?,result_ref=?,updated_at=? WHERE id=?",
        (encode(ref), encode({"compacted": confirmed, **ref}), time.time(), run["job_id"]))
