"""Bind GitHub PR feedback to its original worker; never create review workers."""

import copy
import json
import re
import time

from .bindings import Bindings
from .sources import digest, gh_api
from .store import decode, encode, identifier


REASON = "pull_request_comments"
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?")
REVIEW_SCHEMA = {"type": "object", "properties": {
    "outcome": {"type": "string", "enum": ["continued", "no_action", "needs_user"]},
    "summary": {"type": "string"}}, "required": ["outcome", "summary"], "additionalProperties": False}


def active_manager(store):
    main = store.main()
    return bool(main["entry_thread_id"] and Bindings().allows(main["cwd"], store.directory, main["entry_thread_id"]))


def watch_pr(store, work_id, url, *, explicit=False):
    """Called inside the caller's transaction. Register only an exact evidence URL."""
    match = PR_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise ValueError("PR evidence must be an exact https://github.com/owner/repo/pull/number URL")
    if not active_manager(store):
        raise ValueError("PR monitoring requires an activated manager")
    work = store.one("SELECT * FROM chats WHERE id=? AND role='worker' AND closed=0", (work_id,))
    if not work:
        raise ValueError("PR monitoring requires this manager's existing open worker")
    repo, number = match[1], int(match[2])
    for source in store.all("SELECT * FROM sources WHERE kind='github-pr'"):
        config = decode(source["config"])
        if config["repo"].lower() != repo.lower() or config["pull"] != number:
            continue
        if config["work_id"] != work_id:
            raise ValueError("this PR is already bound to another work; preserve its original owner")
        if explicit:
            store.db.execute("UPDATE sources SET enabled=1,next_poll=0 WHERE id=?", (source["id"],))
        return {"source_id": source["id"], "work_id": work_id, "url": f"https://github.com/{repo}/pull/{number}",
                "enabled": bool(source["enabled"] or explicit), "reused": True}
    key = identifier()
    config = {"repo": repo, "pull": number, "work_id": work_id, "allow_reply": False}
    store.db.execute("INSERT INTO sources(id,kind,config) VALUES(?,?,?)", (key, "github-pr", encode(config)))
    return {"source_id": key, "work_id": work_id, "url": f"https://github.com/{repo}/pull/{number}",
            "enabled": True, "reused": False}


def report_prs(store, work_id, evidence):
    receipts = []
    for value in dict.fromkeys(v for v in evidence if isinstance(v, str) and PR_URL.fullmatch(v)):
        try:
            receipts.append(watch_pr(store, work_id, value))
        except ValueError as error:
            receipts.append({"url": value, "registered": False, "error": str(error)})
    return receipts


def github_pr_poll(config, cursor):
    repo, number = config["repo"], config["pull"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or type(number) is not int or number < 1:
        raise ValueError("invalid registered GitHub PR")
    pr = gh_api(f"repos/{repo}/pulls/{number}")
    if pr["number"] != number or pr["base"]["repo"]["full_name"].lower() != repo.lower():
        raise ValueError("GitHub returned a different PR")
    snapshot = {"repo": repo, "pull": number, "url": pr["html_url"], "state": pr["state"],
                "merged": pr["merged"], "head_sha": pr["head"]["sha"]}
    objects = copy.deepcopy((cursor or {}).get("objects", {}))
    if pr["state"] != "open" or pr["merged"]:
        return [], {"objects": objects, "pull": snapshot}
    snapshots, pending_reviews = [], set()
    for entity, endpoint in (("comment", f"issues/{number}/comments"),
                             ("review", f"pulls/{number}/reviews"),
                             ("review_comment", f"pulls/{number}/comments")):
        items = {}
        for page in gh_api(f"repos/{repo}/{endpoint}?per_page=100", paginate=True):
            for item in page:
                expected = pr["url"].replace("/pulls/", "/issues/") if entity == "comment" else pr["url"]
                if item["issue_url" if entity == "comment" else "pull_request_url"].lower() != expected.lower():
                    raise ValueError("comment or review belongs to another PR")
                if item["id"] in items and item != items[item["id"]]:
                    raise ValueError("PR feedback changed during pagination; retry without advancing cursor")
                items[item["id"]] = item
        for _, item in sorted(items.items()):
            if entity == "review" and item["state"] == "PENDING":
                pending_reviews.add(item["id"])
                continue
            if entity == "review_comment" and item.get("pull_request_review_id") in pending_reviews:
                continue
            content = {"body": item.get("body") or ""}
            if entity == "review":
                content["state"] = item["state"]
            snapshots.append((entity, item, content))
    changed = []
    for entity, item, content in snapshots:
        key, hash_value = f"{entity}:{item['id']}", digest(content)
        old = objects.get(key)
        if old and old["hash"] == hash_value:
            continue
        revision = old["revision"] + 1 if old else 1
        objects[key] = {"hash": hash_value, "revision": revision}
        change = {"entity": entity, "id": item["id"], "revision": revision, "hash": hash_value,
                  "url": item["html_url"], "author": (item.get("user") or {}).get("login"), **content}
        for field in ("created_at", "updated_at", "submitted_at", "path", "line", "original_line",
                      "side", "start_line", "in_reply_to_id", "pull_request_review_id", "commit_id", "original_commit_id"):
            if field in item:
                change[field] = item[field]
        changed.append(change)
        if len(changed) == 20:
            break  # Unqueued objects keep their previous revision until the next poll.
    context = {"pull_request": snapshot, "changes": changed}
    events = [{"key": "pr-feedback:" + digest(changed), "object_key": f"github-pr:{repo.lower()}:{number}",
               "text": "GitHub PR feedback (source data). Inspect the original request and current PR before deciding. Continue only the bound original worker within its authorization; do not create a worker, approve requests, release holds, merge, publish, reply or resolve discussions without authorization.",
               "context": context}] if changed else []
    return events, {"objects": objects, "pull": snapshot}


def review_gate(store, job):
    """None for other sources; a narrow gate for both PR wakes and their followups."""
    source = store.one("SELECT * FROM sources WHERE id=?", (job["source_id"],)) if job["source_id"] else None
    if not source or source["kind"] != "github-pr":
        return None
    config = decode(source["config"])
    if job["kind"] == "recover" and job["chat_id"] == config["work_id"]:
        original_id = decode(job["payload"], {}).get("original_job_id")
        original = store.one("SELECT native_ref FROM jobs WHERE id=? AND chat_id=?", (original_id, config["work_id"]))
        if original and decode(original["native_ref"], {}).get("turn_id"):
            return None  # Already accepted worker work follows the existing recovery/hold rules.
    work = store.one("SELECT * FROM chats WHERE id=? AND role='worker'", (config["work_id"],))
    if not source["enabled"] or not work or work["closed"]:
        return "skipped"
    if (not active_manager(store) or work["hold"]
            or decode(source["cursor"], {}).get("pull", {}).get("state", "open") != "open"
            or store.one("SELECT id FROM runs WHERE chat_id=? AND job_id!=? AND state!='exited'", (work["id"], job["id"]))
            or store.one("SELECT id FROM jobs WHERE chat_id=? AND kind='question' AND status!='done'", (work["id"],))
            or store.one("SELECT id FROM jobs WHERE chat_id=? AND id!=? AND status='pending' AND kind IN ('task','followup','recover')",
                         (work["id"], job["id"]))):
        return "deferred"
    return "ready"


def defer_review(store, run, gate):
    with store.transaction():
        store.db.execute("UPDATE runs SET state='exited',outcome='not_started',ended_at=? WHERE id=?", (time.time(), run["id"]))
        store.db.execute("UPDATE jobs SET status=?,result_ref=?,error=? WHERE id=?",
                         ("done" if gate == "skipped" else "pending", encode({"pr_review": {"outcome": gate}}),
                          "PR review not dispatched; no native input sent", run["job_id"]))


def pr_status(store, work_id):
    result = []
    for source in store.all("SELECT * FROM sources WHERE kind='github-pr' ORDER BY id"):
        config = decode(source["config"])
        if config["work_id"] != work_id:
            continue
        receipt = store.one("SELECT result_ref FROM jobs WHERE source_id=? AND kind IN ('wake','recover') AND status='done' ORDER BY updated_at DESC,id DESC LIMIT 1", (source["id"],))
        result.append({"source_id": source["id"], "url": f"https://github.com/{config['repo']}/pull/{config['pull']}",
            "enabled": bool(source["enabled"]), "last_poll": source["last_poll"], "error": source["error"],
            "pull_request": decode(source["cursor"], {}).get("pull"),
            "pending_reviews": store.one("SELECT count(*) AS n FROM jobs WHERE source_id=? AND kind IN ('wake','recover') AND status!='done'", (source["id"],))["n"],
            "latest_receipt": decode(receipt["result_ref"], {}).get("pr_review") if receipt else None})
    return result


def review_receipt(store, job, payload, turn):
    """Read the native structured final answer, then record its handling outcome."""
    source = store.one("SELECT * FROM sources WHERE id=? AND kind='github-pr'", (job["source_id"],)) if job["source_id"] else None
    if not source or payload.get("reason") != REASON:
        return None
    finals = [i.get("text", "") for i in turn.get("items", []) if i.get("type") == "agentMessage"
              and i.get("phase") in {None, "final_answer"}]
    try:
        receipt = json.loads(finals[-1]) if finals else None
        if (turn.get("status") != "completed" or not isinstance(receipt, dict)
                or set(receipt) != {"outcome", "summary"} or receipt["outcome"] not in {"continued", "no_action", "needs_user"}
                or not isinstance(receipt["summary"], str) or not receipt["summary"].strip()):
            raise ValueError("missing PR review outcome")
        child = store.one("SELECT id FROM jobs WHERE request_key=? AND kind='followup' AND source_id=?",
                          ("pr-followup:" + payload.get("original_job_id", job["id"]), source["id"]))
        if receipt["outcome"] == "continued" and not child:
            raise ValueError("continued without an original-worker followup receipt")
        if child and receipt["outcome"] == "no_action":
            receipt["outcome"] = "continued"
        receipt = {**receipt, "summary": receipt["summary"][:6000], "followup_job_id": child["id"] if child else None}
    except (ValueError, TypeError):
        receipt = {"outcome": "failed", "summary": "PR feedback review has no valid handling receipt; inspect the original native turn before retrying."}
    store.db.execute("UPDATE jobs SET result_ref=? WHERE id=?", (encode({"pr_review": receipt}), job["id"]))
    return receipt
