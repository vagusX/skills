"""Command-line entry point. All user text travels through stdin/argv, not a shell."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

from .codex import Codex, process_alive
from .activation import CONTROL_COMMANDS, background_environment, check_binding, read_session_context, require_active, require_entry, session_context
from .approvals import queue_answer, answer_receipt
from .notifications import acknowledge, bind_entry, inbox, notify
from .controller import Controller
from .listing import list_work
from .panel import ensure_panel, panel_record, panel_url, public_record, serve_panel, stop_panel
from .patrol import bind_heartbeat, heartbeat_plan, heartbeat_receipt, patrol
from .compaction import queue_compaction
from .memory import Memory
from .bindings import Bindings
from .hooks import hook_plan, install_hook
from .pull_requests import watch_pr
from .sources import commit_poll, fetch_source
from .store import Store, data_directory, decode, encode, identifier


def parser():
    p = argparse.ArgumentParser(prog="agent-manager", description="Four-table scheduling for native Codex threads")
    p.add_argument("--project", default=os.getcwd(), help="project/module directory; defaults to cwd")
    p.add_argument("--state-dir", help="isolated state directory; normally derived from project")
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("setup", "run", "start", "stop", "status", "context", "panel", "panel-run", "panel-stop", "unbind-entry"):
        commands.add_parser(name)
    commands.add_parser("list", help="read one snapshot of this manager's registered Codex workers and pending native questions")
    commands.add_parser("patrol", help="check an active entry once and queue deduplicated progress reviews; never wait or approve")
    commands.add_parser("heartbeat-plan", help="describe the App heartbeat for this active entry and its saved receipt")
    commands.add_parser("hook-plan", help="review the manager-only UserPromptSubmit hook without installing it")
    commands.add_parser("hook-install", help="append the conditional prompt hook; native trust is reviewed separately")
    cmd = commands.add_parser("watch-pr", help="bind GitHub PR feedback to an existing worker; do not create work or authorize replies")
    cmd.add_argument("work_id")
    cmd.add_argument("url")
    commands.add_parser("compact", help="queue native compaction of this manager's background main; never take the App entry")
    cmd = commands.add_parser("memory", help="read a bounded project-first memory index, or one explicit scope")
    cmd.add_argument("--scope", choices=("project", "global"))
    for name in ("memory-put", "memory-forget"):
        cmd = commands.add_parser(name)
        cmd.add_argument("key")
        cmd.add_argument("--scope", choices=("project", "global"), default="project")
        if name == "memory-put":
            cmd.add_argument("--source", required=True, help="real evidence or the human statement confirming the fact")
            cmd.add_argument("text", nargs="?", help="omit to read verified memory text from stdin")
    cmd = commands.add_parser("heartbeat-bind", help="save a verified App automation ID for this active entry")
    cmd.add_argument("automation_id")
    cmd = commands.add_parser("bind-entry", help="bind the user-owned entry chat without resuming or taking its writer")
    cmd.add_argument("thread_id", nargs="?", default=os.environ.get("CODEX_THREAD_ID"))
    cmd = commands.add_parser("inbox", help="read entry notifications and their actual native context")
    cmd.add_argument("event_id", nargs="?")
    cmd = commands.add_parser("ack", help="acknowledge that the bound entry chat has read one notification")
    cmd.add_argument("event_id")
    cmd.add_argument("--thread", help="entry thread ID; defaults to CODEX_THREAD_ID")
    cmd = commands.add_parser("answer-status", help="read the original native request's delivery receipt")
    cmd.add_argument("question_id")
    for name in ("submit", "chat", "followup"):
        cmd = commands.add_parser(name)
        if name == "followup":
            cmd.add_argument("work_id")
        cmd.add_argument("text", nargs="?", help="omit to read the original text from stdin")
    cmd = commands.add_parser("read")
    cmd.add_argument("chat_id", nargs="?")
    cmd = commands.add_parser("wait")
    cmd.add_argument("job_id")
    cmd.add_argument("--timeout", type=float, default=60)
    cmd = commands.add_parser("wait-start", help="briefly confirm native worker startup; never wait for completion")
    cmd.add_argument("job_id")
    cmd.add_argument("--timeout", type=float, default=30, help="startup confirmation budget; 0 is one snapshot")
    cmd = commands.add_parser("pause")
    cmd.add_argument("work_id")
    cmd.add_argument("reason", nargs="?", default="Human requested a pause; wait for explicit continue")
    cmd = commands.add_parser("continue")
    cmd.add_argument("work_id")
    cmd.add_argument("text", nargs="?", help="omit to read stdin; empty input uses the original authorized request")
    cmd = commands.add_parser("answer")
    cmd.add_argument("question_id")
    cmd.add_argument("response", nargs="?", help="native response JSON; omit to read stdin")
    cmd = commands.add_parser("source-add")
    group = cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--github", metavar="OWNER/REPO")
    group.add_argument("--command-json", help="JSON argv array for an authorized polling command")
    group.add_argument("--push", metavar="NAME", help="bind an external trigger using ingest")
    cmd.add_argument("--issue", type=int, help="limit GitHub to one explicitly selected issue")
    cmd.add_argument("--allow-reply", action="store_true", help="explicitly authorize replies to this GitHub source")
    cmd = commands.add_parser("poll")
    cmd.add_argument("source_id", nargs="?")
    cmd = commands.add_parser("ingest")
    cmd.add_argument("source_id")
    cmd = commands.add_parser("source-disable")
    cmd.add_argument("source_id")
    return p


def stdin_or(value):
    return value if value is not None else sys.stdin.read()


async def read_chat(store, chat_id):
    chat = store.one("SELECT * FROM chats WHERE id=?", (chat_id or store.main()["id"],))
    if not chat:
        raise ValueError("unknown chat")
    if not chat["thread_id"]:
        return {"chat": chat, "turns": []}
    async def request(client, message):
        raise RuntimeError("read-only history client received an unexpected server request")
    async def event(client, message):
        pass
    client = Codex("history", store.directory, store.main()["cwd"], request, event)
    try:
        await client.start()
        turns = await client.history(chat["thread_id"])
        return {"chat_id": chat["id"], "thread_id": chat["thread_id"], "turns": [
            {"id": t["id"], "status": t["status"], "items": [i for i in t.get("items", [])
             if i.get("type") in {"agentMessage", "dynamicToolCall"}]} for t in turns[-10:]]}
    finally:
        await client.close()


def daemon_record(store):
    try:
        record = json.loads((store.directory / "daemon.json").read_text())
        return record if process_alive(record["pid"], record["started"]) else None
    except (OSError, ValueError, KeyError):
        return None


async def entry_command(store, args):
    if args.command == "bind-entry":
        check_binding(store, args.thread_id)
    async def request(client, message):
        raise RuntimeError("queue/history client received an unexpected server request")
    async def event(client, message):
        pass
    client = Codex("entry-history", store.directory, store.main()["cwd"], request, event)
    try:
        await client.start()
        if args.command == "inbox":
            return await inbox(store, client, args.event_id)
        if not args.thread_id:
            raise ValueError("bind-entry requires a real thread ID or CODEX_THREAD_ID")
        thread = (await client.call("thread/read", {"threadId": args.thread_id}))["thread"]
        if thread["id"] != args.thread_id:
            raise ValueError("native thread/read returned a different ID")
        if not isinstance(thread.get("cwd"), str) or not Path(thread["cwd"]).is_dir():
            raise ValueError("native entry cwd is unavailable; do not guess its hook identity")
        with Bindings().claim(store, args.thread_id, thread["cwd"]):
            with store.transaction():
                check_binding(store, args.thread_id)
                result = bind_entry(store, args.thread_id)
                for question in store.all("SELECT * FROM jobs WHERE kind='question' AND status='pending' AND result_ref IS NULL"):
                    notify(store, question["id"], question["chat_id"], "approval", parent_id=question["id"], question_id=question["id"])
        return result
    finally:
        await client.close()


def dispatch(args, store):
    command = args.command
    if command == "context":
        return session_context(store)
    if command == "setup":
        return {"main": store.setup(args.project), "state_directory": str(store.directory)}
    store.main()
    if command in CONTROL_COMMANDS:
        require_entry(store, command)
    if command == "patrol":
        return patrol(store)
    if command in {"heartbeat-plan", "heartbeat-bind"}:
        return heartbeat_plan(store) if command == "heartbeat-plan" else bind_heartbeat(store, args.automation_id)
    if command in {"hook-plan", "hook-install"}:
        registry = Bindings()
        main = store.main()
        record = registry.read().get(main["cwd"])
        if not record or record["state_directory"] != str(store.directory) or not registry.live(main["cwd"], record):
            raise ValueError("enroll this actual entry with setup-agent-manager before configuring its prompt hook")
        return hook_plan() if command == "hook-plan" else install_hook()
    if command == "list":
        return list_work(store)
    if command == "watch-pr":
        with store.transaction():
            return watch_pr(store, args.work_id, args.url, explicit=True)
    if command == "memory":
        memory = Memory(store)
        return memory.read(args.scope) if args.scope else memory.context()
    if command in {"memory-put", "memory-forget", "compact"}:
        if not store.main()["entry_thread_id"]:
            raise ValueError("memory writes and compaction require an activated manager")
        if command == "compact":
            with store.transaction():
                result = queue_compaction(store)
            return {**result, "daemon": daemon_record(store)}
        memory = Memory(store)
        return (memory.put(args.key, stdin_or(args.text), args.source, args.scope)
                if command == "memory-put" else memory.forget(args.key, args.scope))
    if command in {"bind-entry", "inbox"}:
        return asyncio.run(entry_command(store, args))
    if command == "unbind-entry":
        heartbeat = heartbeat_receipt(store)
        with Bindings().release(store):
            with store.transaction():
                store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        return {"entry_thread_id": None, "already_queued_messages_unchanged": True,
                "heartbeat_to_pause": heartbeat}
    if command == "ack":
        return acknowledge(store, args.event_id, args.thread)
    if command == "answer-status":
        return answer_receipt(store, args.question_id)
    if command == "panel-run":
        serve_panel(store)
        return {"panel_stopped": True}
    if command == "panel":
        record = ensure_panel(store, args.project, open_now=True)
        return {**public_record(record), "url": panel_url(record),
                **({"open_error": record["open_error"]} if record.get("open_error") else {})}
    if command == "panel-stop":
        return stop_panel(store)
    if command == "run":
        ensure_panel(store, args.project)
        asyncio.run(Controller(store).run())
        return {"stopped": True}
    if command == "start":
        if record := daemon_record(store):
            panel = ensure_panel(store, args.project)
            return {"already_running": True, **record, "panel": public_record(panel)}
        launcher = Path(__file__).resolve().parents[1] / "agent_manager.py"
        with (store.directory / "daemon.log").open("ab", buffering=0) as log:
            process = subprocess.Popen([sys.executable, str(launcher), "--project", args.project,
                "--state-dir", str(store.directory), "run"], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, env=background_environment())
        for _ in range(225):
            if process.poll() is not None:
                raise RuntimeError("background startup failed; read daemon.log")
            record = daemon_record(store)
            main = store.main()
            if record and main["thread_id"]:
                panel = ensure_panel(store, args.project)
                return {"running": True, **record, "main_thread_id": main["thread_id"], "panel": public_record(panel)}
            time.sleep(.2)
        raise RuntimeError("background startup is not yet confirmed; inspect status and daemon.log before retrying")
    if command == "stop":
        record = daemon_record(store)
        if not record:
            return {"running": False, "panel": stop_panel(store)}
        os.kill(record["pid"], signal.SIGTERM)
        for _ in range(150):
            if not process_alive(record["pid"], record["started"]):
                return {"stopped": True, "pid": record["pid"], "panel": stop_panel(store)}
            time.sleep(.2)
        return {"stop_requested": True, "confirmed_stopped": False, "pid": record["pid"]}
    if command == "status":
        return {**store.status(), "daemon": daemon_record(store),
                "entry_thread_id": store.main()["entry_thread_id"],
                "heartbeat": heartbeat_receipt(store),
                "notifications": store.all("SELECT * FROM jobs WHERE kind='notification' ORDER BY created_at DESC LIMIT 80"),
                "panel": public_record(panel_record(store)),
                "questions": store.all("SELECT * FROM jobs WHERE kind='question' AND status!='done'")}
    if command in {"submit", "chat", "followup"}:
        return store.submit(stdin_or(args.text), kind="task" if command == "submit" else command,
                            chat_id=args.work_id if command == "followup" else None)
    if command == "read":
        return asyncio.run(read_chat(store, args.chat_id))
    if command == "wait-start":
        deadline = time.monotonic() + max(0, args.timeout)
        while True:
            receipt = store.dispatch_receipt(args.job_id)
            receipt["daemon"] = daemon_record(store)
            if (receipt["phase"] in {"started", "attention", "unknown"}
                    or receipt.get("waiting_for_previous_turn")
                    or not receipt["daemon"] or time.monotonic() >= deadline):
                return receipt
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    if command == "wait":
        deadline = time.monotonic() + max(0, args.timeout)
        while True:
            job = store.one("SELECT * FROM jobs WHERE id=?", (args.job_id,))
            if not job:
                raise ValueError("unknown job")
            if job["status"] in {"done", "unknown"} or time.monotonic() >= deadline:
                return job
            time.sleep(.2)
    if command == "pause":
        with store.transaction():
            if not store.one("SELECT id FROM chats WHERE id=?", (args.work_id,)):
                raise ValueError("unknown chat")
            hold = {"request": args.reason, "at": time.time(), "requires_human_continue": True}
            store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode(hold), args.work_id))
        return {"hold_saved": True, "work_id": args.work_id, "stopped": False}
    if command == "continue":
        text = args.text if args.text is not None else (sys.stdin.read() if not sys.stdin.isatty() else "")
        if not text.strip():
            text = "Continue the original authorized work after checking actual state."
        with store.transaction():
            chat = store.one("SELECT * FROM chats WHERE id=?", (args.work_id,))
            if not chat:
                raise ValueError("unknown chat")
            store.db.execute("UPDATE chats SET hold=NULL WHERE id=?", (chat["id"],))
            return store.enqueue("followup", chat["id"], {"text": text})[0]
    if command == "answer":
        response = json.loads(stdin_or(args.response))
        return queue_answer(store, args.question_id, response)
    if command == "source-add":
        config = {"cwd": store.main()["cwd"], "allow_reply": args.allow_reply}
        if args.github:
            kind = "github"
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.github) or (args.issue is not None and args.issue < 1):
                raise ValueError("GitHub requires owner/repo and a positive issue number")
            config.update(repo=args.github, issue=args.issue,
                          since=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        elif args.command_json:
            kind = "command"
            config["argv"] = json.loads(args.command_json)
            if not isinstance(config["argv"], list) or not config["argv"] or any(not isinstance(x, str) for x in config["argv"]):
                raise ValueError("command-json must be a nonempty argv array")
            if args.allow_reply:
                raise ValueError("command source does not implement external replies")
        else:
            kind, config["name"] = "push", args.push
            if args.allow_reply:
                raise ValueError("push source does not implement external replies")
        with store.transaction():
            existing = next((s for s in store.all("SELECT * FROM sources WHERE kind=?", (kind,))
                if {k: v for k, v in decode(s["config"]).items() if k not in {"since", "allow_reply"}}
                == {k: v for k, v in config.items() if k not in {"since", "allow_reply"}}), None)
            if existing:
                kept = decode(existing["config"])
                kept["allow_reply"] = args.allow_reply
                store.db.execute("UPDATE sources SET config=?,enabled=1,next_poll=0 WHERE id=?", (encode(kept), existing["id"]))
                return store.one("SELECT * FROM sources WHERE id=?", (existing["id"],))
            key = identifier()
            store.db.execute("INSERT INTO sources(id,kind,config) VALUES(?,?,?)", (key, kind, encode(config)))
            return store.one("SELECT * FROM sources WHERE id=?", (key,))
    if command == "poll":
        sources = store.all("SELECT * FROM sources WHERE enabled=1 AND kind!='push'" + (" AND id=?" if args.source_id else ""),
                            (args.source_id,) if args.source_id else ())
        return [{"source_id": source["id"], "new_jobs": commit_poll(store, source, *fetch_source(store, source))} for source in sources]
    if command == "ingest":
        source = store.one("SELECT * FROM sources WHERE id=? AND enabled=1", (args.source_id,))
        if not source:
            raise ValueError("unknown or disabled source")
        event = json.loads(sys.stdin.read())
        events = event if isinstance(event, list) else [event]
        for event in events:
            if not all(isinstance(event.get(k), str) and event[k] for k in ("key", "object_key", "text")):
                raise ValueError("event requires key, object_key, text")
        return {"new_jobs": commit_poll(store, source, events, decode(source["cursor"]))}
    if command == "source-disable":
        if not store.one("SELECT id FROM sources WHERE id=?", (args.source_id,)):
            raise ValueError("unknown source")
        store.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (args.source_id,))
        return {"source_id": args.source_id, "enabled": False}
    raise ValueError("unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    args.project = str(Path(args.project).expanduser().resolve(strict=True))
    directory = Path(args.state_dir or data_directory(args.project)).expanduser().resolve()
    store = None
    try:
        if not args.state_dir:
            directory = Bindings().state_directory(args.project) or directory
        if args.command == "context":
            result = read_session_context(directory)
        elif args.command == "patrol" and not read_session_context(directory)["active"]:
            result = {"active": False, "skipped": True, "reason": "current chat is not the active entry"}
        else:
            if args.command == "memory" and not (directory / "state.sqlite3").is_file():
                raise ValueError("manager is not initialized; memory read did not create state")
            if args.command in CONTROL_COMMANDS:
                require_active(read_session_context(directory), args.command)
            if args.command in {"heartbeat-plan", "heartbeat-bind"} and not read_session_context(directory)["active"]:
                raise ValueError("periodic patrol requires this manager's actual active entry chat")
            store = Store(directory)
            result = dispatch(args, store)
        print(encode(result))
        return 0
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(encode({"error": str(error)}), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
