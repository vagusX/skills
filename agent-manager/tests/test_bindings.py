"""Only the registered root entry receives manager context or controls its cwd."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_prompt import main as hook_main
from agent_manager_runtime.activation import check_binding, session_context
from agent_manager_runtime.bindings import Bindings, prompt_context
from agent_manager_runtime.cli import main as cli_main
from agent_manager_runtime.notifications import bind_entry
from agent_manager_runtime.store import Store

ENTRY = "01a1befa-a180-7a92-b798-fa4ee66a562f"
OTHER = "01a1befa-15e7-7fa2-96de-7266ea1f6303"


def concurrent_bind(project, state, registry, entry, output):
    store = Store(state)
    try:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": entry}):
            with Bindings(registry).claim(store, entry, project):
                with store.transaction():
                    check_binding(store, entry)
                    bind_entry(store, entry)
        output.put("bound")
    except ValueError:
        output.put("conflict")
    finally:
        store.close()


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.entry_cwd = Path(self.temp.name) / "entry-project"
        self.entry_cwd.mkdir()
        self.registry = Bindings(Path(self.temp.name) / "registry")
        self.a = Store(Path(self.temp.name) / "state-a")
        self.b = Store(Path(self.temp.name) / "state-b")
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.a.setup(self.project)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        self.b.setup(alias)
        self.environment = patch.dict(os.environ, {"CODEX_THREAD_ID": ENTRY})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target in ("agent_manager_runtime.cli.Bindings", "agent_manager_runtime.bindings.Bindings"):
            p = patch(target, return_value=self.registry)
            p.start()
            self.addCleanup(p.stop)

    def enroll(self, store=None, entry=ENTRY, cwd=None):
        store = store or self.a
        with patch.dict(os.environ, {"CODEX_THREAD_ID": entry}):
            with self.registry.claim(store, entry, cwd or self.entry_cwd):
                with store.transaction():
                    check_binding(store, entry)
                    bind_entry(store, entry)

    def payload(self, **changes):
        return {"hook_event_name": "UserPromptSubmit", "session_id": ENTRY,
                "cwd": str(self.entry_cwd), "turn_id": "actual-prompt-turn",
                "prompt": "PRIVATE_PROMPT_BODY_MUST_NOT_BE_ECHOED", **changes}

    def test_one_entry_for_canonical_cwd_even_with_another_state_directory(self):
        self.enroll()
        for entry in (OTHER, ENTRY):
            with self.assertRaisesRegex(ValueError, "cwd already"):
                self.enroll(self.b, entry)
        self.assertIsNone(self.b.main()["entry_thread_id"])
        self.assertEqual(self.a.main()["entry_thread_id"], ENTRY)
        self.assertEqual(len(self.registry.read()), 1)

    def test_unbind_removes_injection_and_rebind_reuses_project_pointer(self):
        self.enroll()
        self.assertIsNotNone(prompt_context(self.payload(), self.registry))
        with self.registry.release(self.a):
            with self.a.transaction():
                self.a.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        self.assertIsNone(prompt_context(self.payload(), self.registry))
        self.assertEqual(self.registry.state_directory(self.project), self.a.directory)
        self.enroll(self.b, OTHER)
        self.assertIsNone(prompt_context(self.payload(), self.registry))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER}):
            self.assertIsNotNone(prompt_context(self.payload(session_id=OTHER), self.registry))

    def test_prompt_ignores_forks_subagents_workers_and_unbound_sessions(self):
        self.enroll()
        before = self.registry.path.read_bytes()
        for extra in ({"session_id": OTHER}, {"agent_id": "child"}, {"agent_type": "worker"},
                      {"agent_id": None}, {"agent_type": ""}, {"cwd": str(self.project)},
                      {"hook_event_name": "SessionStart"}, {"turn_id": ""}):
            with self.subTest(extra=extra):
                self.assertIsNone(prompt_context(self.payload(**extra), self.registry))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER}):
            self.assertIsNone(prompt_context(self.payload(), self.registry))
        self.assertEqual(before, self.registry.path.read_bytes())

    def test_hook_only_returns_short_identity_and_paths_without_prompt_or_memory(self):
        self.enroll()
        (self.a.directory / "project-memory.json").write_text("PRIVATE_MEMORY_MUST_NOT_LOAD")
        before = "\n".join(self.a.db.iterdump())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(self.payload()))), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(hook_main(self.registry), 0)
        result = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertEqual(result["hookEventName"], "UserPromptSubmit")
        context = result["additionalContext"]
        self.assertIn(ENTRY, context)
        self.assertIn("agent-manager/SKILL.md", context)
        self.assertIn("actual-prompt-turn", context)
        self.assertLessEqual(len(context), 2400)
        self.assertNotIn("PRIVATE_PROMPT", context)
        self.assertNotIn("PRIVATE_MEMORY", context)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(before, "\n".join(self.a.db.iterdump()))

    def test_bad_stale_or_missing_state_never_injects_and_never_blocks_prompt(self):
        empty = Bindings(Path(self.temp.name) / "missing")
        self.assertIsNone(prompt_context(self.payload(), empty))
        self.assertFalse(empty.directory.exists())
        self.enroll()
        self.a.db.execute("UPDATE chats SET entry_thread_id=NULL WHERE role='main'")
        self.assertIsNone(prompt_context(self.payload(), self.registry))
        self.registry.path.write_text("broken registry")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(self.payload()))), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(hook_main(self.registry), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_reservation_without_committed_sqlite_cannot_activate_and_rollback_restores_index(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with self.registry.claim(self.a, ENTRY, self.entry_cwd):
                self.assertIsNone(prompt_context(self.payload(), self.registry))
                raise RuntimeError("fixture")
        self.assertEqual(self.registry.read(), {})
        self.assertIsNone(self.a.main()["entry_thread_id"])

    def test_stale_database_binding_cannot_bypass_registered_cwd_owner(self):
        self.enroll()
        bind_entry(self.b, OTHER)  # Historical unregistered database, not a new CLI enrollment.
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER}):
            context = session_context(self.b)
            self.assertFalse(context["active"])
            self.assertTrue(context["binding_conflict"])

    def test_same_native_entry_cwd_cannot_bind_another_session_for_another_project(self):
        self.enroll()
        second_project = Path(self.temp.name) / "second-project"
        second_project.mkdir()
        other = Store(Path(self.temp.name) / "state-other-project")
        try:
            other.setup(second_project)
            with self.assertRaisesRegex(ValueError, "entry cwd"):
                self.enroll(other, OTHER)
        finally:
            other.close()

    def test_registered_custom_state_is_used_by_default_context_query(self):
        self.enroll()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(cli_main(["--project", str(self.project), "context"]), 0)
        self.assertTrue(json.loads(stdout.getvalue())["active"])

    def test_concurrent_bindings_have_one_winner(self):
        # Separate native-caller fixtures exercise the real cross-process file lock.
        root = Path(self.temp.name) / "concurrent-registry"
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [context.Process(target=concurrent_bind,
            args=(str(self.project), str(store.directory), str(root), entry, output))
            for store, entry in ((self.a, ENTRY), (self.b, OTHER))]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted([output.get(timeout=2), output.get(timeout=2)]), ["bound", "conflict"])
        self.assertEqual(sum(bool(s.main()["entry_thread_id"]) for s in (self.a, self.b)), 1)
