"""Scheduling and native tool transactions; Codex owns the model loop."""

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import time

from .codex import Codex, process_alive, process_start
from .store import Store, decode, encode, identifier
from .sources import fetch_source, commit_poll, send_outbox, digest
from .tools import MAIN_TOOLS, WORKER_TOOLS
from .approvals import answer_receipt, live_request, validate_response
from .notifications import deliver, notify
from .listing import list_work
from .patrol import progress_target, queue_progress_review, reconcile_progress
from .compaction import context_needs_compaction, finish_compaction, queue_compaction
from .memory import Memory
from .pull_requests import (REASON as PR_REASON, REVIEW_SCHEMA, active_manager, defer_review,
                            pr_status, report_prs, review_gate, review_receipt)


REFERENCES = Path(__file__).resolve().parents[2] / "references"
# Persisted in native history; keep the format across the skill rename.
INPUT_PREFIX = "SUPER_AGENT_INPUT\n"
NAME_PREFIX = "agent-manager · "


def managed_thread_name(chat):
    label = ("主控 · " + (Path(chat["cwd"]).name or chat["cwd"]) if chat["role"] == "main"
             else " ".join(chat["title"].split()) or chat["id"])
    return label if label.startswith(NAME_PREFIX) else NAME_PREFIX + label


class Controller:
    def __init__(self, store):
        self.store = store
        self.clients = {}
        self.loaded = set()
        self.applied_instructions = {}
        self.applied_names = {}
        self.tasks = {}
        self.stop_event = None
        self.polling = set()
        self.lock = None
        self.failed = None
        self.last_recovery = 0
        self.last_notifications = 0
        self.memory_loaded = {}
        self.context_epoch = {}
        self.context_usage = {}
        self.compaction_seen = set()

    def log(self, event, **values):
        print(encode({"at": time.time(), "event": event, **values}), flush=True)

    async def client(self, role):
        if role not in self.clients:
            client = Codex(role, self.store.directory, self.store.main()["cwd"], self.on_request, self.on_event)
            try:
                await client.start()
            except BaseException:
                await client.close()
                raise
            self.clients[role] = client
        return self.clients[role]

    def instructions(self, role):
        name = "orchestrator.md" if role == "main" else "runtime-worker.md"
        return (REFERENCES / name).read_text()

    def config(self, role):
        # These are process/thread capabilities, not a complete native-tool guard.
        return ({"features.shell_tool": False, "agents.enabled": False} if role == "main" else {})

    async def name_thread(self, client, chat):
        name = managed_thread_name(chat)
        if self.applied_names.get(chat["thread_id"]) == name:
            return
        try:
            await client.call("thread/name/set", {"threadId": chat["thread_id"], "name": name}, timeout=5)
        except (RuntimeError, OSError, asyncio.TimeoutError) as error:
            # Optional metadata must not turn a linked thread into an unknown creation.
            self.log("thread_name_unconfirmed", chat_id=chat["id"], thread_id=chat["thread_id"], error_type=type(error).__name__)
            return
        self.applied_names[chat["thread_id"]] = name

    async def bind(self, chat):
        client = await self.client(chat["role"])
        instructions = self.instructions(chat["role"])
        fingerprint = hashlib.sha256(encode({"text": instructions, "config": self.config(chat["role"])}).encode()).hexdigest()
        if chat["id"] in self.loaded and self.applied_instructions.get(chat["id"]) == fingerprint:
            await self.name_thread(client, chat)
            return client
        params = {"cwd": chat["cwd"], "developerInstructions": instructions,
                  "config": self.config(chat["role"])}
        if chat["thread_id"]:
            result = await client.call("thread/resume", {**params, "threadId": chat["thread_id"], "excludeTurns": True})
            if result["thread"]["id"] != chat["thread_id"]:
                raise RuntimeError("resume returned a different thread")
            if Path(result["thread"]["cwd"]).resolve() != Path(chat["cwd"]).resolve():
                raise RuntimeError("resumed native cwd differs from the authorized directory")
        else:
            if chat["binding"] != "unbound":
                raise RuntimeError("thread creation result is unknown; reconcile before retrying")
            self.store.db.execute("UPDATE chats SET binding='creating' WHERE id=?", (chat["id"],))
            try:
                result = await client.call("thread/start", {**params,
                    "sandbox": "workspace-write",
                    "dynamicTools": MAIN_TOOLS if chat["role"] == "main" else WORKER_TOOLS})
            except BaseException:
                self.store.db.execute("UPDATE chats SET binding='unknown' WHERE id=?", (chat["id"],))
                raise
            thread = result["thread"]
            if Path(thread["cwd"]).resolve() != Path(chat["cwd"]).resolve():
                self.store.db.execute("UPDATE chats SET binding='unknown',thread_id=? WHERE id=?", (thread["id"], chat["id"]))
                raise RuntimeError("native cwd differs from the requested work directory")
            self.store.db.execute("UPDATE chats SET thread_id=?,binding='linked' WHERE id=?", (thread["id"], chat["id"]))
            chat["thread_id"] = thread["id"]
        await self.name_thread(client, chat)
        self.loaded.add(chat["id"])
        self.applied_instructions[chat["id"]] = fingerprint
        self.log("thread_bound", chat_id=chat["id"], role=chat["role"], thread_id=chat["thread_id"])
        return client

    async def payload(self, job):
        if job["payload"] is not None:
            return decode(job["payload"])
        ref = decode(job["native_ref"], {})
        chat = self.store.one("SELECT * FROM chats WHERE id=?", (job["chat_id"],))
        if ref.get("turn_id") and chat:
            client = await self.client(chat["role"])
            for turn in await client.history(ref["thread_id"]):
                if turn["id"] != ref["turn_id"]:
                    continue
                for item in turn.get("items", []):
                    if item.get("type") == "userMessage":
                        for content in item.get("content", []):
                            text = content.get("text", "")
                            if text.startswith(INPUT_PREFIX):
                                envelope = json.loads(text[len(INPUT_PREFIX):])
                                if envelope["job_id"] == job["id"]:
                                    return envelope["payload"]
        raise RuntimeError("input is not yet recoverable from native history")

    async def execute(self, run):
        chat = self.store.one("SELECT * FROM chats WHERE id=?", (run["chat_id"],))
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
        try:
            if job["kind"] == "compact":
                if (chat["role"] != "main" or not self.store.main()["entry_thread_id"] or chat["hold"]
                        or self.store.one("SELECT id FROM jobs WHERE kind='question' AND status!='done'")
                        or Memory(self.store).context().get("unavailable")):
                    self.store.finish(run["id"], "skipped", "compaction requires an active idle main, readable memory and no pending questions")
                    return
                client = await self.bind(chat)
                self.store.db.execute("UPDATE runs SET thread_id=?,server_pid=?,server_start=? WHERE id=?",
                    (chat["thread_id"], client.process.pid, client.started, run["id"]))
                await client.call("thread/compact/start", {"threadId": chat["thread_id"]})
                return  # {} acknowledges the request; events confirm completion.
            payload = await self.payload(job)
            gate = review_gate(self.store, job)
            if gate and gate != "ready":
                defer_review(self.store, run, gate)
                return
            if payload.get("reason") == "progress_turn_ended" and not progress_target(self.store, payload):
                self.store.finish(run["id"], "completed", "progress review superseded or blocked; no native input sent")
                return
            client = await self.bind(chat)
            envelope = {"job_id": job["id"], "run_id": run["id"],
                "kind": job["kind"], "chat_id": chat["id"], "cwd": chat["cwd"], "payload": payload}
            memory = Memory(self.store).context()
            if self.memory_loaded.get(chat["id"]) != memory["revision"]:
                envelope["project_context"] = memory
            text = INPUT_PREFIX + encode(envelope)
            self.store.db.execute("UPDATE runs SET thread_id=?,server_pid=?,server_start=? WHERE id=?",
                (chat["thread_id"], client.process.pid, client.started, run["id"]))
            epoch = self.context_epoch.get(chat["id"], 0)
            params = {"threadId": chat["thread_id"], "input": [{"type": "text", "text": text}]}
            if gate and chat["role"] == "main":
                params["outputSchema"] = REVIEW_SCHEMA
            result = await client.call("turn/start", params)
            turn_id = result["turn"]["id"]
            if self.context_epoch.get(chat["id"], 0) == epoch:
                self.memory_loaded[chat["id"]] = memory["revision"]
            with self.store.transaction():
                self.store.db.execute("UPDATE runs SET turn_id=?,state='running' WHERE id=? AND state!='exited'",
                                      (turn_id, run["id"]))
                self.store.db.execute("UPDATE jobs SET native_ref=? WHERE id=?",
                    (encode({"thread_id": chat["thread_id"], "turn_id": turn_id}), job["id"]))
            self.log("turn_started", run_id=run["id"], chat_id=chat["id"], thread_id=chat["thread_id"], turn_id=turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.db.execute("UPDATE runs SET state='unknown',error=? WHERE id=? AND state!='exited'", (str(error), run["id"]))
            with self.store.transaction():
                notify(self.store, "run-unknown:" + run["id"], chat["id"], "failure",
                       parent_id=job["id"], error=str(error))
            self.log("run_unknown", run_id=run["id"], error=str(error))

    def active_run(self, thread_id, turn_id=None):
        run = self.store.one("""SELECT r.* FROM runs r JOIN chats c ON c.id=r.chat_id
            WHERE c.thread_id=? AND r.state!='exited' ORDER BY r.started_at DESC LIMIT 1""", (thread_id,))
        if not run or (turn_id and run["turn_id"] and turn_id != run["turn_id"]):
            raise ValueError("no matching active scheduler run for native request")
        return run

    async def on_request(self, client, message):
        method, params = message["method"], message.get("params", {})
        if method == "item/tool/call":
            result = await self.business_tool(params)
            await client.respond(message["id"], {"contentItems": [{"type": "inputText", "text": encode(result)}], "success": True})
            return
        # A live native request cannot be replaced by an ordinary follow-up turn.
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "item/permissions/requestApproval", "item/tool/requestUserInput", "mcpServer/elicitation/request"}:
            run = self.active_run(params["threadId"], params.get("turnId"))
            key = f"question:{client.process.pid}:{client.started}:{message['id']}"
            context = None
            if method == "item/fileChange/requestApproval":
                try:
                    recent = await client.call("thread/turns/list", {"threadId": params["threadId"],
                        "limit": 1, "itemsView": "full", "sortDirection": "desc"}, timeout=5)
                    context = next((item for turn in recent.get("data", []) for item in turn.get("items", [])
                                    if item.get("id") == params.get("itemId") and item.get("type") == "fileChange"), None)
                except (RuntimeError, asyncio.TimeoutError, OSError) as error:
                    self.log("file_change_context_unavailable", chat_id=run["chat_id"], error_type=type(error).__name__)
            with self.store.transaction():
                question, _ = self.store.enqueue("question", run["chat_id"],
                    {"method": method, "params": params, "request_id": message["id"],
                     "server_pid": client.process.pid, "server_start": client.started, "context": context},
                    request_key=key, parent_id=run["job_id"])
                notify(self.store, question["id"], run["chat_id"], "approval",
                       parent_id=question["id"], question_id=question["id"])
            self.log("needs_input", chat_id=run["chat_id"], method=method)
            return
        await client.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported server request; no automatic approval"}})

    async def business_tool(self, params):
        run = self.active_run(params["threadId"], params.get("turnId"))
        chat = self.store.one("SELECT * FROM chats WHERE id=?", (run["chat_id"],))
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
        if job["kind"] == "compact":
            raise ValueError("compaction is maintenance and does not authorize business tool calls")
        name, args = params["tool"], params.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        allowed = MAIN_TOOLS if chat["role"] == "main" else WORKER_TOOLS
        if name not in {tool["name"] for tool in allowed}:
            raise ValueError("tool is not allowed for this actual thread role")
        if name == "sa_list_work":
            return list_work(self.store)
        if name == "sa_inspect_work":
            # Existing native threads retain their original dynamic tool schemas.
            if args["work_id"] == self.store.main()["id"]:
                return list_work(self.store)
            return await self.inspect(args)
        if name == "sa_memory" and args.get("action") == "read":
            return Memory(self.store).context()
        if job["kind"] == "chat":
            raise ValueError("ordinary conversation does not authorize background work or external writes")
        request_key = f"tool:{params['threadId']}:{params['callId']}"
        hash_value = hashlib.sha256(encode({"tool": name, "arguments": args}).encode()).hexdigest()
        existing = self.store.one("SELECT * FROM jobs WHERE request_key=?", (request_key,))
        if existing:
            if existing["request_hash"] != hash_value:
                raise ValueError("repeated call identity with different arguments")
            return decode(existing["result_ref"])
        payload = await self.payload(job)
        with self.store.transaction():
            existing = self.store.one("SELECT * FROM jobs WHERE request_key=?", (request_key,))
            if existing:
                if existing["request_hash"] != hash_value:
                    raise ValueError("repeated call identity with different arguments")
                return decode(existing["result_ref"])
            if name == "sa_memory":
                if args.get("action") != "put" or args.get("scope", "project") != "project" or not self.store.main()["entry_thread_id"]:
                    raise ValueError("project memory write requires an active manager and action=put")
                result = Memory(self.store).put(args.get("key"), args.get("text"), args.get("source"))
            elif name == "sa_create_work":
                if job["kind"] != "task" and not (job["kind"] == "recover" and payload.get("original_kind") == "task"):
                    raise ValueError("this event does not authorize a new work item")
                operation_key = "create:" + payload.get("original_job_id", job["id"])
                created = self.store.one("SELECT * FROM jobs WHERE request_key=?", (operation_key,))
                if created:
                    result = decode(created["result_ref"])
                else:
                    root = Path(self.store.main()["cwd"])
                    cwd = Path(args.get("cwd") or root).resolve(strict=True)
                    if not cwd.is_dir() or (cwd != root and root not in cwd.parents):
                        raise ValueError("worker cwd must be inside the authorized project")
                    owner = self.store.one("SELECT * FROM chats WHERE role='worker' AND source_id=? AND source_key=? AND closed=0",
                        (job["source_id"], payload.get("object_key"))) if job["source_id"] else None
                    if owner:
                        work_id = owner["id"]
                        self.store.enqueue("followup", work_id, {"text": payload["text"]},
                            request_key="late-source:" + job["id"], parent_id=job["id"])
                    else:
                        work_id = identifier()
                        self.store.db.execute("""INSERT INTO chats(id,role,title,cwd,source_id,source_key,created_at)
                            VALUES(?,?,?,?,?,?,?)""", (work_id, "worker", args["title"], str(cwd), job["source_id"],
                            payload.get("object_key") if job["source_id"] else None, time.time()))
                        worker_job, _ = self.store.enqueue("task", work_id,
                            {"text": payload["text"], "source": payload.get("context"), "work_id": work_id},
                            parent_id=job["id"])
                        self.store.db.execute("UPDATE chats SET request_job_id=? WHERE id=?", (worker_job["id"], work_id))
                    result = {"work_id": work_id, "queued": True}
                    self.store.enqueue("effect", chat["id"], None, request_key=operation_key, status="done", result=result)
                self.store.db.execute("UPDATE jobs SET result_ref=? WHERE id=?", (encode(result), job["id"]))
            elif name in {"sa_report", "sa_return_event"}:
                state = "dropped" if name == "sa_return_event" else args["state"]
                if state not in {"progress", "ready", "blocked", "failed", "dropped"}:
                    raise ValueError("invalid report state")
                if name == "sa_report" and (not isinstance(args["evidence"], list) or not isinstance(args["remaining"], list)):
                    raise ValueError("report evidence and remaining must be arrays")
                ref = {"thread_id": params["threadId"], "turn_id": run["turn_id"] or params.get("turnId"), "call_id": params["callId"]}
                self.store.db.execute("UPDATE chats SET report_state=?,report_ref=? WHERE id=?", (state, encode(ref), chat["id"]))
                if name == "sa_return_event":
                    self.store.db.execute("UPDATE chats SET closed=1 WHERE id=?", (chat["id"],))
                if state != "progress" or args.get("question"):
                    self.store.enqueue("wake", self.store.main()["id"],
                        {"text": "A worker reported. Use sa_inspect_work to read its native report and verify artifacts before responding.",
                         "work_id": chat["id"], "state": state, "reason": args.get("reason"),
                         "question": args.get("question"), "report_ref": ref},
                        request_key="wake:" + request_key, parent_id=job["id"])
                result = {"recorded": True, "state": state}
                if name == "sa_report" and state != "dropped":
                    result["pull_requests"] = report_prs(self.store, chat["id"], args["evidence"])
            elif name in {"sa_followup", "sa_attach_event"}:
                work = self.worker(args["work_id"])
                if work["closed"]:
                    raise ValueError("work has been returned or closed")
                if name == "sa_attach_event" and job["kind"] not in {"task", "recover"}:
                    raise ValueError("only a current authorized task can be attached")
                pr_source = self.store.one("SELECT * FROM sources WHERE id=? AND kind='github-pr'", (job["source_id"],)) if job["source_id"] else None
                if pr_source and (decode(pr_source["config"])["work_id"] != work["id"] or payload.get("reason") != PR_REASON):
                    raise ValueError("PR feedback can continue only its explicitly bound original worker")
                if job["source_id"] and not pr_source and (work["source_id"] != job["source_id"] or work["source_key"] != payload.get("object_key")):
                    raise ValueError("this source is not bound to the target work")
                if work["hold"]:
                    raise ValueError("work is on hold; only a human continue command can release it")
                if payload.get("reason") == "progress_turn_ended":
                    if work["id"] != payload["work_id"] or not progress_target(self.store, payload):
                        raise ValueError("progress review is stale, blocked or already continued; inspect current state")
                followup_key = "pr-followup:" + payload.get("original_job_id", job["id"]) if pr_source else "followup:" + request_key
                existing_followup = self.store.one("SELECT * FROM jobs WHERE request_key=?", (followup_key,))
                if pr_source and not existing_followup and review_gate(self.store, job) != "ready":
                    raise ValueError("PR feedback is inactive, held, blocked, superseded or already queued; preserve the original worker")
                next_payload = {"text": args["text"] if name == "sa_followup" else payload["text"]}
                if pr_source:
                    next_payload.update(reason=PR_REASON, work_id=work["id"], context=payload.get("context"))
                followup, _ = self.store.enqueue("followup", work["id"], next_payload,
                    source_id=job["source_id"] if pr_source else None,
                    request_key=followup_key, parent_id=job["id"])
                result = {"job_id": followup["id"], "queued": True}
            elif name == "sa_set_hold":
                work = self.worker(args["work_id"])
                if job["source_id"] or job["kind"] not in {"task", "followup"}:
                    raise ValueError("only a direct human control request can set this hold")
                self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({"request": args["reason"],
                    "at": time.time(), "requires_human_continue": True}), work["id"]))
                result = {"hold_saved": True, "work_id": work["id"], "stopped": False}
            elif name == "sa_reply":
                work = self.worker(args["work_id"])
                source = self.store.one("SELECT * FROM sources WHERE id=?", (work["source_id"],)) if work["source_id"] else None
                config = decode(source["config"], {}) if source else {}
                if not config.get("allow_reply"):
                    result = {"sent": False, "draft_only": True, "reason": "source has not authorized external replies"}
                else:
                    number = int(work["source_key"].rsplit(":", 1)[1])
                    outbox, _ = self.store.enqueue("outbox", work["id"], {"text": args["text"], "issue": number},
                        source_id=source["id"], request_key="outbox:" + request_key, parent_id=job["id"])
                    result = {"job_id": outbox["id"], "queued": True}
            else:
                raise ValueError("unsupported business tool")
            self.store.enqueue("effect", chat["id"], None, request_key=request_key,
                               status="done", result=result, digest=hash_value, parent_id=job["id"])
        return result

    def worker(self, work_id):
        row = self.store.one("SELECT * FROM chats WHERE id=? AND role='worker'", (work_id,))
        if not row:
            raise ValueError("unknown worker")
        return row

    async def inspect(self, args):
        chat = self.worker(args["work_id"])
        result = {"work_id": chat["id"], "thread_id": chat["thread_id"], "cwd": chat["cwd"],
                  "hold": decode(chat["hold"]), "report_state": chat["report_state"], "turns": [], "artifacts": {}}
        result["pull_requests"] = pr_status(self.store, chat["id"])
        result["native_questions"] = []
        for question in self.store.all("SELECT * FROM jobs WHERE kind='question' AND chat_id=? ORDER BY created_at DESC LIMIT 20", (chat["id"],)):
            response = decode(question["result_ref"], {})
            result["native_questions"].append({**answer_receipt(self.store, question["id"]),
                "decision": response.get("decision"), "response_recorded": question["result_ref"] is not None})
        if chat["thread_id"]:
            result["turns"] = await (await self.client("worker")).history(chat["thread_id"], limit=3)
        if chat["request_job_id"]:
            original = self.store.one("SELECT * FROM jobs WHERE id=?", (chat["request_job_id"],))
            try:
                result["original_request"] = await self.payload(original) if original else None
            except RuntimeError:
                result["original_request"] = None
                result["original_request_error"] = "original input is not recoverable; do not infer authorization"
        for name in args.get("paths", []):
            path = (Path(chat["cwd"]) / name).resolve(strict=True)
            if Path(chat["cwd"]) not in path.parents or not path.is_file():
                raise ValueError("artifact is outside the authorized work directory")
            with path.open("rb") as artifact:
                content = artifact.read(128 * 1024 + 1)
            if len(content) > 128 * 1024:
                result["artifacts"][name] = {"error": "artifact too large for inline reading"}
            else:
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                result["artifacts"][name] = {"text": text, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        return result

    async def on_event(self, client, message):
        method, params = message["method"], message.get("params", {})
        if method == "thread/tokenUsage/updated":
            self.context_usage[params["threadId"]] = params
            run = self.store.one("""SELECT r.* FROM runs r JOIN chats c ON c.id=r.chat_id
                JOIN jobs j ON j.id=r.job_id WHERE c.role='main' AND c.thread_id=? AND r.turn_id=?
                AND r.state='exited' AND r.outcome='completed' AND j.kind!='compact'""",
                (params["threadId"], params["turnId"]))
            if run and context_needs_compaction(params, run["turn_id"]):
                with self.store.transaction():
                    queue_compaction(self.store, after_run=run)
        elif (method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction") or method == "thread/compacted":
            chat = self.store.one("SELECT id FROM chats WHERE thread_id=?", (params["threadId"],))
            if chat:
                self.memory_loaded.pop(chat["id"], None)
                self.context_epoch[chat["id"]] = self.context_epoch.get(chat["id"], 0) + 1
            run = self.store.one("""SELECT r.id FROM runs r JOIN jobs j ON j.id=r.job_id
                WHERE r.thread_id=? AND r.turn_id=? AND r.state!='exited' AND j.kind='compact'""",
                (params["threadId"], params["turnId"]))
            if run:
                self.compaction_seen.add((params["threadId"], params["turnId"]))
        elif method == "turn/started":
            try:
                run = self.active_run(params["threadId"])
            except ValueError:
                return
            self.store.db.execute("UPDATE runs SET turn_id=?,state='running' WHERE id=?", (params["turn"]["id"], run["id"]))
            self.store.db.execute("UPDATE jobs SET native_ref=? WHERE id=?",
                (encode({"thread_id": params["threadId"], "turn_id": params["turn"]["id"]}), run["job_id"]))
        elif method == "turn/completed":
            turn = params["turn"]
            run = self.store.one("SELECT * FROM runs WHERE turn_id=? AND state!='exited'", (turn["id"],))
            if not run:
                return
            await self.completed(run, turn)
        elif method == "serverRequest/resolved":
            for job in self.store.all("SELECT * FROM jobs WHERE kind='question' AND status!='done'"):
                payload = decode(job["payload"], {})
                if (payload.get("server_pid") == client.process.pid and payload.get("server_start") == client.started
                        and payload.get("request_id") == params.get("requestId")
                        and (not params.get("threadId") or payload.get("params", {}).get("threadId") == params["threadId"])):
                    ref = {k: payload.get("params", {}).get(k) for k in ("threadId", "turnId", "itemId")}
                    ref.update(resolved_at=time.time(), request_id=payload["request_id"],
                               server_pid=client.process.pid, server_start=client.started, method=payload["method"],
                               answer_sent=job["status"] in {"inflight", "unknown"} and job["result_ref"] is not None)
                    self.store.db.execute("UPDATE jobs SET status='done',payload=NULL,native_ref=?,updated_at=?,error=? WHERE id=?",
                        (encode(ref), time.time(), None if ref["answer_sent"] else "native request closed without a sent answer", job["id"]))
        elif method == "agent-manager/disconnected":
            self.failed = params["error"]
            self.stop_event.set()
            self.log("disconnected", role=client.role, error=self.failed)
        elif method == "agent-manager/request-error":
            self.log("request_error", **params)

    async def completed(self, run, turn, notify_missing=True):
        outcome = turn.get("status", "unknown")
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
        payload = decode(job["payload"], {})
        is_pr_review = (payload.get("reason") == PR_REASON and self.store.one(
            "SELECT id FROM sources WHERE id=? AND kind='github-pr'", (job["source_id"],))
            and run["chat_id"] == self.store.main()["id"])
        if is_pr_review and outcome == "completed" and not any(i.get("type") == "agentMessage" for i in turn.get("items", [])):
            try:
                chat = self.store.main()
                history = await (await self.client("main")).history(chat["thread_id"], limit=3)
                turn = next((t for t in history if t["id"] == turn["id"]), turn)
            except (RuntimeError, OSError, asyncio.TimeoutError):
                pass  # The missing outcome becomes a failure receipt, never a silent acknowledgement.
        with self.store.transaction():
            actual = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
            if not actual or actual["state"] == "exited":
                return
            chat = self.store.one("SELECT * FROM chats WHERE id=?", (run["chat_id"],))
            if job["kind"] == "compact":
                seen = (chat["thread_id"], turn["id"]) in self.compaction_seen or any(
                    i.get("type") == "contextCompaction" for i in turn.get("items", []))
                finish_compaction(self.store, {**actual, "thread_id": chat["thread_id"]}, turn, seen)
                self.compaction_seen.discard((chat["thread_id"], turn["id"]))
                self.memory_loaded.pop(chat["id"], None)
                self.context_usage.pop(chat["thread_id"], None)
                if outcome != "completed" or not seen:
                    notify(self.store, "compact-unconfirmed:" + run["id"], chat["id"], "failure",
                           parent_id=job["id"], error="Native compaction did not complete with confirmed evidence; inspect status, do not replay it.")
                return
            error = encode(turn["error"]) if turn.get("error") else None
            self.store.db.execute("UPDATE runs SET state='exited',outcome=?,ended_at=?,error=? WHERE id=?",
                (outcome, time.time(), error, run["id"]))
            # The native user item is now the source of delivered input.
            self.store.db.execute("UPDATE jobs SET status='done',payload=NULL,native_ref=?,updated_at=?,error=? WHERE id=?",
                (encode({"thread_id": chat["thread_id"], "turn_id": turn["id"]}), time.time(), error, run["job_id"]))
            self.store.db.execute("UPDATE jobs SET status='done',payload=NULL,error='native turn ended' WHERE kind='question' AND parent_id=? AND status!='done'", (run["job_id"],))
            if chat["role"] == "worker" and notify_missing:
                report = decode(chat["report_ref"], {})
                if report.get("turn_id") != turn["id"]:
                    self.store.enqueue("wake", self.store.main()["id"],
                        {"text": "Worker turn ended without a valid report. Inspect native history; do not infer ready or repeatedly restart it.",
                         "work_id": chat["id"], "native_status": outcome},
                        request_key="missing-report:" + run["id"], parent_id=run["job_id"])
                elif outcome == "completed":
                    queue_progress_review(self.store, chat,
                        {**run, "state": "exited", "outcome": outcome, "turn_id": turn["id"]})
            if chat["role"] == "main" and (job["kind"] == "wake" or
                    (job["kind"] == "recover" and payload.get("original_kind") == "wake")):
                corrective = self.store.one("SELECT id FROM jobs WHERE parent_id=? AND kind IN ('task','followup','recover')",
                                            (job["id"],))
                receipt = review_receipt(self.store, job, payload, turn) if is_pr_review else None
                if receipt:
                    if receipt["outcome"] in {"needs_user", "failed"}:
                        notify(self.store, "main-result:" + run["id"], payload["work_id"],
                            "decision" if receipt["outcome"] == "needs_user" else "failure", parent_id=job["id"],
                            ref={"thread_id": chat["thread_id"], "turn_id": turn["id"]}, error=receipt["summary"])
                elif not corrective:
                    kind = "result"
                    if (outcome != "completed" or payload.get("state") in {"failed", "dropped"}
                            or payload.get("reason") == "progress_turn_ended"):
                        kind = "failure"
                    elif payload.get("question"):
                        kind = "decision"
                    notify(self.store, "main-result:" + run["id"], payload.get("work_id") or chat["id"],
                           kind, parent_id=job["id"], ref={"thread_id": chat["thread_id"], "turn_id": turn["id"]},
                           error=error, report_ref=payload.get("report_ref"))
            if (chat["role"] == "main" and outcome == "completed" and
                    context_needs_compaction(self.context_usage.get(chat["thread_id"]), turn["id"])):
                queue_compaction(self.store, after_run=run)
        self.log("turn_completed", run_id=run["id"], chat_id=chat["id"], turn_id=turn["id"], outcome=outcome)

    async def recover(self, startup=False):
        for run in self.store.all("SELECT * FROM runs WHERE state!='exited' ORDER BY started_at"):
            if run["id"] in self.tasks and not self.tasks[run["id"]].done():
                continue
            if any(c.process.pid == run["server_pid"] and c.started == run["server_start"]
                   for c in self.clients.values()):
                continue
            if process_alive(run["server_pid"], run["server_start"]):
                self.store.db.execute("UPDATE runs SET state='unknown',error=? WHERE id=?", ("previous Codex process is still alive; not resuming concurrently", run["id"]))
                self.log("recovery_waiting_for_process", run_id=run["id"], pid=run["server_pid"])
                continue
            chat = self.store.one("SELECT * FROM chats WHERE id=?", (run["chat_id"],))
            if not chat["thread_id"]:
                if chat["binding"] == "unbound":
                    with self.store.transaction():
                        self.store.db.execute("UPDATE runs SET state='exited',outcome='not_started',ended_at=? WHERE id=?", (time.time(), run["id"]))
                        self.store.db.execute("UPDATE jobs SET status='pending' WHERE id=?", (run["job_id"],))
                else:
                    self.store.db.execute("UPDATE runs SET state='unknown',error=? WHERE id=?", ("native creation result unknown", run["id"]))
                continue
            client = await self.bind(chat)
            turns = await client.history(chat["thread_id"])
            turn = next((t for t in turns if t["id"] == run["turn_id"]), None)
            original = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
            if original["kind"] == "compact":
                if turn and turn.get("status") in {"completed", "failed", "interrupted"}:
                    await self.completed(run, turn)
                else:
                    self.store.db.execute("UPDATE runs SET state='unknown',error='native compaction acceptance or completion cannot be confirmed; not replaying' WHERE id=?", (run["id"],))
                continue
            if turn is None:
                matches = []
                for candidate in turns:
                    for item in candidate.get("items", []):
                        if item.get("type") != "userMessage":
                            continue
                        for content in item.get("content", []):
                            text = content.get("text", "")
                            if not text.startswith(INPUT_PREFIX):
                                continue
                            envelope = json.loads(text[len(INPUT_PREFIX):])
                            if envelope.get("run_id") == run["id"] and envelope.get("job_id") == run["job_id"]:
                                matches.append(candidate)
                if len(matches) == 1:
                    turn = matches[0]
                    self.store.db.execute("UPDATE runs SET turn_id=? WHERE id=?", (turn["id"], run["id"]))
                    self.store.db.execute("UPDATE jobs SET native_ref=? WHERE id=?",
                        (encode({"thread_id": chat["thread_id"], "turn_id": turn["id"]}), run["job_id"]))
                    run["turn_id"] = turn["id"]
                elif run["server_pid"] is None:
                    with self.store.transaction():
                        self.store.db.execute("UPDATE runs SET state='exited',outcome='not_started',ended_at=? WHERE id=?", (time.time(), run["id"]))
                        self.store.db.execute("UPDATE jobs SET status='pending' WHERE id=?", (run["job_id"],))
                    continue
                else:
                    self.store.db.execute("UPDATE runs SET state='unknown',error='native input acceptance cannot be confirmed' WHERE id=?", (run["id"],))
                    continue
            if turn and turn.get("status") in {"completed", "failed", "interrupted"}:
                await self.completed(run, turn, notify_missing=turn["status"] == "completed")
                if turn["status"] == "completed":
                    continue
            else:
                # Only after the owning server has exited and the native thread is idle.
                status = (await client.call("thread/read", {"threadId": chat["thread_id"]}))["thread"]["status"]
                if status.get("type") == "active":
                    self.store.db.execute("UPDATE runs SET state='unknown',error='native thread still active' WHERE id=?", (run["id"],))
                    continue
                self.store.finish(run["id"], "interrupted", "owning Codex process exited before a terminal observation")
            original = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
            try:
                original_payload = await self.payload(original)
            except RuntimeError:
                self.store.db.execute("UPDATE runs SET state='unknown',error='cannot reconcile original input' WHERE id=?", (run["id"],))
                continue
            recovery_payload = {**original_payload, "original_job_id": original_payload.get("original_job_id", original["id"]),
                "original_kind": original_payload.get("original_kind", original["kind"]),
                "text": "Recovery after an interrupted native run. First inspect actual files, Git and external state; do not repeat completed side effects. Continue only the original authorized request below.\n\n" + original_payload.get("text", "")}
            with self.store.transaction():
                self.store.enqueue("recover", chat["id"], recovery_payload,
                    source_id=original["source_id"], request_key="recover:" + run["id"], parent_id=original["id"])
            self.log("recovery_queued", chat_id=chat["id"], thread_id=chat["thread_id"], previous_run=run["id"])
        # A lost POST response is never silently returned to pending.
        if startup:
            self.store.db.execute("UPDATE jobs SET status='unknown' WHERE kind='outbox' AND status='inflight'")
            self.store.db.execute("UPDATE jobs SET status='done',error='native connection ended; request must be reissued' WHERE kind='question' AND status!='done'")
            reconcile_progress(self.store)

    def poll_source(self, source_id):
        # Each thread has its own SQLite connection; network calls hold no write transaction.
        store = Store(self.store.directory)
        try:
            source = store.one("SELECT * FROM sources WHERE id=?", (source_id,))
            events, cursor = fetch_source(store, source)
            return commit_poll(store, source, events, cursor)
        finally:
            store.close()

    async def source_task(self, source_id):
        try:
            count = await asyncio.to_thread(self.poll_source, source_id)
            self.log("source_polled", source_id=source_id, new_jobs=count)
        except Exception as error:
            self.store.db.execute("UPDATE sources SET error=?,next_poll=? WHERE id=?", (str(error), time.time() + 60, source_id))
            source = self.store.one("SELECT * FROM sources WHERE id=? AND kind='github-pr'", (source_id,))
            if source and source["enabled"] and active_manager(self.store):
                config = decode(source["config"])
                key = digest([source_id, source["last_poll"], str(error)])
                with self.store.transaction():
                    notify(self.store, "pr-source-error:" + key, config["work_id"], "failure", parent_id=None,
                           error="Registered PR feedback could not be read; inspect status.sources and restore access. No cursor advanced or review sent.")
            self.log("source_error", source_id=source_id, error=str(error))
        finally:
            self.polling.discard(source_id)

    def deliver_outbox(self, job_id):
        store = Store(self.store.directory)
        try:
            job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            return send_outbox(store, job)
        finally:
            store.close()

    async def outbox_task(self, job):
        try:
            receipt = await asyncio.to_thread(self.deliver_outbox, job["id"])
            self.store.db.execute("UPDATE jobs SET status='done',result_ref=?,payload=NULL,updated_at=? WHERE id=?",
                                  (encode(receipt), time.time(), job["id"]))
            self.log("outbox_delivered", job_id=job["id"], **receipt)
        except Exception as error:
            self.store.db.execute("UPDATE jobs SET status='unknown',error=?,updated_at=? WHERE id=?",
                                  (str(error), time.time(), job["id"]))
            self.log("outbox_unknown", job_id=job["id"], error=str(error))

    def answer_unknown(self, job, error):
        with self.store.transaction():
            self.store.db.execute("UPDATE jobs SET status='unknown',error=?,updated_at=? WHERE id=?",
                                  (str(error), time.time(), job["id"]))
            notify(self.store, "answer-unknown:" + job["id"], job["chat_id"], "failure", parent_id=job["id"],
                   error=f"Native answer delivery is unconfirmed for question {job['id']}: {error}. Read answer-status; do not resend the answer.")

    async def answer_questions(self):
        for selected in self.store.all("SELECT id FROM jobs WHERE kind='question' AND status='pending' AND result_ref IS NOT NULL"):
            job = self.store.one("SELECT * FROM jobs WHERE id=? AND status='pending' AND result_ref IS NOT NULL", (selected["id"],))
            if not job:
                continue
            request = decode(job["payload"])
            response = decode(job["result_ref"])
            chat = self.store.one("SELECT * FROM chats WHERE id=?", (job["chat_id"],))
            client = self.clients.get(chat["role"])
            if not client or request["server_pid"] != client.process.pid or request["server_start"] != client.started:
                self.answer_unknown(job, "native request connection changed")
                continue
            if not live_request(self.store, job):
                self.store.db.execute("UPDATE jobs SET status='done',error='native request is no longer live',updated_at=? WHERE id=?",
                                      (time.time(), job["id"]))
                continue
            validate_response(request, response)
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET status='inflight' WHERE id=?", (job["id"],))
                if (request["method"] in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
                        and response.get("decision") == "cancel"):
                    self.store.db.execute("UPDATE chats SET hold=COALESCE(hold,?) WHERE id=?", (
                        encode({"request": "Human cancelled the native command/file turn; wait for explicit continue",
                                "at": time.time(), "question_id": job["id"], "requires_human_continue": True,
                                "interrupt_requested": True}), job["chat_id"]))
            try:
                await client.respond(request["request_id"], response)
            except (RuntimeError, OSError) as error:
                self.answer_unknown(job, error)

    async def notifications(self):
        try:
            await deliver(self.store, await self.client("main"))
        except (RuntimeError, OSError) as error:
            self.log("notification_error", error=str(error))

    async def honor_holds(self):
        for row in self.store.all("""SELECT c.*,r.turn_id,r.id AS run_id FROM chats c JOIN runs r ON r.chat_id=c.id
                WHERE c.hold IS NOT NULL AND r.state='running' AND r.turn_id IS NOT NULL"""):
            hold = decode(row["hold"])
            if hold.get("interrupt_requested"):
                continue
            client = self.clients.get(row["role"])
            if client:
                await client.call("turn/interrupt", {"threadId": row["thread_id"], "turnId": row["turn_id"]})
                hold["interrupt_requested"] = True
                self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode(hold), row["id"]))

    async def run(self):
        self.stop_event = asyncio.Event()
        self.lock = (self.store.directory / "daemon.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("another agent-manager run owns this state directory") from None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop_event.set)
        try:
            (self.store.directory / "daemon.json").write_text(encode({"pid": os.getpid(), "started": process_start(os.getpid())}))
            await self.recover(startup=True)
            await self.bind(self.store.main())
            self.log("daemon_ready")
            while not self.stop_event.is_set():
                if time.monotonic() - self.last_recovery > 5:
                    await self.recover()
                    self.last_recovery = time.monotonic()
                for source in self.store.all("SELECT * FROM sources WHERE enabled=1 AND kind!='push' AND next_poll<=?", (time.time(),)):
                    if source["kind"] == "github-pr" and not active_manager(self.store):
                        continue
                    if source["id"] not in self.polling:
                        self.polling.add(source["id"])
                        task = asyncio.create_task(self.source_task(source["id"]))
                        self.tasks["source:" + source["id"]] = task
                for outbox in self.store.all("""SELECT j.* FROM jobs j JOIN chats c ON c.id=j.chat_id
                        WHERE j.kind='outbox' AND ((j.status='pending' AND c.hold IS NULL)
                        OR (j.status='unknown' AND j.updated_at<?))""", (time.time() - 60,)):
                    task_key = "outbox:" + outbox["id"]
                    if task_key in self.tasks:
                        continue
                    if outbox["status"] == "pending":
                        self.store.db.execute("UPDATE jobs SET status='inflight',updated_at=? WHERE id=?", (time.time(), outbox["id"]))
                    self.tasks[task_key] = asyncio.create_task(self.outbox_task(outbox))
                if time.monotonic() - self.last_notifications > 5 and "notifications" not in self.tasks:
                    self.tasks["notifications"] = asyncio.create_task(self.notifications())
                    self.last_notifications = time.monotonic()
                await self.answer_questions()
                await self.honor_holds()
                for chat in self.store.all("SELECT * FROM chats WHERE closed=0 ORDER BY created_at"):
                    run = self.store.claim(chat["id"])
                    if run:
                        self.tasks[run["id"]] = asyncio.create_task(self.execute(run))
                self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
                try:
                    await asyncio.wait_for(self.stop_event.wait(), .25)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.log("daemon_stopping")
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            for client in self.clients.values():
                await client.close()
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            (self.store.directory / "daemon.json").unlink(missing_ok=True)
        if self.failed:
            raise RuntimeError(self.failed)
