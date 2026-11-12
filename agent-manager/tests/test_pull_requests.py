"""PR feedback is versioned input to the original work, never a new assignment."""

import asyncio
from argparse import Namespace
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.bindings import Bindings
from agent_manager_runtime.cli import dispatch
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.notifications import bind_entry
from agent_manager_runtime.pull_requests import REASON, github_pr_poll, pr_status, watch_pr
from agent_manager_runtime.sources import commit_poll, fetch_source
from agent_manager_runtime.store import Store, decode, encode

ENTRY = "00000000-0000-4000-8000-000000000001"
URL = "https://github.com/owner/project/pull/7"
API = "https://api.github.com/repos/owner/project/pulls/7"


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.directory = Path(self.temp.name) / "state"
        self.store = Store(self.directory)
        self.addCleanup(lambda: self.store.close())
        self.main = self.store.setup(self.project)
        bind_entry(self.store, ENTRY)
        self.store.db.execute("UPDATE chats SET thread_id='main-native',binding='linked' WHERE role='main'")
        self.store.db.execute("INSERT INTO sources(id,kind,config) VALUES('original-issue','github',?)", (encode({"repo": "owner/project"}),))
        self.store.db.execute("""INSERT INTO chats(id,role,title,cwd,thread_id,binding,source_id,source_key,report_state,created_at)
            VALUES('work','worker','original work',?,'worker-native','linked','original-issue','github:owner/project:3','ready',1)""", (str(self.project),))
        original, _ = self.store.enqueue("task", "work", {"text": "Original request: repair the calculation and verify it. Do not merge, publish or reply."}, status="done")
        self.store.db.execute("UPDATE chats SET request_job_id=? WHERE id='work'", (original["id"],))
        self.controller = Controller(self.store)
        self.config = {"repo": "owner/project", "pull": 7, "work_id": "work", "allow_reply": False}
        self.pr = {"number": 7, "url": API, "html_url": URL, "state": "open", "merged": False,
                   "head": {"sha": "head-1"}, "base": {"repo": {"full_name": "owner/project"}}}
        self.pages = {"comment": [[]], "review": [[]], "review_comment": [[]]}
        self.calls = []
        registry = Bindings(Path(self.temp.name) / "entries")
        for target, value in (("agent_manager_runtime.pull_requests.Bindings", registry),):
            p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": ENTRY})
        env.start()
        self.addCleanup(env.stop)
        api = patch("agent_manager_runtime.pull_requests.gh_api", side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)

    def api(self, path, **kwargs):
        self.calls.append(path)
        if path == "repos/owner/project/pulls/7":
            return copy.deepcopy(self.pr)
        kind = {"issues/7/comments": "comment", "pulls/7/reviews": "review", "pulls/7/comments": "review_comment"}
        endpoint = path.removeprefix("repos/owner/project/").split("?")[0]
        self.assertTrue(kwargs.get("paginate"))
        return copy.deepcopy(self.pages[kind[endpoint]])

    def item(self, kind="comment", ident=1, body="Please fix the original calculation"):
        item = {"id": ident, "body": body, "html_url": URL + "#comment-" + str(ident), "user": {"login": "reviewer"},
                "issue_url" if kind == "comment" else "pull_request_url": API.replace("/pulls/", "/issues/") if kind == "comment" else API,
                "updated_at": "2026-11-12T04:00:00Z"}
        if kind == "review":
            item.update(state="CHANGES_REQUESTED", commit_id="old-head")
        if kind == "review_comment":
            item.update(path="calc.py", line=7, original_line=5, commit_id="old-head", original_commit_id="first-head",
                        in_reply_to_id=9, pull_request_review_id=4)
        return item

    def watch(self):
        with self.store.transaction():
            receipt = watch_pr(self.store, "work", URL)
        return self.store.one("SELECT * FROM sources WHERE id=?", (receipt["source_id"],))

    def poll(self, source=None):
        source = source or self.store.one("SELECT * FROM sources WHERE kind='github-pr'")
        return commit_poll(self.store, source, *fetch_source(self.store, source))

    def pending_review(self):
        self.watch()
        self.pages["comment"] = [[self.item()]]
        self.assertEqual(self.poll(), 1)
        return self.store.one("SELECT * FROM jobs WHERE kind='wake'")

    def start_review(self):
        run = self.store.claim(self.main["id"])
        self.assertIsNotNone(run)
        self.store.db.execute("UPDATE runs SET thread_id='main-native',turn_id='review-turn',state='running' WHERE id=?", (run["id"],))
        return run

    def followup(self, work="work", call="followup-call"):
        return asyncio.run(self.controller.business_tool({"threadId": "main-native", "turnId": "review-turn", "callId": call,
            "tool": "sa_followup", "arguments": {"work_id": work, "text": "Inspect this PR feedback and correct the original calculation within the original request. Verify acceptance; do not merge or reply."}}))

    def complete_review(self, run, outcome="no_action", summary="Acknowledgement only; no change requested"):
        turn = {"id": "review-turn", "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer",
                 "text": json.dumps({"outcome": outcome, "summary": summary})}]}
        asyncio.run(self.controller.completed(run, turn))
        return decode(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (run["job_id"],))["result_ref"])["pr_review"]

    def test_three_comment_kinds_and_edits_reverts_are_distinct_but_positions_are_not(self):
        for kind in self.pages:
            self.pages[kind] = [[self.item(kind)]]
        events, cursor = github_pr_poll(self.config, None)
        changes = events[0]["context"]["changes"]
        self.assertEqual({c["entity"] for c in changes}, set(self.pages))
        inline = next(c for c in changes if c["entity"] == "review_comment")
        self.assertEqual((inline["commit_id"], inline["path"], inline["in_reply_to_id"]), ("old-head", "calc.py", 9))
        self.pages["review_comment"][0][0]["line"] = None
        self.pr["head"]["sha"] = "new-head"
        self.assertEqual(github_pr_poll(self.config, cursor)[0], [])
        self.pages["comment"][0][0]["body"] = "Edited requirement"
        edited, second = github_pr_poll(self.config, cursor)
        self.pages["comment"][0][0]["body"] = self.item()["body"]
        reverted, third = github_pr_poll(self.config, second)
        self.assertNotEqual(edited[0]["key"], reverted[0]["key"])
        self.assertEqual(third["objects"]["comment:1"]["revision"], 3)

    def test_pending_review_is_not_delivered_until_submitted_and_state_change_is_detected(self):
        item = self.item("review")
        item["state"] = "PENDING"
        self.pages["review"] = [[item]]
        inline = self.item("review_comment")
        inline["pull_request_review_id"] = item["id"]
        self.pages["review_comment"] = [[inline]]
        self.assertEqual(github_pr_poll(self.config, None)[0], [])
        item["state"] = "APPROVED"
        events, cursor = github_pr_poll(self.config, None)
        self.assertEqual(events[0]["context"]["changes"][0]["state"], "APPROVED")
        item["state"] = "DISMISSED"
        self.assertTrue(github_pr_poll(self.config, cursor)[0])

    def test_batch_limit_preserves_undelivered_revisions(self):
        self.pages["comment"] = [[self.item(ident=n) for n in range(1, 26)]]
        first, cursor = github_pr_poll(self.config, None)
        second, cursor = github_pr_poll(self.config, cursor)
        self.assertEqual([len(e[0]["context"]["changes"]) for e in (first, second)], [20, 5])
        self.assertEqual(github_pr_poll(self.config, cursor)[0], [])

    def test_failed_or_inconsistent_pagination_does_not_commit_any_cursor(self):
        source = self.watch()
        bad = self.item()
        bad["issue_url"] = "https://api.github.com/repos/other/repo/issues/7"
        self.pages["comment"] = [[bad]]
        with self.assertRaisesRegex(ValueError, "another PR"):
            self.poll(source)
        self.assertIsNone(self.store.one("SELECT cursor FROM sources WHERE id=?", (source["id"],))["cursor"])
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))
        first = self.item()
        second = {**first, "body": "changed while paginating"}
        self.pages["comment"] = [[first], [second]]
        with self.assertRaisesRegex(ValueError, "pagination"):
            self.poll(source)
        with patch("agent_manager_runtime.pull_requests.gh_api", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.poll(source)
        self.assertIsNone(self.store.one("SELECT cursor FROM sources WHERE id=?", (source["id"],))["cursor"])

    def test_registration_is_idempotent_and_does_not_replace_original_issue_owner(self):
        source = self.watch()
        same = self.watch()
        self.assertEqual(source["id"], same["id"])
        work = self.store.one("SELECT * FROM chats WHERE id='work'")
        self.assertEqual((work["source_id"], work["source_key"]), ("original-issue", "github:owner/project:3"))
        self.store.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (source["id"],))
        self.assertFalse(self.watch()["enabled"])
        receipt = dispatch(Namespace(command="watch-pr", work_id="work", url=URL), self.store)
        self.assertTrue(receipt["enabled"])
        for url in (URL + "#comment", "https://evil.example/pull/7", URL + "?x=1"):
            with self.assertRaises(ValueError):
                dispatch(Namespace(command="watch-pr", work_id="work", url=url), self.store)

    def test_report_automatically_registers_only_exact_delivery_evidence_urls(self):
        job, _ = self.store.enqueue("followup", "work", {"text": "Finish the original delivery"})
        run = self.store.claim("work")
        self.store.db.execute("UPDATE runs SET turn_id='worker-turn',state='running' WHERE id=?", (run["id"],))
        params = {"threadId": "worker-native", "turnId": "worker-turn", "callId": "report", "tool": "sa_report", "arguments": {
            "state": "ready", "summary": URL, "evidence": ["Reference: " + URL, URL, URL], "remaining": [], "question": None}}
        result = asyncio.run(self.controller.business_tool(params))
        self.assertEqual(len(result["pull_requests"]), 1)
        self.assertEqual(asyncio.run(self.controller.business_tool(params)), result)
        self.assertEqual(len(self.store.all("SELECT id FROM sources WHERE kind='github-pr'")), 1)

    def test_comments_wake_manager_and_continue_same_ready_worker_once_across_restart(self):
        job = self.pending_review()
        self.store.close()
        self.store = Store(self.directory)
        self.controller = Controller(self.store)
        self.assertEqual(self.poll(), 0)
        run = self.start_review()
        result = self.followup()
        self.assertEqual(self.followup(call="different-native-call"), result)
        self.assertEqual(self.complete_review(run, "continued", "Original worker continues"),
            {"outcome": "continued", "summary": "Original worker continues", "followup_job_id": result["job_id"]})
        next_run = self.store.claim("work")
        self.assertEqual((next_run["thread_id"], next_run["job_id"]), ("worker-native", result["job_id"]))
        self.assertEqual(len(self.store.all("SELECT id FROM chats WHERE role='worker'")), 1)
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='notification'"))
        original = asyncio.run(self.controller.payload(self.store.one("SELECT * FROM jobs WHERE id=(SELECT request_job_id FROM chats WHERE id='work')")))
        self.assertIn("Do not merge, publish or reply", original["text"])

    def test_hold_pending_approval_unknown_execution_and_unbinding_defer_without_losing_comments(self):
        job = self.pending_review()
        self.store.db.execute("UPDATE chats SET hold='human pause' WHERE id='work'")
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.store.db.execute("UPDATE chats SET hold=NULL WHERE id='work'")
        question, _ = self.store.enqueue("question", "work", {"method": "item/commandExecution/requestApproval"})
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.store.db.execute("UPDATE jobs SET status='done' WHERE id=?", (question["id"],))
        task, _ = self.store.enqueue("followup", "work", {"text": "Human correction"})
        busy = self.store.claim("work")
        self.store.db.execute("UPDATE runs SET state='unknown' WHERE id=?", (busy["id"],))
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.store.finish(busy["id"], "completed")
        self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        self.calls.clear()
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.claim(self.main["id"]))
        # A blocked review cannot block unrelated human chat input.
        human = self.store.submit("ordinary chat", kind="chat")
        human_run = self.store.claim(self.main["id"])
        self.assertEqual(human_run["job_id"], human["id"])
        self.store.finish(human_run["id"], "completed")
        bind_entry(self.store, ENTRY)
        self.assertEqual(self.start_review()["job_id"], job["id"])
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='outbox'"))

    def test_nonaction_is_silent_but_human_decision_has_one_notification_and_no_answer(self):
        job = self.pending_review()
        run = self.start_review()
        self.assertEqual(self.complete_review(run)["outcome"], "no_action")
        self.assertEqual(pr_status(self.store, "work")[0]["latest_receipt"]["outcome"], "no_action")
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='notification'"))
        self.pages["comment"][0][0]["body"] = "Please deploy to production"
        self.assertEqual(self.poll(), 1)
        run = self.start_review()
        self.assertEqual(self.complete_review(run, "needs_user", "Reviewer requests deployment; confirm target and authority.")["outcome"], "needs_user")
        notices = self.store.all("SELECT * FROM jobs WHERE kind='notification'")
        self.assertEqual(len(notices), 1)
        self.assertEqual(decode(notices[0]["payload"])["kind"], "decision")
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind IN ('followup','outbox','question')"))

    def test_claimed_review_rechecks_unbinding_before_sending_native_input(self):
        job = self.pending_review()
        run = self.store.claim(self.main["id"])
        self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        async def forbidden(*args):
            raise AssertionError("must not bind or send native input")
        self.controller.bind = forbidden
        asyncio.run(self.controller.execute(run))
        self.assertEqual(self.store.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"], "pending")
        bind_entry(self.store, ENTRY)
        self.assertEqual(self.store.claim(self.main["id"])["job_id"], job["id"])

    def test_pr_wake_cannot_create_workers_change_owner_or_bypass_hold(self):
        self.pending_review()
        run = self.start_review()
        self.store.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES('other','worker','other',?,2)", (str(self.project),))
        with self.assertRaisesRegex(ValueError, "original worker"):
            self.followup(work="other")
        self.store.db.execute("UPDATE chats SET hold='human pause' WHERE id='work'")
        with self.assertRaisesRegex(ValueError, "on hold"):
            self.followup()
        with self.assertRaisesRegex(ValueError, "new work"):
            asyncio.run(self.controller.business_tool({"threadId": "main-native", "turnId": "review-turn", "callId": "create",
                "tool": "sa_create_work", "arguments": {"title": "replacement"}}))
        with self.assertRaisesRegex(ValueError, "another work"):
            with self.store.transaction():
                watch_pr(self.store, "other", URL)

    def test_closed_and_merged_prs_never_dispatch_comments_or_recreate_work(self):
        job = self.pending_review()
        self.pr["state"] = "closed"
        self.assertEqual(self.poll(), 0)
        self.assertIsNone(self.store.claim(self.main["id"]))
        self.pr["state"] = "open"
        self.poll()
        self.assertIsNotNone(self.store.claim(self.main["id"]))
        # A merge is terminal for this watcher; an already queued child cannot run.
        self.pr.update(state="closed", merged=True)
        self.poll()
        source = self.store.one("SELECT * FROM sources WHERE kind='github-pr'")
        self.assertFalse(source["enabled"])
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.store.db.execute("UPDATE runs SET state='running',turn_id='review-turn' WHERE job_id=?", (job["id"],))
            self.followup()

    def test_missing_or_false_continued_receipt_is_reported_as_failure(self):
        self.pending_review()
        run = self.start_review()
        self.assertEqual(self.complete_review(run, "continued", "Claimed completion without tool receipt")["outcome"], "failed")
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='notification'")), 1)

    def test_native_schema_is_main_only_and_minimal_completion_reads_exact_native_final(self):
        self.pending_review()
        run = self.store.claim(self.main["id"])
        calls = []
        class Native:
            process = Namespace(pid=os.getpid())
            started = "native-process-identity"
            async def call(self, method, params):
                calls.append((method, params))
                return {"turn": {"id": "review-turn"}}
            async def history(self, thread_id, limit=None):
                self_thread = "main-native"
                if thread_id != self_thread:
                    raise AssertionError(thread_id)
                return [{"id": "review-turn", "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer",
                         "text": '{"outcome":"no_action","summary":"Approval only"}'}]}]
        native = Native()
        async def bind(chat):
            return native
        self.controller.bind = bind
        self.controller.clients["main"] = native
        asyncio.run(self.controller.execute(run))
        self.assertIn("outputSchema", calls[0][1])
        asyncio.run(self.controller.completed(run, {"id": "review-turn", "status": "completed", "items": []}))
        receipt = decode(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (run["job_id"],))["result_ref"])
        self.assertEqual(receipt["pr_review"]["outcome"], "no_action")
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='notification'"))

    def test_interrupted_review_recovers_same_event_without_another_worker_followup(self):
        original = self.pending_review()
        run = self.start_review()
        result = self.followup()
        self.store.finish(run["id"], "interrupted")
        child = self.store.claim("work")
        self.store.finish(child["id"], "completed")
        payload = {**decode(original["payload"]), "original_kind": "wake", "original_job_id": original["id"]}
        recovery, _ = self.store.enqueue("recover", self.main["id"], payload, source_id=original["source_id"], parent_id=original["id"])
        recovered = self.start_review()
        self.assertEqual(recovered["job_id"], recovery["id"])
        self.assertEqual(self.followup(call="after-restart"), result)
        self.assertEqual(self.complete_review(recovered, "continued", "Original continuation already recorded")["followup_job_id"], result["job_id"])
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='followup'")), 1)

    def test_queued_pr_followup_stays_deferred_when_entry_is_unbound(self):
        self.pending_review()
        run = self.start_review()
        result = self.followup()
        self.complete_review(run, "continued", "Original worker continuation queued")
        self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        self.assertIsNone(self.store.claim("work"))
        bind_entry(self.store, ENTRY)
        self.assertEqual(self.store.claim("work")["job_id"], result["job_id"])

    def test_deleted_comment_author_does_not_prevent_collection(self):
        item = self.item()
        item["user"] = None
        self.pages["comment"] = [[item]]
        events, cursor = github_pr_poll(self.config, None)
        self.assertIsNone(events[0]["context"]["changes"][0]["author"])
        self.assertIn("comment:1", cursor["objects"])

    def test_unbind_or_disable_during_network_read_does_not_commit_events(self):
        source = self.watch()
        self.pages["comment"] = [[self.item()]]
        events, cursor = fetch_source(self.store, source)
        self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        self.assertEqual(commit_poll(self.store, source, events, cursor), 0)
        bind_entry(self.store, ENTRY)
        self.store.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (source["id"],))
        self.assertEqual(commit_poll(self.store, source, events, cursor), 0)
        self.assertIsNone(self.store.one("SELECT cursor FROM sources WHERE id=?", (source["id"],))["cursor"])
        self.assertFalse(self.store.all("SELECT id FROM jobs WHERE kind='wake'"))

    def test_source_failure_notifies_once_until_a_successful_poll(self):
        source = self.watch()
        with patch.object(self.controller, "poll_source", side_effect=RuntimeError("GitHub unavailable")):
            asyncio.run(self.controller.source_task(source["id"]))
            asyncio.run(self.controller.source_task(source["id"]))
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='notification'")), 1)
        self.assertEqual(self.poll(), 0)
        with patch.object(self.controller, "poll_source", side_effect=RuntimeError("GitHub unavailable")):
            asyncio.run(self.controller.source_task(source["id"]))
        self.assertEqual(len(self.store.all("SELECT id FROM jobs WHERE kind='notification'")), 2)
        self.assertIsNone(pr_status(self.store, "work")[0]["latest_receipt"])

    def test_accepted_worker_input_can_recover_after_monitoring_is_disabled_and_entry_unbound(self):
        self.pending_review()
        run = self.start_review()
        result = self.followup()
        self.complete_review(run, "continued", "Original continuation queued")
        child = self.store.claim("work")
        self.store.db.execute("UPDATE jobs SET native_ref=? WHERE id=?", (encode({"thread_id": "worker-native", "turn_id": "accepted-worker-turn"}), result["job_id"]))
        self.store.finish(child["id"], "interrupted")
        original = self.store.one("SELECT * FROM jobs WHERE id=?", (result["job_id"],))
        payload = {**decode(original["payload"]), "original_kind": "followup", "original_job_id": original["id"]}
        recovery, _ = self.store.enqueue("recover", "work", payload, source_id=original["source_id"], parent_id=original["id"])
        self.store.db.execute("UPDATE sources SET enabled=0 WHERE kind='github-pr'")
        self.store.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        recovered = self.store.claim("work")
        self.assertEqual((recovered["job_id"], recovered["thread_id"]), (recovery["id"], "worker-native"))
