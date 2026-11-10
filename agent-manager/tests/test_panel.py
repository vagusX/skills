"""Human interaction over real HTTP/SQLite, without model or permission grants."""

import asyncio
from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.approvals import (COMMAND, FILE, INPUT, PERMISSIONS, ELICITATION,
                                            pending_questions, queue_answer, validate_response)
from agent_manager_runtime.codex import process_start
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.panel import PanelServer, ensure_panel, notify_pending, panel_record, stop_panel
from agent_manager_runtime.store import Store, decode, encode


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.main = self.store.setup(self.project)
        self.job = self.store.submit("Test-only human input")
        self.run = self.store.claim(self.main["id"])
        self.started = process_start(os.getpid())
        self.store.db.execute("UPDATE chats SET thread_id='fixture-thread' WHERE id=?", (self.main["id"],))
        self.store.db.execute("UPDATE runs SET state='running',thread_id='fixture-thread',turn_id='fixture-turn',server_pid=?,server_start=? WHERE id=?",
                              (os.getpid(), self.started, self.run["id"]))
        (self.store.directory / "daemon.json").write_text(encode({"pid": os.getpid(), "started": self.started}))
        self.responses = []
        async def respond(request_id, value):
            self.responses.append((request_id, value))
        async def call(method, params, **kwargs):
            return {"data": [{"id": "fixture-turn", "items": [{"id": "fixture-item", "type": "fileChange", "changes": [{"path": "fixture.txt", "diff": "+test"}]}]}]}
        self.client = SimpleNamespace(process=SimpleNamespace(pid=os.getpid()), started=self.started, respond=respond, call=call)
        self.controller = Controller(self.store)
        self.controller.clients["main"] = self.client
        self.request_id = 0

    def tearDown(self):
        stop_panel(self.store)
        self.store.close()
        self.temp.cleanup()

    def question(self, method=COMMAND, **params):
        self.request_id += 1
        with redirect_stdout(io.StringIO()):
            asyncio.run(self.controller.on_request(self.client, {"method": method, "id": self.request_id,
                "params": {"threadId": "fixture-thread", "turnId": "fixture-turn", "itemId": "fixture-item", **params}}))
        return self.store.one("SELECT * FROM jobs WHERE kind='question' ORDER BY created_at DESC LIMIT 1")

    def test_answer_reaches_original_request_once_and_waits_for_native_confirmation(self):
        q = self.question(command="pwd", availableDecisions=["accept", "cancel"])
        queue_answer(self.store, q["id"], {"decision": "accept"})
        with self.assertRaisesRegex(ValueError, "already"):
            queue_answer(self.store, q["id"], {"decision": "cancel"})
        self.assertEqual(pending_questions(self.store)[0]["status"], "answer_queued")
        asyncio.run(self.controller.answer_questions())
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(self.responses, [(1, {"decision": "accept"})])
        self.assertEqual(self.store.one("SELECT status FROM jobs WHERE id=?", (q["id"],))["status"], "inflight")
        asyncio.run(self.controller.on_event(self.client, {"method": "serverRequest/resolved", "params": {"requestId": 1}}))
        self.assertEqual(pending_questions(self.store), [])
        self.assertEqual(len(self.store.all("SELECT * FROM runs")), 1)

    def test_unoffered_decision_or_expired_turn_cannot_be_approved(self):
        q = self.question(command="pwd", availableDecisions=["accept", "cancel"])
        with self.assertRaisesRegex(ValueError, "not offered"):
            queue_answer(self.store, q["id"], {"decision": "acceptForSession"})
        self.store.db.execute("UPDATE runs SET turn_id='a-different-turn' WHERE id=?", (self.run["id"],))
        with self.assertRaisesRegex(ValueError, "no longer live"):
            queue_answer(self.store, q["id"], {"decision": "accept"})
        self.assertIsNone(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (q["id"],))["result_ref"])
        self.assertEqual(pending_questions(self.store)[0]["status"], "expired")

    def test_dead_owner_cannot_receive_an_answer_even_if_native_pid_still_exists(self):
        q = self.question(command="pwd")
        (self.store.directory / "daemon.json").write_text(encode({"pid": 99999999, "started": "gone"}))
        with self.assertRaisesRegex(ValueError, "no longer live"):
            queue_answer(self.store, q["id"], {"decision": "accept"})
        self.assertEqual(pending_questions(self.store)[0]["status"], "expired")

    def test_saved_human_pause_prevents_a_racing_approval(self):
        q = self.question(command="pwd")
        self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({"request": "wait for me"}), self.main["id"]))
        with self.assertRaisesRegex(ValueError, "no longer live"):
            queue_answer(self.store, q["id"], {"decision": "accept"})
        self.assertIsNotNone(self.store.main()["hold"])
        self.assertIsNone(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (q["id"],))["result_ref"])

    def test_native_question_ids_options_other_and_secret(self):
        q = self.question(INPUT, questions=[{"id": "choice", "header": "Select", "question": "alpha or beta?",
            "options": [{"label": "alpha", "description": "A"}, {"label": "beta", "description": "B"}]},
            {"id": "secret", "header": "Text", "question": "Test-only secret", "isSecret": True}])
        answer = {"answers": {"choice": {"answers": ["alpha"]}, "secret": {"answers": ["fixture-secret"]}}}
        bad = {"answers": {"choice": {"answers": ["unknown"]}, "secret": {"answers": ["fixture-secret"]}}}
        with self.assertRaisesRegex(ValueError, "offered option"):
            queue_answer(self.store, q["id"], bad)
        queue_answer(self.store, q["id"], answer)
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(self.responses[0][1], answer)

    def test_permissions_preserve_requested_path_objects_and_refuse_expansion(self):
        entry = {"access": "read", "path": {"type": "glob_pattern", "pattern": "/tmp/fixture/**"}}
        q = self.question(PERMISSIONS, permissions={"network": {"enabled": True}, "fileSystem": {"entries": [entry]}})
        response = {"permissions": {"fileSystem": {"entries": [entry]}}, "scope": "turn"}
        bad = {"permissions": {"fileSystem": {"entries": [{**entry, "access": "write"}]}}, "scope": "turn"}
        with self.assertRaisesRegex(ValueError, "unrequested path"):
            queue_answer(self.store, q["id"], bad)
        queue_answer(self.store, q["id"], response)
        self.assertEqual(decode(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (q["id"],))["result_ref"]), response)

    def test_mcp_form_preserves_boolean_number_array_types_and_rejects_nan(self):
        schema = {"type": "object", "required": ["enabled", "count", "colors"], "properties": {
            "enabled": {"type": "boolean"}, "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "colors": {"type": "array", "items": {"type": "string", "enum": ["red", "blue"]}, "minItems": 1}}}
        q = self.question(ELICITATION, mode="form", message="Test-only form", requestedSchema=schema)
        request = decode(q["payload"])
        for value in [float("nan"), 2.5, True, 4]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response(request, {"action": "accept", "content": {"enabled": True, "count": value, "colors": ["red"]}})
        response = {"action": "accept", "content": {"enabled": False, "count": 2, "colors": ["red", "blue"]}}
        queue_answer(self.store, q["id"], response)
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(self.responses[0][1], response)

    def test_unsupported_mcp_schema_stays_declineable_without_guessing(self):
        for spec in [{"type": ["string", "null"]}, {"type": "string", "oneOf": "invalid"}, {"type": "array", "items": {"anyOf": [None]}}, {"type": "number", "minimum": []}]:
            with self.subTest(spec=spec):
                q = self.question(ELICITATION, mode="openai/form", requestedSchema={"type": "object", "properties": {"nested": spec}})
                self.assertFalse(pending_questions(self.store)[-1]["form_supported"])
                with self.assertRaisesRegex(ValueError, "unsupported form"):
                    queue_answer(self.store, q["id"], {"action": "accept", "content": {"nested": "guessed"}})
                queue_answer(self.store, q["id"], {"action": "decline", "content": None})

    def test_malformed_text_response_does_not_modify_the_request(self):
        q = self.question(INPUT, questions=[{"id": "q", "header": "Q", "question": "Text"}])
        with self.assertRaises(ValueError):
            queue_answer(self.store, q["id"], {"answers": {"q": None}})
        self.assertIsNone(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (q["id"],))["result_ref"])

    def test_file_approval_includes_the_original_change_details(self):
        self.question(FILE, grantRoot="/tmp/fixture")
        self.assertEqual(pending_questions(self.store)[0]["context"]["changes"], [{"path": "fixture.txt", "diff": "+test"}])

    def test_http_rejects_missing_token_cross_origin_and_duplicate_answer(self):
        q = self.question(command="pwd", availableDecisions=["accept", "cancel"])
        server = PanelServer(self.store.directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def call(path, *, value=None, token=None, origin=None, host=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {}
            if token is not None: headers["Authorization"] = "Bearer " + token
            if origin is not None: headers["Origin"] = origin
            if host is not None: headers["Host"] = host
            if value is not None: headers["Content-Type"] = "application/json"
            connection.request("POST" if value is not None else "GET", path, body=json.dumps(value) if value is not None else None, headers=headers)
            result = connection.getresponse(); body = result.read(); connection.close()
            return result.status, json.loads(body)
        try:
            self.assertEqual(call("/api/questions")[0], 403)
            self.assertEqual(call("/api/questions", token=server.token, host="attacker.example")[0], 403)
            self.assertEqual(call("/api/questions", token=server.token)[1]["questions"][0]["id"], q["id"])
            answer = {"question_id": q["id"], "response": {"decision": "accept"}}
            self.assertEqual(call("/api/answer", value=answer, token=server.token, origin="https://attacker.example")[0], 403)
            self.assertIsNone(self.store.one("SELECT result_ref FROM jobs WHERE id=?", (q["id"],))["result_ref"])
            self.assertEqual(call("/api/answer", value=answer, token=server.token, origin=server.origin)[0], 200)
            self.assertEqual(call("/api/answer", value=answer, token=server.token, origin=server.origin)[0], 409)
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_notifications_only_for_new_live_questions_not_progress_or_refreshes(self):
        with patch("agent_manager_runtime.panel.open_panel") as opened, patch("agent_manager_runtime.panel.notify") as notified:
            seen = notify_pending(self.store, {}, set())
            self.assertFalse(opened.called)
            q = self.question(command="pwd")
            seen = notify_pending(self.store, {}, seen)
            notify_pending(self.store, {}, seen)
            opened.assert_not_called()
            self.assertEqual(notified.call_count, 1)
            queue_answer(self.store, q["id"], {"decision": "cancel"})
            seen = notify_pending(self.store, {}, seen)
            self.question(INPUT, questions=[{"id": "q", "header": "Q", "question": "Test-only question"}])
            notify_pending(self.store, {}, seen)
            opened.assert_not_called()
            self.assertEqual(notified.call_count, 2)

    def test_pending_questions_and_restarts_never_open_browser_automatically(self):
        self.question(command="pwd")
        with patch("agent_manager_runtime.panel.open_panel") as opened, patch("agent_manager_runtime.panel.notify"):
            seen = notify_pending(self.store, {}, set())
            notify_pending(self.store, {}, seen)
            notify_pending(self.store, {}, set())
        opened.assert_not_called()

    def test_new_question_does_not_repeat_notification_for_an_existing_waiting_batch(self):
        self.question(command="pwd")
        with patch("agent_manager_runtime.panel.open_panel"), patch("agent_manager_runtime.panel.notify") as notified:
            seen = notify_pending(self.store, {}, set())
            self.question(INPUT, questions=[{"id": "q", "header": "Q", "question": "Another fixture question"}])
            notify_pending(self.store, {}, seen)
        self.assertEqual(notified.call_count, 1)

    def test_explicit_panel_open_still_opens_with_pending_questions(self):
        self.question(command="pwd")
        record = {"pid": 123, "started": "fixture", "port": 1, "url": "http://127.0.0.1:1/", "token": "fixture" * 8}
        fake = SimpleNamespace(poll=lambda: None)
        with patch("agent_manager_runtime.panel.panel_record", side_effect=[None, record]), patch("agent_manager_runtime.panel.subprocess.Popen", return_value=fake), patch("agent_manager_runtime.panel.pending_questions", return_value=[{"status": "awaiting_confirmation"}]), patch("agent_manager_runtime.panel.open_panel") as opened:
            ensure_panel(self.store, str(self.project), open_now=True)
        opened.assert_called_once_with(record)

    def test_panel_attaches_idempotently_without_creating_native_runs(self):
        first = ensure_panel(self.store, str(self.project))
        second = ensure_panel(self.store, str(self.project))
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(panel_record(self.store)["pid"], first["pid"])
        self.assertEqual(len(self.store.all("SELECT * FROM runs")), 1)
        self.assertEqual(len(self.store.all("SELECT name FROM sqlite_master WHERE type='table'")), 4)
        self.assertEqual((self.store.directory / "panel.json").stat().st_mode & 0o777, 0o600)

    def test_browser_launch_failure_still_returns_an_accessible_panel_address(self):
        with patch("agent_manager_runtime.panel.open_panel", side_effect=RuntimeError("headless fixture")):
            record = ensure_panel(self.store, str(self.project), open_now=True)
        self.assertIn("open_error", record)
        self.assertTrue(record["url"].startswith("http://127.0.0.1:"))
        self.assertGreater(len(record["token"]), 32)


if __name__ == "__main__":
    unittest.main()
