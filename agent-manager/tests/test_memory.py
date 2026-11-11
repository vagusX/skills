"""Facts stay project-scoped, bounded and independent of live execution state."""

from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.memory import CONTEXT_CHARACTERS, Memory
from agent_manager_runtime.store import Store


def global_writer(directory, project, key):
    store = SimpleNamespace(directory=Path(directory) / ("state-" + key), main=lambda: {"cwd": project})
    return Memory(store, Path(directory) / "shared").put(key, "Confirmed preference " + key,
                                                       "Human explicitly applied it across projects", scope="global")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state")
        self.addCleanup(lambda: self.store.close())
        self.store.setup(self.project)
        self.memory = Memory(self.store, self.root / "shared")

    def test_read_context_and_missing_forget_do_not_create_memory_or_global_directory(self):
        before = sorted(self.root.rglob("*"))
        changes = self.store.db.total_changes
        read = self.memory.read()
        self.assertTrue(read["missing"])
        self.assertEqual(read["entries"], [])
        context = self.memory.context()
        self.assertEqual(context["missing"], ["project", "global"])
        self.assertEqual(context["project_memory"], [])
        self.assertFalse(self.memory.forget("unknown")["changed"])
        self.assertEqual(sorted(self.root.rglob("*")), before)
        self.assertEqual(self.store.db.total_changes, changes)

    def test_put_replace_forget_reopen_keep_source_and_do_not_change_scheduler(self):
        before = "\n".join(self.store.db.iterdump())
        first = self.memory.put("test-command", "Use the verified test command", "README.md:12")
        self.assertTrue(first["changed"])
        duplicate = self.memory.put("test-command", "Use the verified test command", "README.md:12")
        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["revision"], first["revision"])
        replacement = self.memory.put("test-command", "Use the corrected test command", "Human correction in current chat")
        self.assertNotEqual(replacement["revision"], first["revision"])
        self.store.close()
        self.store = Store(self.root / "state")
        memory = Memory(self.store, self.root / "shared")
        entries = memory.read()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Use the corrected test command")
        self.assertEqual(entries[0]["source"], "Human correction in current chat")
        self.assertGreater(entries[0]["updated_at"], 0)
        self.assertEqual("\n".join(self.store.db.iterdump()), before)
        self.assertTrue(memory.forget("test-command")["changed"])
        self.assertEqual(memory.read()["entries"], [])

    def test_project_is_manager_scope_not_worker_cwd_and_global_is_shared(self):
        self.memory.put("architecture", "Project one uses its own protocol", "Project one design")
        self.memory.put("language", "Reply in Chinese", "Explicit cross-project preference", scope="global")
        self.store.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES('worker','worker','worker',?,1)",
                              (str(self.root / "another-worktree"),))
        self.assertEqual(self.memory.read()["project"], str(self.project.resolve()))
        second_project = self.root / "another-project"
        second_project.mkdir()
        second_store = Store(self.root / "second-state")
        self.addCleanup(second_store.close)
        second_store.setup(second_project)
        second = Memory(second_store, self.root / "shared")
        self.assertEqual(second.read()["entries"], [])
        self.assertEqual(second.read("global")["entries"], self.memory.read("global")["entries"])
        second.put("architecture", "Project two has a different protocol", "Project two design")
        self.assertEqual(self.memory.read()["entries"][0]["text"], "Project one uses its own protocol")

    def test_global_concurrent_writers_merge_without_overwriting_other_projects(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(global_writer, str(self.root), str(self.project), "preference-" + str(i))
                       for i in range(12)]
            receipts = [future.result(timeout=20) for future in futures]
        self.assertTrue(all(receipt["changed"] for receipt in receipts))
        read = self.memory.read("global")
        self.assertEqual({entry["key"] for entry in read["entries"]}, {"preference-" + str(i) for i in range(12)})
        self.assertFalse((self.store.directory / "project-memory.json").exists())

    def test_failed_atomic_replace_keeps_previous_facts_and_cleans_only_own_tempfile(self):
        self.memory.put("design", "Confirmed original fact", "Human statement")
        path = self.store.directory / "project-memory.json"
        original = path.read_bytes()
        with patch("agent_manager_runtime.memory.os.replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                self.memory.put("design", "Uncommitted replacement", "Later statement")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob(".memory-*.tmp")))
        self.assertEqual(self.memory.read()["entries"][0]["text"], "Confirmed original fact")

    def test_files_are_private_and_bad_input_does_not_create_files(self):
        before = sorted(self.root.rglob("*"))
        invalid = [
            ("../escape", "text", "source", "project"),
            ("key", "", "source", "project"),
            ("key", "text", "", "project"),
            ("key", "x" * 2001, "source", "project"),
            ("key", "text", "x" * 501, "project"),
            ("key", "text", "source", "another-project"),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.memory.put(*values)
        self.assertEqual(sorted(self.root.rglob("*")), before)
        self.memory.put("design", "Confirmed fact", "Human statement")
        self.memory.put("language", "Chinese", "Explicit global instruction", scope="global")
        for path in (self.store.directory / "project-memory.json", self.root / "shared/global-memory.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_context_is_bounded_project_first_and_exposes_omissions(self):
        for i in range(12):
            self.memory.put("project-" + str(i), "Project-specific fact " + "p" * 850, "verified project evidence")
            self.memory.put("global-" + str(i), "Global preference " + "g" * 850,
                            "Human cross-project instruction", scope="global")
        context = self.memory.context()
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))), CONTEXT_CHARACTERS)
        self.assertTrue(context["project_memory"])
        self.assertGreater(context["truncated"]["project"], 0)
        self.assertGreater(context["truncated"]["global"], 0)
        project_count = len(context["project_memory"])
        self.assertEqual(project_count + context["truncated"]["project"], 12)
        self.assertEqual(len(context["global_memory"]) + context["truncated"]["global"], 12)
        revision = context["revision"]
        self.memory.put("new-project-fact", "Latest confirmed decision", "Human project decision")
        updated = self.memory.context()
        self.assertNotEqual(updated["revision"], revision)
        self.assertEqual(updated["project_memory"][0]["key"], "new-project-fact")

    def test_bad_existing_file_is_not_echoed_overwritten_or_used_as_context(self):
        path = self.store.directory / "project-memory.json"
        secret = b"malformed PRIVATE_TOKEN_DO_NOT_ECHO"
        path.write_bytes(secret)
        with self.assertRaises(RuntimeError) as raised:
            self.memory.read()
        self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))
        with self.assertRaises(RuntimeError):
            self.memory.put("key", "Replacement", "Human")
        self.assertEqual(path.read_bytes(), secret)
        context = self.memory.context()
        self.assertEqual(context["unavailable"], ["project"])
        self.assertEqual(context["project_memory"], [])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(context))

    def test_memory_file_cannot_be_borrowed_from_another_project_or_symlink(self):
        self.memory.put("design", "Own fact", "Source")
        path = self.store.directory / "project-memory.json"
        value = json.loads(path.read_text())
        value["project"] = str(self.root / "another-project")
        path.write_text(json.dumps(value))
        with self.assertRaises(RuntimeError):
            self.memory.read()
        path.unlink()
        outside = self.root / "outside.json"
        outside.write_text("preserve outside content")
        path.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.memory.put("design", "Replacement", "Source")
        self.assertEqual(outside.read_text(), "preserve outside content")


if __name__ == "__main__":
    unittest.main()
