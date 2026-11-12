"""Activation belongs to one actual chat, not a directory or copied history."""

import asyncio
from argparse import Namespace
import io
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
from agent_manager_runtime.activation import (
    CONTROL_COMMANDS, background_environment, check_binding, session_context,
)
from agent_manager_runtime.cli import dispatch, entry_command, parser
from agent_manager_runtime.notifications import acknowledge, bind_entry, message, notify
from agent_manager_runtime.store import Store, decode, encode
from agent_manager_runtime.bindings import Bindings

ENTRY = "01a1befa-15e7-7fa2-96de-7266ea1f6303"
OTHER = "01a1befa-a180-7a92-b798-fa4ee66a562f"
MAIN = "01a1befa-b180-7a92-b798-fa4ee66a562f"
WORKER = "01a1befa-c180-7a92-b798-fa4ee66a562f"


class ActivationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"CODEX_THREAD_ID": ENTRY})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        registry = Bindings(Path(self.temp.name) / "entries")
        for target in ("agent_manager_runtime.cli.Bindings", "agent_manager_runtime.bindings.Bindings"):
            index = patch(target, return_value=registry)
            index.start()
            self.addCleanup(index.stop)
        self.main = self.store.setup(self.project)
        self.store.db.execute("UPDATE chats SET thread_id=? WHERE id=?", (MAIN, self.main["id"]))
        self.store.db.execute("INSERT INTO chats(id,role,title,cwd,thread_id,created_at) VALUES('work','worker','existing',?,?,0)",
                              (str(self.project), WORKER))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def dump(self):
        return "\n".join(self.store.db.iterdump())

    def test_unbound_chat_control_commands_reject_before_any_effect_or_stdin(self):
        before = self.dump()
        changes = self.store.db.total_changes
        with patch("agent_manager_runtime.cli.subprocess.Popen", side_effect=AssertionError("must not spawn")), \
                patch("agent_manager_runtime.cli.stdin_or", side_effect=AssertionError("must not read input")):
            for command in CONTROL_COMMANDS:
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "not this manager's active entry"):
                    dispatch(Namespace(command=command), self.store)
        self.assertFalse(session_context(self.store)["active"])
        self.assertEqual((self.dump(), self.store.db.total_changes), (before, changes))

    def test_same_directory_other_chat_and_managed_threads_do_not_inherit_activation(self):
        bind_entry(self.store, ENTRY)
        before = self.dump()
        for caller, role in [(OTHER, "other"), (MAIN, "main"), (WORKER, "worker")]:
            with self.subTest(role=role), patch.dict(os.environ, {"CODEX_THREAD_ID": caller}):
                context = dispatch(Namespace(command="context"), self.store)
                self.assertEqual(context["role"], role)
                self.assertFalse(context["active"])
                for command in ("submit", "start", "followup", "answer", "source-add", "unbind-entry"):
                    with self.assertRaisesRegex(ValueError, "not this manager's active entry"):
                        dispatch(Namespace(command=command), self.store)
        self.assertEqual(self.dump(), before)

    def test_binding_uses_actual_host_id_and_never_takes_over_other_entry(self):
        before = self.dump()
        with patch("agent_manager_runtime.cli.Codex", side_effect=AssertionError("no RPC before rejection")):
            with self.assertRaisesRegex(ValueError, "requires a real current"):
                asyncio.run(entry_command(self.store, Namespace(command="bind-entry", thread_id=None)))
            with self.assertRaisesRegex(ValueError, "actual current CODEX_THREAD_ID"):
                asyncio.run(entry_command(self.store, Namespace(command="bind-entry", thread_id=OTHER)))
            for caller in (MAIN, WORKER):
                with patch.dict(os.environ, {"CODEX_THREAD_ID": caller}), self.assertRaisesRegex(ValueError, "managed main or worker"):
                    asyncio.run(entry_command(self.store, Namespace(command="bind-entry", thread_id=caller)))
            bind_entry(self.store, OTHER)
            with self.assertRaisesRegex(ValueError, "another active entry"):
                asyncio.run(entry_command(self.store, Namespace(command="bind-entry", thread_id=ENTRY)))
        self.assertEqual(self.store.main()["entry_thread_id"], OTHER)
        bind_entry(self.store, ENTRY)
        self.assertNotEqual(self.dump(), before)  # Only the explicit fixture binding changed.

    def test_explicit_setup_binding_activates_only_current_chat_and_can_submit_original_input(self):
        calls = []
        project = str(self.project.resolve())

        class NativeRead:
            def __init__(client, *args):
                pass

            async def start(client):
                pass

            async def call(client, method, params):
                calls.append((method, params))
                return {"thread": {"id": params["threadId"], "cwd": project}}

            async def close(client):
                pass

        args = parser().parse_args(["bind-entry"])
        with patch("agent_manager_runtime.cli.Codex", NativeRead):
            asyncio.run(entry_command(self.store, args))
            asyncio.run(entry_command(self.store, args))
        self.assertTrue(session_context(self.store)["active"])
        self.assertEqual(calls, [("thread/read", {"threadId": ENTRY})] * 2)
        original = "Keep the original scope.\nDo not publish.\n"
        with patch("sys.stdin", io.StringIO(original)):
            job = dispatch(parser().parse_args(["submit"]), self.store)
        self.assertEqual(decode(job["payload"])["text"], original)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM chats")["n"], 2)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM runs")["n"], 0)
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='task'")), 1)

    def test_read_only_queries_do_not_activate_other_chat_or_create_jobs(self):
        bind_entry(self.store, OTHER)
        before = self.dump()
        for command in ("context", "list", "status"):
            result = dispatch(Namespace(command=command), self.store)
            self.assertIsInstance(result, dict)
        self.assertFalse(session_context(self.store)["active"])
        self.assertEqual(self.dump(), before)

    def test_copied_ack_id_cannot_impersonate_actual_host_or_activate_a_chat(self):
        bind_entry(self.store, ENTRY)
        job = notify(self.store, "fixture", "work", "result", parent_id=None)
        before = self.dump()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER}):
            with self.assertRaisesRegex(ValueError, "cannot override"):
                acknowledge(self.store, job["id"], ENTRY)
            with self.assertRaisesRegex(ValueError, "not this manager's active entry"):
                dispatch(Namespace(command="ack", event_id=job["id"], thread=ENTRY), self.store)
            self.assertFalse(session_context(self.store)["active"])
        self.assertEqual(self.dump(), before)
        self.assertEqual(acknowledge(self.store, job["id"])["phase"], "acknowledged")

    def test_deactivation_leaves_existing_work_and_authorized_background_input_unchanged(self):
        bind_entry(self.store, ENTRY)
        job, _ = self.store.enqueue("task", "work", {"text": "already authorized"})
        dispatch(Namespace(command="unbind-entry"), self.store)
        self.assertFalse(session_context(self.store)["active"])
        self.assertEqual(self.store.one("SELECT * FROM jobs WHERE id=?", (job["id"],))["status"], "pending")
        # The scheduler uses Store directly; an entry's return or deactivation is not a stop.
        run = self.store.claim("work")
        self.assertEqual(run["job_id"], job["id"])
        self.assertEqual(self.store.one("SELECT thread_id FROM chats WHERE id='work'")["thread_id"], WORKER)

    def test_terminal_and_internal_services_do_not_inherit_a_launching_chat_identity(self):
        bind_entry(self.store, ENTRY)
        parent = dict(os.environ)
        env = background_environment()
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertEqual(dict(os.environ), parent)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(session_context(self.store)["role"], "terminal")
            self.assertFalse(session_context(self.store)["active"])
            job = dispatch(Namespace(command="submit", text="explicit terminal input"), self.store)
            self.assertEqual(decode(job["payload"])["text"], "explicit terminal input")
            # Explicit terminals also unbind before replacing the current entry.
            with self.assertRaisesRegex(ValueError, "another active entry"):
                check_binding(self.store, OTHER)

    def test_context_real_cli_on_uninitialized_project_does_not_create_database(self):
        directory = Path(self.temp.name) / "uninitialized"
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
            "--state-dir", str(directory), "context"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["active"])
        self.assertFalse(directory.exists())

    def test_real_cli_rejects_foreign_dispatch_and_copied_ack_without_starting_codex(self):
        bind_entry(self.store, ENTRY)
        notification = notify(self.store, "fixture", "work", "result", parent_id=None)
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        before = self.dump()
        env = {**os.environ, "CODEX_THREAD_ID": OTHER, "PATH": ""}
        for command in (["submit", "do work"], ["start"], ["bind-entry", ENTRY],
                        ["bind-entry", OTHER], ["ack", notification["id"], "--thread", ENTRY]):
            result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
                "--state-dir", str(self.store.directory), *command], capture_output=True, text=True,
                timeout=10, env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("error", json.loads(result.stderr))
        self.assertEqual(self.dump(), before)

    def test_legacy_context_and_denied_dispatch_do_not_migrate_or_write_state(self):
        directory = Path(self.temp.name) / "legacy"
        directory.mkdir()
        path = directory / "state.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE chats(id TEXT,role TEXT,thread_id TEXT)")
            db.execute("INSERT INTO chats VALUES('legacy','main',?)", (MAIN,))
            db.commit()
        finally:
            db.close()
        before = path.read_bytes()
        launcher = Path(__file__).resolve().parents[1] / "scripts/agent_manager.py"
        for command, expected in [(["context"], 0), (["submit", "do not dispatch"], 1)]:
            result = subprocess.run([sys.executable, str(launcher), "--project", str(self.project),
                "--state-dir", str(directory), *command], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(path.read_bytes(), before)
        with sqlite3.connect(path) as db:
            self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(chats)")], ["id", "role", "thread_id"])

    def test_notification_contains_exact_target_and_actual_host_ack_without_override(self):
        bind_entry(self.store, ENTRY)
        job = notify(self.store, "fixture", "work", "result", parent_id=None)
        text = message(self.store, job)
        header = json.loads(text.splitlines()[1])
        self.assertEqual(header["entry_thread_id"], ENTRY)
        self.assertIn(" context\n", text)
        self.assertNotIn(" --thread ", text)


if __name__ == "__main__":
    unittest.main()
