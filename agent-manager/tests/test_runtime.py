"""Behavioral checks for scheduling transactions; no model/network calls."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.sources import commit_poll, github_poll, command_poll, send_outbox
from agent_manager_runtime.store import Store, data_directory, decode, encode, identifier
from agent_manager_runtime.cli import dispatch
from argparse import Namespace


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.main = self.store.setup(self.project)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def active_main(self, kind="task"):
        job = self.store.submit("Original human request", kind=kind)
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE chats SET thread_id='native-main',binding='linked' WHERE id=?", (self.main["id"],))
        self.store.db.execute("UPDATE runs SET state='running',turn_id='turn-main',thread_id='native-main' WHERE id=?", (run["id"],))
        return job, run

    def call(self, name, args, call_id="call-main"):
        return asyncio.run(Controller(self.store).business_tool({"threadId": "native-main",
            "turnId": "turn-main", "callId": call_id, "tool": name, "arguments": args}))

    def test_exactly_four_tables_and_single_active_run(self):
        self.assertEqual({r["name"] for r in self.store.all("SELECT name FROM sqlite_master WHERE type='table'")},
                         {"chats", "runs", "jobs", "sources"})
        first = self.store.submit("one")
        self.store.submit("two")
        self.assertEqual(self.store.claim(self.main["id"])["job_id"], first["id"])
        self.assertIsNone(self.store.claim(self.main["id"]))

    def test_new_project_uses_renamed_data_directory(self):
        with patch("agent_manager_runtime.store.Path.home", return_value=Path(self.temp.name)):
            directory = data_directory(self.project)
        self.assertEqual(directory.parent.name, "agent-manager")
        self.assertFalse(directory.exists())

    def test_renamed_entry_preserves_legacy_project_database_and_bindings(self):
        with patch("agent_manager_runtime.store.Path.home", return_value=Path(self.temp.name)):
            new_directory = data_directory(self.project)
            legacy_directory = new_directory.parent.parent / "super-agent" / new_directory.name
            old = Store(legacy_directory)
            try:
                main = old.setup(self.project)
                old.db.execute("UPDATE chats SET thread_id='existing-native-thread' WHERE id=?", (main["id"],))
            finally:
                old.close()
            restored = Store(data_directory(self.project))
            try:
                self.assertEqual(restored.directory, legacy_directory.resolve())
                self.assertEqual(restored.main()["id"], main["id"])
                self.assertEqual(restored.main()["thread_id"], "existing-native-thread")
                self.assertEqual(len(restored.all("SELECT * FROM chats")), 1)
                self.assertFalse(new_directory.exists())
            finally:
                restored.close()

    def started_worker(self):
        parent, main_run = self.active_main()
        work_id = self.call("sa_create_work", {"title": "background"})["work_id"]
        self.store.finish(main_run["id"], "completed")
        self.store.db.execute("UPDATE chats SET thread_id='native-worker',binding='linked' WHERE id=?", (work_id,))
        run = self.store.claim(work_id)
        self.store.db.execute("UPDATE runs SET turn_id='worker-active',thread_id='native-worker',state='running' WHERE id=?", (run["id"],))
        return parent, work_id, run

    def test_start_receipt_returns_without_waiting_for_execution(self):
        parent, work_id, run = self.started_worker()
        before = self.store.db.total_changes
        with patch("agent_manager_runtime.cli.daemon_record", return_value={"pid": 123, "started": "fixture"}), patch("agent_manager_runtime.cli.time.sleep", side_effect=AssertionError("must not wait for worker completion")):
            result = dispatch(Namespace(command="wait-start", job_id=parent["id"], timeout=30), self.store)
        self.assertEqual(result["phase"], "started")
        self.assertEqual((result["work_id"], result["thread_id"], result["turn_id"]), (work_id, "native-worker", "worker-active"))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "running")
        self.assertEqual(self.store.db.total_changes, before)

    def test_followup_receipt_does_not_reuse_previous_turn(self):
        _, work_id, run = self.started_worker()
        self.store.finish(run["id"], "completed")
        followup = self.store.submit("authorized correction", kind="followup", chat_id=work_id)
        receipt = self.store.dispatch_receipt(followup["id"])
        self.assertEqual(receipt["phase"], "dispatching")
        self.assertEqual(receipt["thread_id"], "native-worker")
        self.assertIsNone(receipt["turn_id"])
        self.assertEqual(len(self.store.all("SELECT * FROM chats WHERE role='worker'")), 1)

    def test_busy_worker_followup_returns_queued_without_waiting_for_old_turn(self):
        _, work_id, _ = self.started_worker()
        followup = self.store.submit("authorized correction", kind="followup", chat_id=work_id)
        with patch("agent_manager_runtime.cli.daemon_record", return_value={"pid": 123, "started": "fixture"}), patch("agent_manager_runtime.cli.time.sleep", side_effect=AssertionError("do not wait for the previous turn")):
            result = dispatch(Namespace(command="wait-start", job_id=followup["id"], timeout=30), self.store)
        self.assertEqual(result["phase"], "queued")
        self.assertTrue(result["waiting_for_previous_turn"])
        self.assertIsNone(result["turn_id"])
        self.assertEqual(result["active_turn_id"], "worker-active")

    def test_main_routing_to_busy_worker_confirms_existing_queued_delivery(self):
        _, work_id, _ = self.started_worker()
        parent, main_run = self.active_main()
        self.call("sa_attach_event", {"work_id": work_id}, "attach-existing")
        self.store.finish(main_run["id"], "completed")
        with patch("agent_manager_runtime.cli.daemon_record", return_value={"pid": 123, "started": "fixture"}), patch("agent_manager_runtime.cli.time.sleep", side_effect=AssertionError("routing must not wait for the busy worker")):
            result = dispatch(Namespace(command="wait-start", job_id=parent["id"], timeout=30), self.store)
        self.assertEqual(result["phase"], "queued")
        self.assertEqual((result["work_id"], result["thread_id"]), (work_id, "native-worker"))
        self.assertTrue(result["waiting_for_previous_turn"])
        self.assertIsNone(result["turn_id"])
        self.assertEqual(len(self.store.all("SELECT * FROM chats WHERE role='worker'")), 1)

    def test_start_confirmation_without_daemon_preserves_queued_state(self):
        job = self.store.submit("queued request")
        with patch("agent_manager_runtime.cli.daemon_record", return_value=None), patch("agent_manager_runtime.cli.time.sleep", side_effect=AssertionError("must return queued")):
            result = dispatch(Namespace(command="wait-start", job_id=job["id"], timeout=30), self.store)
        self.assertEqual(result["phase"], "queued")
        self.assertIsNone(result["thread_id"])
        self.assertEqual(len(self.store.all("SELECT * FROM runs")), 0)

    def test_start_receipt_reports_attention_or_unknown_without_recreation(self):
        parent, work_id, run = self.started_worker()
        self.store.db.execute("UPDATE runs SET state='unknown',error='lost observation' WHERE id=?", (run["id"],))
        self.assertEqual(self.store.dispatch_receipt(parent["id"])["phase"], "unknown")
        self.store.db.execute("UPDATE runs SET state='exited',outcome='failed',error='native failure' WHERE id=?", (run["id"],))
        failed = self.store.dispatch_receipt(parent["id"])
        self.assertEqual((failed["phase"], failed["error"]), ("attention", "native failure"))
        self.store.db.execute("UPDATE runs SET state='exited',outcome='completed' WHERE id=?", (run["id"],))
        self.store.db.execute("UPDATE chats SET report_state='blocked',report_ref=? WHERE id=?", (encode({"turn_id": "worker-active"}), work_id))
        self.assertEqual(self.store.dispatch_receipt(parent["id"])["phase"], "attention")
        self.assertEqual(len(self.store.all("SELECT * FROM chats WHERE role='worker'")), 1)

    def test_chat_cannot_create_work(self):
        self.active_main("chat")
        with self.assertRaisesRegex(ValueError, "ordinary conversation"):
            self.call("sa_create_work", {"title": "unexpected"})
        self.assertEqual(len(self.store.all("SELECT * FROM chats")), 1)

    def test_create_retries_use_both_call_and_causal_request_identity(self):
        job, _ = self.active_main()
        first = self.call("sa_create_work", {"title": "work"})
        self.assertEqual(first, self.call("sa_create_work", {"title": "work"}))
        self.assertEqual(first, self.call("sa_create_work", {"title": "work"}, "new-call-after-retry"))
        self.assertEqual(len(self.store.all("SELECT * FROM chats WHERE role='worker'")), 1)
        self.assertEqual(decode(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (job["id"],))["result_ref"]), first)
        with self.assertRaisesRegex(ValueError, "different arguments"):
            self.call("sa_create_work", {"title": "different"})

    def test_source_events_queued_before_first_assignment_reuse_worker(self):
        sid = identifier()
        self.store.db.execute("INSERT INTO sources(id,kind,config) VALUES(?,?,?)", (sid, "push", "{}"))
        source = self.store.one("SELECT * FROM sources WHERE id=?", (sid,))
        commit_poll(self.store, source, [
            {"key": "one", "object_key": "same-issue", "text": "original"},
            {"key": "two", "object_key": "same-issue", "text": "followup"}], None)
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE chats SET thread_id='native-main' WHERE id=?", (self.main["id"],))
        self.store.db.execute("UPDATE runs SET state='running',turn_id='turn-main' WHERE id=?", (run["id"],))
        first = self.call("sa_create_work", {"title": "first"})
        self.store.finish(run["id"], "completed")
        second_run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE runs SET state='running',turn_id='turn-main-2' WHERE id=?", (second_run["id"],))
        second = asyncio.run(Controller(self.store).business_tool({"threadId": "native-main", "turnId": "turn-main-2",
            "callId": "second-source", "tool": "sa_create_work", "arguments": {"title": "second"}}))
        self.assertEqual(first["work_id"], second["work_id"])
        self.assertEqual(len(self.store.all("SELECT * FROM chats WHERE role='worker'")), 1)
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='followup'")), 1)

    def test_failed_transaction_does_not_leave_work_or_receipt(self):
        self.active_main()
        with self.assertRaises(ValueError):
            self.call("sa_create_work", {"title": "bad", "cwd": self.temp.name})
        self.assertFalse(self.store.all("SELECT * FROM chats WHERE role='worker'"))
        self.assertFalse(self.store.all("SELECT * FROM jobs WHERE kind='effect'"))

    def test_hold_blocks_dispatch_but_not_status(self):
        self.store.submit("one")
        self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({"request": "wait for me"}), self.main["id"]))
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.assertTrue(self.store.status()["chats"][0]["hold"])

    def test_report_wakes_main_once_and_preserves_hold(self):
        _, main_run = self.active_main()
        work_id = self.call("sa_create_work", {"title": "work"})["work_id"]
        self.store.finish(main_run["id"], "completed")
        self.store.db.execute("UPDATE chats SET thread_id='native-worker',hold=? WHERE id=?", (encode({"request": "wait for me"}), work_id))
        self.store.db.execute("UPDATE chats SET hold=NULL WHERE id=?", (work_id,))
        run = self.store.claim(work_id)
        self.store.db.execute("UPDATE runs SET turn_id='turn-worker',state='running' WHERE id=?", (run["id"],))
        self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({"request": "wait for me"}), work_id))
        args = {"state": "ready", "summary": "result", "evidence": ["result.txt"], "question": None, "remaining": []}
        params = {"threadId": "native-worker", "turnId": "turn-worker", "callId": "report-1", "tool": "sa_report", "arguments": args}
        controller = Controller(self.store)
        asyncio.run(controller.business_tool(params))
        asyncio.run(controller.business_tool(params))
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='wake'")), 1)
        self.assertIsNotNone(self.store.one("SELECT hold FROM chats WHERE id=?", (work_id,))["hold"])
        self.assertEqual(self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))["state"], "running")

    def test_source_commit_and_cursor_are_atomic_and_retries_deduplicate(self):
        source_id = identifier()
        self.store.db.execute("INSERT INTO sources(id,kind,config) VALUES(?,?,?)", (source_id, "push", "{}"))
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        events = [{"key": "event-1", "object_key": "issue-1", "text": "original"}]
        self.assertEqual(commit_poll(self.store, source, events, {"page": 1}), 1)
        self.assertEqual(commit_poll(self.store, source, events, {"page": 2}), 0)
        current = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        self.assertEqual(decode(current["cursor"]), {"page": 1})
        self.assertEqual(commit_poll(self.store, current, events, {"page": 2}), 0)
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE source_id=?", (source_id,))), 1)

    def test_artifact_symlink_cannot_escape_work_directory(self):
        self.active_main()
        work_id = self.call("sa_create_work", {"title": "work"})["work_id"]
        secret = Path(self.temp.name) / "outside.txt"
        secret.write_text("outside")
        (self.project / "link.txt").symlink_to(secret)
        with self.assertRaisesRegex(ValueError, "outside"):
            asyncio.run(Controller(self.store).inspect({"work_id": work_id, "paths": ["link.txt"]}))

    def test_unknown_run_prevents_replacement(self):
        _, run = self.active_main()
        self.store.db.execute("UPDATE runs SET state='unknown' WHERE id=?", (run["id"],))
        self.store.submit("followup")
        self.assertIsNone(self.store.claim(self.main["id"]))

    def test_sibling_modules_in_same_checkout_do_not_write_concurrently(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "a").mkdir()
        (self.project / "b").mkdir()
        self.active_main()
        first = self.call("sa_create_work", {"title": "a", "cwd": str(self.project / "a")})["work_id"]
        second = identifier()
        self.store.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES(?,?,?,?,0)",
                              (second, "worker", "b", str(self.project / "b")))
        self.store.submit("second", chat_id=second)
        self.assertIsNotNone(self.store.claim(first))
        self.assertIsNone(self.store.claim(second))

    def test_lost_start_response_is_reconciled_from_native_input_marker(self):
        job, run = self.active_main()
        self.store.db.execute("UPDATE runs SET turn_id=NULL,server_pid=99999999,server_start='gone' WHERE id=?", (run["id"],))
        self.store.db.execute("UPDATE chats SET thread_id='native-main' WHERE id=?", (self.main["id"],))
        from agent_manager_runtime.controller import INPUT_PREFIX
        turn = {"id": "accepted-before-crash", "status": "completed", "items": [{"type": "userMessage", "content": [
            {"type": "text", "text": INPUT_PREFIX + encode({"run_id": run["id"], "job_id": job["id"], "payload": {"text": "Original human request"}})}]}]}
        class FakeClient:
            async def history(self, thread):
                return [turn]
        controller = Controller(self.store)
        async def bind(chat):
            return FakeClient()
        controller.bind = bind
        asyncio.run(controller.recover())
        result = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual((result["state"], result["outcome"], result["turn_id"]), ("exited", "completed", "accepted-before-crash"))
        self.assertFalse(self.store.all("SELECT * FROM jobs WHERE kind='recover'"))

    def test_unconfirmed_native_acceptance_never_retries_blindly(self):
        _, run = self.active_main()
        self.store.db.execute("UPDATE runs SET turn_id=NULL,server_pid=99999999,server_start='gone' WHERE id=?", (run["id"],))
        class FakeClient:
            async def history(self, thread):
                return []
        controller = Controller(self.store)
        async def bind(chat):
            return FakeClient()
        controller.bind = bind
        asyncio.run(controller.recover())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "unknown")
        self.assertFalse(self.store.all("SELECT * FROM jobs WHERE kind='recover'"))

    def test_terminal_observation_and_missing_report_wakeup_commit_together(self):
        _, main_run = self.active_main()
        work_id = self.call("sa_create_work", {"title": "work"})["work_id"]
        self.store.finish(main_run["id"], "completed")
        run = self.store.claim(work_id)
        controller = Controller(self.store)
        turn = {"id": "worker-final", "status": "failed", "error": {"message": "fixture"}}
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("fixture commit interruption")):
            with self.assertRaises(RuntimeError):
                asyncio.run(controller.completed(run, turn))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "prepared")
        asyncio.run(controller.completed(run, turn))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "exited")
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='wake'")), 1)


class GithubTests(unittest.TestCase):
    def test_command_source_passes_cursor_on_stdin_without_shell(self):
        script = "import sys,json; v=json.load(sys.stdin); print(json.dumps({'events':[{'key':'1','object_key':'o','text':'task'}], 'cursor':v['cursor']+1}))"
        events, cursor = command_poll({"argv": [sys.executable, "-c", script], "cwd": tempfile.gettempdir()}, 4)
        self.assertEqual(cursor, 5)
        self.assertEqual(events[0]["text"], "task")

    def test_unknown_outbox_does_not_repeat_a_post(self):
        class FakeStore:
            def one(self, *args):
                return {"kind": "github", "config": encode({"allow_reply": True, "repo": "owner/repo"})}
        job = {"id": "out-1", "source_id": "source", "status": "unknown", "payload": encode({"issue": 2, "text": "reply"})}
        with patch("agent_manager_runtime.sources.gh_api", return_value=[[]]) as api:
            with self.assertRaisesRegex(RuntimeError, "not resending"):
                send_outbox(FakeStore(), job)
            self.assertEqual(api.call_count, 1)
            self.assertNotIn("body", api.call_args.kwargs)

    def test_unknown_outbox_recovers_actual_remote_receipt(self):
        class FakeStore:
            def one(self, *args):
                return {"kind": "github", "config": encode({"allow_reply": True, "repo": "owner/repo"})}
        job = {"id": "out-1", "source_id": "source", "status": "unknown", "payload": encode({"issue": 2, "text": "reply"})}
        comment = {"id": 29, "html_url": "receipt", "body": "reply\n\n<!-- super-agent:out-1 -->"}
        with patch("agent_manager_runtime.sources.gh_api", return_value=[[comment]]) as api:
            self.assertEqual(send_outbox(FakeStore(), job), {"remote_id": 29, "url": "receipt"})
            self.assertEqual(api.call_count, 1)

    def test_initial_context_edit_revert_and_own_output_filter(self):
        issue = {"id": 11, "number": 2, "title": "title", "body": "A", "url": "api-issue",
                 "html_url": "issue-url", "user": {"login": "same-person"}}
        comments = [{"id": 7, "body": "human", "issue_url": "api-issue", "html_url": "comment-url", "user": {"login": "same-person"}}]
        def api(path, **kwargs):
            return [comments] if "/comments" in path else issue
        with patch("agent_manager_runtime.sources.gh_api", side_effect=api):
            config = {"repo": "owner/repo", "issue": 2}
            first, cursor = github_poll(config, None)
            self.assertEqual(len(first), 1)
            self.assertEqual(len(first[0]["context"]["existing_comments"]), 1)
            issue["body"] = "B"
            second, cursor = github_poll(config, cursor)
            issue["body"] = "A"
            third, cursor = github_poll(config, cursor)
            self.assertNotEqual(first[0]["key"], third[0]["key"])
            comments.append({**comments[0], "id": 8, "body": "human again"})
            fourth, cursor = github_poll(config, cursor)
            self.assertEqual(len(fourth), 1)
            comments.append({**comments[0], "id": 9, "body": "own output"})
            fifth, _ = github_poll(config, cursor, own_ids=[9])
            self.assertEqual(fifth, [])


if __name__ == "__main__":
    unittest.main()
