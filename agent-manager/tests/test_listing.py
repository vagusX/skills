"""Manager-scoped listing via SQLite, CLI and native dynamic tool dispatch."""

import asyncio
from argparse import Namespace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.cli import dispatch
from agent_manager_runtime.codex import process_start
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.listing import list_work
from agent_manager_runtime.store import Store, encode, identifier


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.main = self.store.setup(self.project)
        self.pid, self.started = os.getpid(), process_start(os.getpid())
        (self.store.directory / "daemon.json").write_text(encode({"pid": self.pid, "started": self.started}))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def worker(self, title="fixture worker", linked=True):
        key = identifier()
        self.store.db.execute("""INSERT INTO chats(id,role,title,cwd,thread_id,binding,created_at)
            VALUES(?,'worker',?,?,?,?,1)""", (key, title, str(self.project),
                "fixture-" + key if linked else None, "linked" if linked else "unbound"))
        self.store.enqueue("task", key, {"text": "original request"})
        return key

    def running(self, work_id):
        run = self.store.claim(work_id)
        thread = self.store.one("SELECT thread_id FROM chats WHERE id=?", (work_id,))["thread_id"]
        self.store.db.execute("""UPDATE runs SET state='running',thread_id=?,turn_id=?,server_pid=?,server_start=?
            WHERE id=?""", (thread, "turn-" + work_id, self.pid, self.started, run["id"]))
        return self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))

    def question(self, run, status="pending", answered=False, valid=True):
        job, _ = self.store.enqueue("question", run["chat_id"], {
            "method": "item/tool/requestUserInput", "server_pid": self.pid, "server_start": self.started,
            "params": {"threadId": run["thread_id"], "turnId": run["turn_id"] if valid else "old-turn",
                       "questions": [{"id": "secret", "question": "private question", "isSecret": True}]},
        }, parent_id=run["job_id"], status=status,
            result={"answers": {"secret": {"answers": ["private answer"]}}} if answered else None)
        return job["id"]

    def row(self, work_id):
        return next(row for row in list_work(self.store)["works"] if row["work_id"] == work_id)

    def active_main(self):
        self.store.submit("show managed work", kind="chat")
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE chats SET thread_id='fixture-main',binding='linked' WHERE id=?", (self.main["id"],))
        self.store.db.execute("UPDATE runs SET state='running',turn_id='fixture-main-turn' WHERE id=?", (run["id"],))

    def call(self, tool, args=None, thread="fixture-main", turn="fixture-main-turn"):
        return asyncio.run(Controller(self.store).business_tool({"threadId": thread, "turnId": turn,
            "callId": "fixture-list-call", "tool": tool, "arguments": args or {}}))

    def test_empty_and_other_manager_are_not_global_codex_sessions(self):
        other = Store(Path(self.temp.name) / "other-state")
        try:
            other.setup(self.project)
            other.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES('other','worker','other',?,1)", (str(self.project),))
            with patch("agent_manager_runtime.controller.Codex", side_effect=AssertionError("no RPC")):
                self.assertEqual(list_work(self.store)["works"], [])
                mine = self.worker(linked=False)
                result = list_work(self.store)
            self.assertEqual([row["work_id"] for row in result["works"]], [mine])
            self.assertIsNone(result["works"][0]["thread_id"])
            self.assertEqual(result["works"][0]["execution"]["state"], "queued")
            self.assertEqual(result["manager_id"], self.main["id"])
        finally:
            other.close()

    def test_running_followup_and_old_report_are_separate(self):
        work = self.worker()
        run = self.running(work)
        followup, _ = self.store.enqueue("followup", work, {"text": "authorized correction"})
        self.store.db.execute("UPDATE chats SET report_state='ready',report_ref=? WHERE id=?",
            (encode({"thread_id": run["thread_id"], "turn_id": "older-turn", "call_id": "report"}), work))
        row = self.row(work)
        self.assertEqual(row["execution"]["state"], "running")
        self.assertEqual(row["execution"]["turn_id"], run["turn_id"])
        self.assertEqual(row["queued_job_ids"], [followup["id"]])
        self.assertEqual(row["report_state"], "ready")
        self.assertFalse(row["report_current"])
        self.assertNotIn("completed", row)

    def test_only_live_unanswered_native_requests_count_and_secrets_are_not_copied(self):
        work = self.worker()
        run = self.running(work)
        pending = self.question(run)
        queued = self.question(run, answered=True)
        sent = self.question(run, status="inflight", answered=True)
        unknown = self.question(run, status="unknown", answered=True)
        stale = self.question(run, valid=False)
        self.question(run, status="done")
        row = self.row(work)
        self.assertEqual(row["pending_question_count"], 1)
        self.assertEqual(row["execution"]["state"], "awaiting_answer")
        questions = {q["question_id"]: q for q in row["questions"]}
        self.assertTrue(questions[pending]["needs_answer"])
        for key, phase in [(queued, "answer_queued"), (sent, "sent"), (unknown, "unknown")]:
            self.assertEqual(questions[key]["phase"], phase)
            self.assertFalse(questions[key]["needs_answer"])
        self.assertFalse(questions[stale]["live"])
        self.assertNotIn("private question", encode(row))
        self.assertNotIn("private answer", encode(row))
        self.store.db.execute("UPDATE runs SET server_start='different process' WHERE id=?", (run["id"],))
        self.assertEqual(self.row(work)["pending_question_count"], 0)
        self.assertEqual(self.row(work)["execution"]["state"], "unknown")

    def test_hold_does_not_claim_running_worker_stopped_and_query_does_not_release_it(self):
        work = self.worker()
        run = self.running(work)
        self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({"request": "human pause"}), work))
        row = self.row(work)
        self.assertEqual(row["execution"]["state"], "running")
        self.assertEqual(row["hold"]["request"], "human pause")
        self.store.finish(run["id"], "interrupted")
        self.assertEqual(self.row(work)["execution"]["state"], "paused")

    def test_terminal_outcome_not_report_or_acceptance_and_current_report_has_a_turn(self):
        for outcome, state in [("completed", "idle"), ("failed", "failed"), ("interrupted", "interrupted"), ("unknown", "unknown")]:
            with self.subTest(outcome=outcome):
                work = self.worker(outcome)
                run = self.running(work)
                self.store.finish(run["id"], outcome)
                self.store.db.execute("UPDATE chats SET report_state='blocked',report_ref=? WHERE id=?",
                    (encode({"thread_id": run["thread_id"], "turn_id": run["turn_id"]}), work))
                row = self.row(work)
                self.assertEqual(row["execution"]["state"], state)
                self.assertEqual(row["report_state"], "blocked")
                self.assertTrue(row["report_current"])
                self.assertEqual(row["pending_question_count"], 0)

    def test_prepared_unknown_binding_missing_process_and_closed_are_explicit(self):
        work = self.worker()
        run = self.store.claim(work)
        self.assertEqual(self.row(work)["execution"]["state"], "starting")
        self.store.db.execute("UPDATE chats SET binding='unknown' WHERE id=?", (work,))
        self.assertEqual(self.row(work)["execution"]["state"], "unknown")
        self.store.db.execute("UPDATE chats SET binding='linked' WHERE id=?", (work,))
        self.store.db.execute("UPDATE runs SET state='running' WHERE id=?", (run["id"],))
        self.assertEqual(self.row(work)["execution"]["state"], "unknown")
        self.store.finish(run["id"], "completed")
        self.store.db.execute("UPDATE chats SET closed=1 WHERE id=?", (work,))
        self.assertEqual(self.row(work)["execution"]["state"], "closed")

    def test_cli_new_tool_and_legacy_tool_share_read_only_snapshot_even_in_chat(self):
        self.active_main()
        work = self.worker()
        self.running(work)
        before = "\n".join(self.store.db.iterdump())
        changes = self.store.db.total_changes
        # SQLite itself rejects any writes during listing, not just expected calls.
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE}
        self.store.db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        try:
            with patch.object(Controller, "client", side_effect=AssertionError("list cannot load a native thread")):
                results = [dispatch(Namespace(command="list"), self.store),
                           self.call("sa_list_work"),
                           self.call("sa_inspect_work", {"work_id": self.main["id"]})]
            for result in results:
                result.pop("snapshot_at")
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[1], results[2])
            self.assertEqual(self.store.db.total_changes, changes)
            self.assertEqual("\n".join(self.store.db.iterdump()), before)
        finally:
            self.store.db.set_authorizer(None)

    def test_worker_cannot_list_or_inspect_manager_and_unknown_id_does_not_expand_scope(self):
        work = self.worker()
        run = self.running(work)
        for name, args in [("sa_list_work", {}), ("sa_inspect_work", {"work_id": self.main["id"]})]:
            with self.assertRaisesRegex(ValueError, "actual thread role"):
                self.call(name, args, run["thread_id"], run["turn_id"])
        self.active_main()
        with self.assertRaisesRegex(ValueError, "unknown worker"):
            self.call("sa_inspect_work", {"work_id": "another-manager"})

    def test_native_tool_response_and_same_thread_resume_with_legacy_instructions(self):
        self.active_main()
        work = self.worker(linked=False)
        controller = Controller(self.store)

        class Client:
            def __init__(client):
                client.calls, client.responses = [], []

            async def call(client, method, params):
                client.calls.append((method, params))
                return {"thread": {"id": params.get("threadId", "fixture-created"), "cwd": params["cwd"]}}

            async def respond(client, request_id, result):
                client.responses.append((request_id, result))

        client = Client()

        async def scenario():
            async def get_client(role):
                return client
            controller.client = get_client
            await controller.bind(self.store.main())
            await controller.bind(self.store.one("SELECT * FROM chats WHERE id=?", (work,)))
            await controller.on_request(client, {"id": "rpc-list", "method": "item/tool/call", "params": {
                "threadId": "fixture-main", "turnId": "fixture-main-turn", "callId": "call-list",
                "tool": "sa_inspect_work", "arguments": {"work_id": self.main["id"]}}})

        asyncio.run(scenario())
        self.assertEqual(client.calls[0][0], "thread/resume")
        self.assertEqual(client.calls[0][1]["threadId"], "fixture-main")
        self.assertIn("envelope.chat_id", client.calls[0][1]["developerInstructions"])
        self.assertNotIn("dynamicTools", client.calls[0][1])
        response = client.responses[0][1]
        self.assertTrue(response["success"])
        self.assertEqual(json.loads(response["contentItems"][0]["text"])["works"][0]["work_id"], work)

    def test_new_main_exposes_list_tool_without_enabling_worker_management_tools(self):
        controller = Controller(self.store)
        calls = []

        class Client:
            async def call(client, method, params):
                calls.append((method, params))
                return {"thread": {"id": "fixture-new-main", "cwd": params["cwd"]}}

        async def get_client(role):
            return Client()

        controller.client = get_client
        asyncio.run(controller.bind(self.store.main()))
        self.assertEqual(calls[0][0], "thread/start")
        names = {tool["name"] for tool in calls[0][1]["dynamicTools"]}
        self.assertIn("sa_list_work", names)
        self.assertNotIn("sa_report", names)

    def test_reopening_state_lists_original_work_and_queued_followup_without_new_rows(self):
        work = self.worker()
        run = self.running(work)
        followup, _ = self.store.enqueue("followup", work, {"text": "authorized correction"})
        before = "\n".join(self.store.db.iterdump())
        reopened = Store(self.store.directory)
        try:
            row = list_work(reopened)["works"][0]
            self.assertEqual((row["work_id"], row["thread_id"], row["execution"]["turn_id"]),
                             (work, run["thread_id"], run["turn_id"]))
            self.assertEqual(row["queued_job_ids"], [followup["id"]])
            self.assertEqual("\n".join(reopened.db.iterdump()), before)
        finally:
            reopened.close()

    def test_real_cli_returns_one_json_snapshot_and_does_not_need_codex_on_path(self):
        work = self.worker(linked=False)
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
            "--state-dir", str(self.store.directory), "list"], capture_output=True, text=True,
            env={**os.environ, "PATH": ""}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual([row["work_id"] for row in snapshot["works"]], [work])
        self.assertEqual(snapshot["works"][0]["execution"]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
