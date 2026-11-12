"""Source polling and delivery. No model is needed to detect a new event."""

import copy
import hashlib
import json
import re
import subprocess
import time

from .store import encode, decode


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def gh_api(path, *, paginate=False, body=None):
    args = ["gh", "api", "--hostname", "github.com", "--method", "GET" if body is None else "POST",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if paginate:
        args += ["--paginate", "--slurp"]
    if body is not None:
        args += ["--input", "-"]
    result = subprocess.run(args + [path], input=encode(body) if body is not None else None,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("GitHub API request failed; source cursor was not advanced")
    return json.loads(result.stdout)


def github_poll(config, cursor, own_ids=(), own_markers=()):
    repo = config["repo"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must be owner/name")
    issue_number = config.get("issue")
    if issue_number:
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("invalid issue number")
        issues = [gh_api(f"repos/{repo}/issues/{issue_number}")]
    else:
        pages = gh_api(f"repos/{repo}/issues?state=all&per_page=100&since={config['since']}", paginate=True)
        issues = [issue for page in pages for issue in page]
    old = cursor or {"objects": {}}
    objects = copy.deepcopy(old.get("objects", {}))
    events = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        number = issue["number"]
        group = f"github:{repo.lower()}:{number}"
        pages = gh_api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True)
        comments = {}
        for page in pages:
            for comment in page:
                if comment["issue_url"] != issue["url"]:
                    raise ValueError("comment belongs to another issue")
                if comment["id"] in comments and comment != comments[comment["id"]]:
                    raise ValueError("comment changed during pagination; retry")
                comments[comment["id"]] = comment
        initial = f"issue:{issue['id']}" not in objects
        def own(comment):
            return (str(comment["id"]) in set(map(str, own_ids)) or
                    any((comment.get("body") or "").endswith(marker) for marker in own_markers))
        visible = [c for _, c in sorted(comments.items()) if not own(c)]
        snapshots = [("issue", issue, {"title": issue["title"], "body": issue.get("body") or ""})]
        snapshots += [("comment", c, {"body": c.get("body") or ""}) for _, c in sorted(comments.items())]
        for entity, item, content in snapshots:
            key = f"{entity}:{item['id']}"
            previous = objects.get(key)
            changed = not previous or previous["hash"] != digest(content)
            revision = (previous["revision"] + 1 if previous else 1) if changed else previous["revision"]
            objects[key] = {"hash": digest(content), "revision": revision}
            if not changed or (entity == "comment" and (initial or own(item))):
                continue
            context = {"repo": repo, "issue": number, "url": item["html_url"],
                       "entity": entity, "author": item["user"]["login"], "content": content}
            if initial:
                context["existing_comments"] = [
                    {"id": c["id"], "author": c["user"]["login"], "body": c.get("body") or ""}
                    for c in visible]
            events.append({"key": f"{key}:{revision}:{digest(content)}", "object_key": group,
                           "text": "GitHub issue input (source data):\n" + encode(context),
                           "context": context})
    return events, {"objects": objects}


def command_poll(config, cursor):
    argv = config.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
        raise ValueError("command source requires a nonempty argv array")
    result = subprocess.run(argv, input=encode({"cursor": cursor}), capture_output=True,
                            text=True, timeout=60, cwd=config["cwd"])
    if result.returncode:
        raise RuntimeError("source command failed; cursor was not advanced")
    response = json.loads(result.stdout)
    if not isinstance(response.get("events"), list) or "cursor" not in response:
        raise ValueError("source command must return events and cursor")
    for event in response["events"]:
        if not all(isinstance(event.get(k), str) and event[k] for k in ("key", "object_key", "text")):
            raise ValueError("source event requires key, object_key, text")
    return response["events"], response["cursor"]


def fetch_source(store, source):
    config, cursor = decode(source["config"]), decode(source["cursor"])
    if source["kind"] == "github-pr":
        from .pull_requests import active_manager, github_pr_poll
        work = store.one("SELECT id FROM chats WHERE id=? AND role='worker' AND closed=0", (config["work_id"],))
        if not active_manager(store) or not work:
            return [], cursor
        return github_pr_poll(config, cursor)
    if source["kind"] == "github":
        outbound = store.all("SELECT id,result_ref FROM jobs WHERE source_id=? AND kind='outbox'", (source["id"],))
        own = [decode(j["result_ref"], {}).get("remote_id") for j in outbound]
        return github_poll(config, cursor, [x for x in own if x is not None],
                           [f"<!-- super-agent:{j['id']} -->" for j in outbound])
    if source["kind"] == "command":
        return command_poll(config, cursor)
    raise ValueError("unknown source kind")


def commit_poll(store, source, events, cursor):
    count = 0
    with store.transaction():
        current = store.one("SELECT cursor,enabled FROM sources WHERE id=?", (source["id"],))
        if not current or not current["enabled"] or current["cursor"] != source["cursor"]:
            return 0
        main = store.main()
        if source["kind"] == "github-pr":
            from .pull_requests import active_manager, REASON
            config = decode(source["config"])
            if not active_manager(store):
                return 0
            work = store.one("SELECT id FROM chats WHERE id=? AND role='worker' AND closed=0", (config["work_id"],))
            if not work:
                store.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (source["id"],))
                return 0
            for event in events:
                _, inserted = store.enqueue("wake", main["id"],
                    {**event, "reason": REASON, "work_id": work["id"]}, source_id=source["id"], source_key=event["key"])
                count += int(inserted)
            merged = (cursor or {}).get("pull", {}).get("merged", False)
            store.db.execute("UPDATE sources SET cursor=?,last_poll=?,next_poll=?,error=NULL,enabled=? WHERE id=?",
                (encode(cursor), time.time(), time.time() + 60, int(bool(work) and not merged), source["id"]))
            return count
        for event in events:
            owner = store.one("SELECT * FROM chats WHERE source_id=? AND source_key=? AND closed=0",
                              (source["id"], event["object_key"]))
            job, inserted = store.enqueue("followup" if owner else "task",
                owner["id"] if owner else main["id"], event,
                source_id=source["id"], source_key=event["key"])
            count += int(inserted)
        store.db.execute("UPDATE sources SET cursor=?,last_poll=?,next_poll=?,error=NULL WHERE id=?",
                         (encode(cursor), time.time(), time.time() + 60, source["id"]))
    return count


def send_outbox(store, job):
    source = store.one("SELECT * FROM sources WHERE id=?", (job["source_id"],))
    config = decode(source["config"])
    if not config.get("allow_reply") or source["kind"] != "github":
        raise ValueError("this source does not authorize external replies")
    payload = decode(job["payload"])
    # Keep the persisted receipt format so existing issue replies remain identifiable.
    marker = f"<!-- super-agent:{job['id']} -->"
    endpoint = f"repos/{config['repo']}/issues/{payload['issue']}/comments"
    # Reconcile a lost response before attempting any repeat POST.
    for page in gh_api(endpoint + "?per_page=100", paginate=True):
        for comment in page:
            if comment.get("body", "") == payload["text"] + "\n\n" + marker:
                return {"remote_id": comment["id"], "url": comment["html_url"]}
    if job["status"] == "unknown":
        raise RuntimeError("outbox result is unknown; no confirmed receipt, not resending")
    response = gh_api(endpoint, body={"body": payload["text"] + "\n\n" + marker})
    return {"remote_id": response["id"], "url": response["html_url"]}
