"""Maintenance, memory and business delivery remain separate across recovery."""

import asyncio
from argparse import Namespace
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.cli import dispatch, parser
from agent_manager_runtime.compaction import context_needs_compaction, queue_compaction
from agent_manager_runtime.controller import Controller, INPUT_PREFIX
from agent_manager_runtime.codex import Codex
from agent_manager_runtime.memory import Memory
from agent_manager_runtime.store import Store, decode

ENTRY = "01a1befa-a180-7a92-b798-fa4ee66a562f"


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        self.main = self.store.setup(self.project)
        self.store.db.execute("UPDATE chats SET thread_id='native-main',binding='linked',entry_thread_id=? WHERE role='main'", (ENTRY,))
        self.memory = Memory(self.store, Path(self.temp.name) / "global")
        mem = patch("agent_manager_runtime.controller.Memory", lambda store: self.memory)
        mem.start()
        self.addCleanup(mem.stop)
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": ENTRY})
        env.start()
        self.addCleanup(env.stop)
        self.controller = Controller(self.store)
        self.calls = []
        outer = self

        class Client:
            process = SimpleNamespace(pid=999999)
            started = "fixture start"

            async def call(self, method, params, **options):
                outer.calls.append((method, params))
                if method == "thread/resume":
                    return {"thread": {"id": params["threadId"], "cwd": params["cwd"]}}
                if method == "turn/start":
                    return {"turn": {"id": "business-" + str(len(outer.calls))}}
                if method in {"thread/name/set", "thread/compact/start"}:
                    return {}
                raise AssertionError(method)

            async def history(self, thread):
                return []

        self.client = Client()

        async def get_client(role):
            return self.client

        self.controller.client = get_client

    def call(self, coroutine):
        return asyncio.run(coroutine)

    def compact_run(self):
        result = dispatch(Namespace(command="compact"), self.store)
        run = self.store.claim(self.main["id"])
        self.assertEqual(result["job_id"], run["job_id"])
        return run

    def finish_native_compact(self, seen=True):
        self.call(self.controller.on_event(self.client, {"method": "turn/started", "params": {
            "threadId": "native-main", "turn": {"id": "compact-turn"}}}))
        if seen:
            self.call(self.controller.on_event(self.client, {"method": "item/completed", "params": {
                "threadId": "native-main", "turnId": "compact-turn", "item": {"id": "c", "type": "contextCompaction"}}}))
        self.call(self.controller.on_event(self.client, {"method": "turn/completed", "params": {
            "threadId": "native-main", "turn": {"id": "compact-turn", "status": "completed"}}}))

    def test_native_acceptance_is_not_completion_and_business_waits_for_maintenance(self):
        run = self.compact_run()
        self.store.submit("new original request", kind="chat")
        self.call(self.controller.execute(run))
        actual = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(actual["state"], "prepared")
        self.assertIsNone(actual["turn_id"])
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.assertFalse(any(method == "turn/start" for method, _ in self.calls))
        self.finish_native_compact()
        actual = self.store.one("SELECT * FROM jobs WHERE id=?", (run["job_id"],))
        self.assertTrue(decode(actual["result_ref"])["compacted"])
        self.assertEqual(actual["status"], "done")
        self.assertIsNotNone(self.store.claim(self.main["id"]))
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind IN ('wake','notification')"))
        self.assertIsNone(self.store.main()["report_state"])

    def test_missing_completion_evidence_preserves_unknown_without_replay(self):
        run = self.compact_run()
        self.call(self.controller.execute(run))
        self.finish_native_compact(seen=False)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "unknown")
        self.assertIsNone(self.store.claim(self.main["id"]))
        again = dispatch(Namespace(command="compact"), self.store)
        self.assertEqual(again["job_id"], run["job_id"])
        self.assertFalse(again["queued"])
        self.assertEqual(len([m for m, _ in self.calls if m == "thread/compact/start"]), 1)

    def test_lost_compact_response_is_not_matched_to_an_unrelated_turn_on_restart(self):
        run = self.compact_run()
        self.store.db.execute("UPDATE runs SET server_pid=123,server_start='old' WHERE id=?", (run["id"],))
        with patch("agent_manager_runtime.controller.process_alive", return_value=False):
            self.call(self.controller.recover())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "unknown")
        self.assertFalse(any(m in {"thread/compact/start", "turn/start"} for m, _ in self.calls))
        self.assertEqual(len(self.store.all("SELECT id FROM jobs")), 1)

    def test_only_current_context_occupancy_schedules_once_after_main_turn(self):
        job = self.store.submit("plain chat", kind="chat")
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE runs SET thread_id='native-main',turn_id='business-turn',state='running' WHERE id=?", (run["id"],))
        usage = {"threadId": "native-main", "turnId": "business-turn", "tokenUsage": {
            "modelContextWindow": 100, "last": {"inputTokens": 70, "outputTokens": 10},
            "total": {"totalTokens": 999999}}}
        self.call(self.controller.on_event(self.client, {"method": "thread/tokenUsage/updated", "params": usage}))
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='compact'"))
        self.call(self.controller.completed(run, {"id": "business-turn", "status": "completed"}))
        self.call(self.controller.on_event(self.client, {"method": "thread/tokenUsage/updated", "params": usage}))
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='compact'")), 1)
        usage["tokenUsage"]["last"] = {"inputTokens": 5, "outputTokens": 5}
        self.assertFalse(context_needs_compaction(usage, "business-turn"))
        self.assertFalse(context_needs_compaction(usage, "other-turn"))
        usage["tokenUsage"]["modelContextWindow"] = None
        self.assertFalse(context_needs_compaction(usage, "business-turn"))

    def test_inactive_hold_and_pending_approval_do_not_compact_or_answer(self):
        for condition in ("inactive", "hold", "question"):
            self.store.db.execute("UPDATE chats SET entry_thread_id=?,hold=NULL WHERE role='main'", (ENTRY,))
            self.store.db.execute("DELETE FROM jobs")
            if condition == "inactive":
                self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
            elif condition == "hold":
                self.store.db.execute("UPDATE chats SET hold='human pause' WHERE role='main'")
            else:
                self.store.enqueue("question", self.main["id"], {"request": "pending approval"})
            with self.store.transaction():
                result = queue_compaction(self.store)
            self.assertFalse(result["queued"])
            self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='compact'"))
        q = self.store.one("SELECT * FROM jobs WHERE kind='question'")
        self.assertEqual(q["status"], "pending")
        self.assertIsNone(q["result_ref"])

    def test_memory_hydrates_only_on_change_or_after_native_compaction(self):
        self.memory.put("test-method", "Use the verified project test command", "human statement in this test")

        def business():
            self.store.submit("original request; do not publish", kind="chat")
            run = self.store.claim(self.main["id"])
            self.call(self.controller.execute(run))
            params = next(p for m, p in reversed(self.calls) if m == "turn/start")
            self.store.finish(run["id"], "completed")
            return decode(params["input"][0]["text"][len(INPUT_PREFIX):])

        first, second = business(), business()
        self.assertIn("project_context", first)
        self.assertNotIn("project_context", second)
        self.assertEqual(first["payload"], second["payload"])
        self.assertNotIn("project_context", first["payload"])
        run = self.compact_run()
        self.call(self.controller.execute(run))
        self.finish_native_compact()
        third = business()
        self.assertIn("project_context", third)
        self.assertEqual(first["project_context"]["revision"], third["project_context"]["revision"])
        self.assertEqual(third["project_context"]["project"], str(self.project.resolve()))

    def test_memory_write_role_scope_and_plain_chat_boundaries(self):
        self.store.submit("record verified project knowledge", kind="task")
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE runs SET turn_id='main-turn',state='running' WHERE id=?", (run["id"],))
        params = {"threadId": "native-main", "turnId": "main-turn", "callId": "memory-call", "tool": "sa_memory",
                  "arguments": {"action": "put", "key": "verified", "text": "Project fact", "source": "actual check"}}
        receipt = self.call(self.controller.business_tool(params))
        self.assertEqual(self.call(self.controller.business_tool(params)), receipt)
        self.assertEqual(len(self.memory.read()["entries"]), 1)
        params["callId"] = "global-call"
        params["arguments"]["scope"] = "global"
        with self.assertRaises(ValueError):
            self.call(self.controller.business_tool(params))
        self.assertTrue(self.memory.read("global")["missing"])
        self.store.finish(run["id"], "completed")
        self.store.submit("ordinary chat", kind="chat")
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE runs SET turn_id='chat-turn',state='running' WHERE id=?", (run["id"],))
        params.update(turnId="chat-turn", callId="chat-memory")
        params["arguments"].pop("scope")
        with self.assertRaisesRegex(ValueError, "ordinary conversation"):
            self.call(self.controller.business_tool(params))

    def test_recent_inspection_does_not_fetch_old_full_history(self):
        client = Codex("history", self.store.directory, str(self.project), None, None)
        requests = []

        async def rpc(method, params):
            requests.append((method, params))
            return {"data": [{"id": "recent-3"}, {"id": "recent-2"}, {"id": "recent-1"}],
                    "nextCursor": "much-older-history"}

        client.call = rpc
        turns = self.call(client.history("native-worker", limit=3))
        self.assertEqual([t["id"] for t in turns], ["recent-1", "recent-2", "recent-3"])
        self.assertEqual(len(requests), 1)

    def test_native_auto_compaction_invalidates_memory_without_finishing_a_business_run(self):
        self.memory.put("project-fact", "Verified project fact", "this test")
        self.store.submit("ordinary question", kind="chat")
        run = self.store.claim(self.main["id"])
        native = self.client.call

        async def compact_before_reply(method, params, **options):
            if method == "turn/start":
                await self.controller.on_event(self.client, {"method": "item/completed", "params": {
                    "threadId": "native-main", "turnId": "auto-compact-in-business",
                    "item": {"id": "auto-c", "type": "contextCompaction"}}})
            return await native(method, params, **options)

        self.client.call = compact_before_reply
        self.call(self.controller.execute(run))
        self.assertNotIn(self.main["id"], self.controller.memory_loaded)
        self.assertEqual(self.controller.compaction_seen, set())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "running")
        self.assertEqual(self.store.one("SELECT status FROM jobs WHERE id=?", (run["job_id"],))["status"], "inflight")

    def test_reading_memory_of_an_uninitialized_project_does_not_create_state(self):
        directory = Path(self.temp.name) / "not-initialized"
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
            "--state-dir", str(directory), "memory"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not create state", result.stderr)
        self.assertFalse(directory.exists())
