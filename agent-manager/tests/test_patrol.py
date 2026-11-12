"""Progress continuation and periodic checks do not grant new work or approval."""

import asyncio
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.cli import dispatch
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.notifications import bind_entry
from agent_manager_runtime.patrol import bind_heartbeat, heartbeat_plan, patrol
from agent_manager_runtime.store import Store, decode, encode

ENTRY = "01a1befa-15e7-7fa2-96de-7266ea1f6303"
OTHER = "01a1befa-a180-7a92-b798-fa4ee66a562f"


class PatrolTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": ENTRY})
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(lambda: self.store.close())
        self.main = self.store.setup(self.project)
        bind_entry(self.store, ENTRY)
        self.store.db.execute("UPDATE chats SET thread_id='native-main',binding='linked' WHERE role='main'")
        self.store.db.execute("""INSERT INTO chats(id,role,title,cwd,thread_id,binding,created_at)
            VALUES('work','worker','original work',?,'native-worker','linked',1)""", (str(self.project),))
        self.job, _ = self.store.enqueue("task", "work", {"text": "Original request. Verify acceptance. Do not publish."})
        self.run = self.store.claim("work")
        self.store.db.execute("UPDATE runs SET state='running',thread_id='native-worker',turn_id='worker-turn' WHERE id=?", (self.run["id"],))
        self.controller = Controller(self.store)

    def report(self, question=None, state="progress"):
        return asyncio.run(self.controller.business_tool({"threadId": "native-worker", "turnId": "worker-turn",
            "callId": "report-call", "tool": "sa_report", "arguments": {
                "state": state, "summary": "Implementation exists; acceptance pending", "evidence": [],
                "remaining": ["Run the original acceptance checks"], "question": question}}))

    def complete(self):
        asyncio.run(self.controller.completed(self.run, {"id": "worker-turn", "status": "completed"}))

    def followup(self, call="continue-call", work="work"):
        return asyncio.run(self.controller.business_tool({"threadId": "native-main", "turnId": "main-turn",
            "callId": call, "tool": "sa_followup", "arguments": {"work_id": work,
                "text": "Continue the original acceptance checks within the original authorization."}}))

    def start_review(self):
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE runs SET state='running',thread_id='native-main',turn_id='main-turn' WHERE id=?", (run["id"],))
        return run

    def test_progress_turn_end_queues_one_review_and_continues_the_same_worker(self):
        self.report()
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))
        self.complete()
        self.complete()  # duplicate terminal notification
        patrol(self.store)
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='wake'")), 1)
        review = self.start_review()
        result = self.followup()
        self.assertEqual(self.followup(), result)  # same native call receipt
        with self.assertRaisesRegex(ValueError, "already continued"):
            self.followup(call="another-call")
        asyncio.run(self.controller.completed(review, {"id": "main-turn", "status": "completed"}))
        next_run = self.store.claim("work")
        self.assertEqual(next_run["thread_id"], "native-worker")
        self.assertEqual(next_run["job_id"], result["job_id"])
        self.assertEqual(len(self.store.all("SELECT id FROM chats WHERE role='worker'")), 1)
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='notification'"))
        ref = decode(self.store.one("SELECT native_ref FROM jobs WHERE id=?", (self.job["id"],))["native_ref"])
        self.assertEqual(ref, {"thread_id": "native-worker", "turn_id": "worker-turn"})

    def test_question_bearing_progress_wakes_once_without_automatic_answer(self):
        self.report(question="Which delivery target should be used?")
        self.complete()
        patrol(self.store)
        wakes = self.store.all("SELECT * FROM jobs WHERE kind='wake'")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(decode(wakes[0]["payload"])["question"], "Which delivery target should be used?")
        self.assertIsNone(self.store.claim("work"))
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='followup'"))

    def test_hold_and_unresolved_native_request_prevent_progress_continuation(self):
        self.report()
        self.store.enqueue("question", "work", {"method": "item/commandExecution/requestApproval"}, parent_id=self.job["id"])
        self.store.finish(self.run["id"], "completed")
        patrol(self.store)
        question = self.store.one("SELECT * FROM jobs WHERE kind='question'")
        self.assertEqual(question["status"], "pending")
        self.assertIsNone(question["result_ref"])
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))
        self.store.db.execute("UPDATE jobs SET status='done' WHERE id=?", (question["id"],))
        self.store.db.execute("UPDATE chats SET hold=? WHERE id='work'", (encode({"requires_human_continue": True}),))
        patrol(self.store)
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))
        self.assertIsNotNone(self.store.one("SELECT hold FROM chats WHERE id='work'")["hold"])

    def test_patrol_repairs_old_idle_progress_and_deduplicates_after_reopening(self):
        self.report()
        self.store.finish(self.run["id"], "completed")
        first = patrol(self.store)
        self.assertTrue(first["changed"])
        self.assertEqual(len(first["queued_review_job_ids"]), 1)
        self.store.close()
        self.store = Store(Path(self.temp.name) / "state")
        second = patrol(self.store)
        self.assertFalse(second["changed"])
        self.assertEqual(second["queued_review_job_ids"], [])
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='wake'")), 1)

    def test_unknown_or_running_execution_is_only_observed(self):
        self.report()
        for state in ("running", "unknown"):
            self.store.db.execute("UPDATE runs SET state=? WHERE id=?", (state, self.run["id"]))
            with patch("agent_manager_runtime.cli.subprocess.Popen", side_effect=AssertionError("must not restart")):
                self.assertEqual(patrol(self.store)["queued_review_job_ids"], [])
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind IN ('wake','followup')"))

    def test_already_queued_followup_prevents_another_review(self):
        self.report()
        queued, _ = self.store.enqueue("followup", "work", {"text": "original authorized correction"})
        self.complete()
        self.assertEqual(patrol(self.store)["queued_review_job_ids"], [])
        self.assertEqual(self.store.claim("work")["job_id"], queued["id"])

    def test_stale_review_sends_no_native_input_after_new_input_or_deactivation(self):
        self.report()
        self.complete()
        review = self.store.claim(self.main["id"])
        self.store.enqueue("followup", "work", {"text": "new human input"})
        with patch.object(self.controller, "bind", side_effect=AssertionError("must not resume or send")):
            asyncio.run(self.controller.execute(review))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (review["id"],))["state"], "exited")
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='notification'"))

    def test_recovered_review_cannot_create_a_replacement_worker(self):
        self.report()
        self.complete()
        review = self.store.one("SELECT * FROM jobs WHERE kind='wake'")
        payload = {**decode(review["payload"]), "original_kind": "wake"}
        self.store.db.execute("UPDATE jobs SET kind='recover',payload=? WHERE id=?", (encode(payload), review["id"]))
        self.start_review()
        with self.assertRaisesRegex(ValueError, "does not authorize"):
            asyncio.run(self.controller.business_tool({"threadId": "native-main", "turnId": "main-turn",
                "callId": "incorrect-create", "tool": "sa_create_work", "arguments": {"title": "replacement"}}))
        self.assertEqual(len(self.store.all("SELECT id FROM chats WHERE role='worker'")), 1)

    def test_old_review_is_not_reused_after_a_new_worker_turn(self):
        self.report()
        self.complete()
        review = self.store.claim(self.main["id"])
        self.store.enqueue("followup", "work", {"text": "new human input"})
        later = self.store.claim("work")
        self.store.db.execute("UPDATE runs SET turn_id='later-turn' WHERE id=?", (later["id"],))
        self.store.finish(later["id"], "completed")
        with patch.object(self.controller, "bind", side_effect=AssertionError("must not resume or send")):
            asyncio.run(self.controller.execute(review))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (review["id"],))["state"], "exited")

    def test_inactive_chats_and_terminal_ticks_are_noops(self):
        before = "\n".join(self.store.db.iterdump())
        for caller in (OTHER, "native-main", "native-worker", ""):
            with patch.dict(os.environ, {"CODEX_THREAD_ID": caller}):
                self.assertFalse(patrol(self.store)["active"])
                with self.assertRaises(ValueError):
                    heartbeat_plan(self.store)
        self.assertEqual("\n".join(self.store.db.iterdump()), before)

    def test_heartbeat_receipt_survives_repeated_setup_and_unbind_disables_ticks(self):
        plan = heartbeat_plan(self.store)
        self.assertEqual(plan["entry_thread_id"], ENTRY)
        self.assertEqual(plan["interval_minutes"], 10)
        self.assertIsNone(plan["saved"])
        receipt = bind_heartbeat(self.store, "actual-app-automation-id")
        self.assertEqual(bind_heartbeat(self.store, "actual-app-automation-id"), receipt)
        self.assertEqual(heartbeat_plan(self.store)["saved"], receipt)
        with self.assertRaisesRegex(ValueError, "different heartbeat"):
            bind_heartbeat(self.store, "duplicate-id")
        result = dispatch(Namespace(command="unbind-entry"), self.store)
        self.assertEqual(result["heartbeat_to_pause"], receipt)
        self.assertFalse(patrol(self.store)["active"])
        self.report()
        self.complete()
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))

    def test_real_cli_inactive_patrol_creates_no_state_and_does_not_read_stdin(self):
        directory = Path(self.temp.name) / "not-initialized"
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        env = {**os.environ, "CODEX_THREAD_ID": OTHER, "PATH": ""}
        result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
            "--state-dir", str(directory), "patrol"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["active"])
        self.assertFalse(directory.exists())

    def test_patrol_emits_only_changed_work_and_does_not_repeat_closed_history(self):
        for index in range(40):
            self.store.db.execute("INSERT INTO chats(id,role,title,cwd,closed,created_at) VALUES(?,'worker',?,?,1,0)",
                ("old-" + str(index), "historical detail " * 20, str(self.project)))
        first = patrol(self.store)
        self.assertEqual([w["work_id"] for w in first["works"]], ["work"])
        self.assertEqual(first["counts"]["registered"], 41)
        quiet = patrol(self.store)
        self.assertFalse(quiet["changed"])
        self.assertEqual(quiet["works"], [])
        self.assertLess(len(json.dumps(quiet)), 1500)
        self.store.db.execute("UPDATE chats SET title='verified change' WHERE id='work'")
        changed = patrol(self.store)
        self.assertEqual([w["work_id"] for w in changed["works"]], ["work"])
        self.store.db.execute("UPDATE chats SET closed=1 WHERE id='work'")
        closed = patrol(self.store)
        self.assertTrue(closed["works"][0]["closed"])
        self.assertEqual(patrol(self.store)["works"], [])


if __name__ == "__main__":
    unittest.main()
