"""Hook installation stays explicit, additive, private, and independent of trust."""

import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_manager_runtime.hooks import MAX_HOOKS_BYTES, hook_plan, install_hook


class HookInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex home"
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.home / "hooks.json"

    def write(self, config):
        self.home.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return self.path.read_bytes()

    def test_plan_has_only_one_short_sync_event_and_creates_nothing(self):
        result = hook_plan()
        self.assertFalse(self.home.exists())
        self.assertEqual(result["config_path"], str(self.path.resolve()))
        self.assertEqual(result["event_name"], "UserPromptSubmit")
        command = shlex.split(result["command"])
        self.assertEqual(command, [str(Path(sys.executable).resolve()), str(SCRIPTS / "agent_manager_prompt.py")])
        self.assertTrue(all(Path(part).is_absolute() for part in command))
        self.assertEqual(result["group"], {"hooks": [{"type": "command", "command": result["command"], "timeout": 3}]})
        result["group"]["hooks"][0]["timeout"] = 99
        self.assertEqual(hook_plan()["group"]["hooks"][0]["timeout"], 3)

    def test_plan_shell_quotes_absolute_paths_without_running_a_shell(self):
        executable = self.home / "python with spaces"
        module = self.home / "skill's files" / "scripts" / "agent_manager_runtime" / "hooks.py"
        with patch("agent_manager_runtime.hooks.sys.executable", str(executable)), \
                patch("agent_manager_runtime.hooks.__file__", str(module)):
            result = hook_plan()
        self.assertEqual(shlex.split(result["command"]), [
            str(executable.resolve()), str(module.resolve().parents[1] / "agent_manager_prompt.py")])
        self.assertFalse(self.home.exists())

    def test_new_install_is_private_and_does_not_claim_native_trust(self):
        result = install_hook()
        self.assertTrue(result["changed"])
        self.assertFalse(result["reused"])
        self.assertTrue(result["requires_native_review"])
        self.assertIsNone(result["existing_settings_match"])
        self.assertIn("/hooks", result["review_message"])
        self.assertIn("does not confirm", result["review_message"])
        self.assertEqual(json.loads(self.path.read_text()), {"hooks": {"UserPromptSubmit": [hook_plan()["group"]]}})
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / ".agent-manager-hooks.lock").stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".agent-manager-hooks.lock", "hooks.json"])

    def test_repeat_reuses_command_without_rewriting_or_duplicating(self):
        install_hook()
        config = json.loads(self.path.read_text())
        self.path.write_text(json.dumps(config, separators=(",", ":")) + "\n", encoding="utf-8")
        before = self.path.read_bytes()
        with patch("agent_manager_runtime.hooks.os.replace", side_effect=AssertionError("must not rewrite")):
            result = install_hook()
        self.assertFalse(result["changed"])
        self.assertTrue(result["reused"])
        self.assertTrue(result["existing_settings_match"])
        self.assertTrue(result["requires_native_review"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_merge_preserves_all_fields_order_handlers_and_trust_records(self):
        first = {"matcher": "ignored-existing-value", "hooks": [
            {"type": "command", "command": "first", "timeout": 1},
            {"type": "command", "command": "second", "async": True}]}
        second = {"hooks": [{"type": "command", "command": "third"}], "custom": {"keep": True}}
        config = {"description": "已有配置", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "stop"}]}],
            "UserPromptSubmit": [first, second], "SessionStart": []},
            "hook_states": {"fixture:key": {"trusted_hash": "fixture-hash", "enabled": False}},
            "unrelated": [1, {"keep": "yes"}]}
        self.write(config)
        settings = self.home / "config.toml"
        settings.write_text('[hook_states."fixture:key"]\ntrusted_hash = "fixture-hash"\n', encoding="utf-8")
        before_settings = settings.read_bytes()
        install_hook()
        after = json.loads(self.path.read_text())
        self.assertEqual(list(after), list(config))
        self.assertEqual(list(after["hooks"]), list(config["hooks"]))
        self.assertEqual(after["hooks"]["UserPromptSubmit"], [first, second, hook_plan()["group"]])
        after["hooks"]["UserPromptSubmit"].pop()
        self.assertEqual(after, config)
        self.assertEqual(settings.read_bytes(), before_settings)

    def test_existing_command_with_other_settings_is_not_overridden(self):
        command = hook_plan()["command"]
        existing = {"matcher": "custom", "hooks": [
            {"type": "command", "command": command, "timeout": 42, "statusMessage": "existing"},
            {"type": "command", "command": "other"}]}
        before = self.write({"hooks": {"UserPromptSubmit": [existing]}})
        result = install_hook()
        self.assertFalse(result["changed"])
        self.assertTrue(result["reused"])
        self.assertFalse(result["existing_settings_match"])
        self.assertIn("different settings", result["review_message"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_same_command_in_other_event_does_not_replace_prompt_registration(self):
        group = hook_plan()["group"]
        self.write({"hooks": {"SessionStart": [group]}})
        self.assertTrue(install_hook()["changed"])
        self.assertEqual(json.loads(self.path.read_text())["hooks"], {
            "SessionStart": [group], "UserPromptSubmit": [group]})

    def test_invalid_json_or_structure_is_never_overwritten(self):
        self.home.mkdir()
        for content in ['{broken', '[]', '{"hooks":[]}', '{"hooks":{"UserPromptSubmit":{}}}',
                        '{"hooks":{"UserPromptSubmit":[{}]}}',
                        '{"hooks":{"UserPromptSubmit":[{"hooks":["bad"]}]}}',
                        '{"hooks":{},"hooks":{}}']:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    install_hook()
                self.assertEqual(self.path.read_text(), content)

    def test_oversize_read_or_merged_result_is_never_written(self):
        before = self.write({"description": "x" * MAX_HOOKS_BYTES})
        with self.assertRaisesRegex(ValueError, "limit"):
            install_hook()
        self.assertEqual(self.path.read_bytes(), before)
        before = self.write({"hooks": {}})
        with patch("agent_manager_runtime.hooks.MAX_HOOKS_BYTES", len(before) + 1):
            with self.assertRaisesRegex(ValueError, "merged"):
                install_hook()
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary(self):
        before = self.write({"description": "preserved", "hooks": {}})
        with patch("agent_manager_runtime.hooks.os.replace", side_effect=OSError("fixture failure")):
            with self.assertRaisesRegex(OSError, "fixture failure"):
                install_hook()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.home.glob(".agent-manager-hooks-*.tmp")))

    def test_symlink_is_not_replaced_or_written_through(self):
        self.home.mkdir()
        target = Path(self.temp.name) / "elsewhere.json"
        target.write_text('{"unchanged":true}', encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            install_hook()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), '{"unchanged":true}')

    def test_parallel_installers_under_one_lock_append_exactly_once(self):
        self.home.mkdir()
        env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        code = ('import json; from agent_manager_runtime.hooks import install_hook; '
                'print("ready",flush=True); print(json.dumps(install_hook()),flush=True)')
        processes = []
        with (self.home / ".agent-manager-hooks.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                for _ in range(6):
                    processes.append(subprocess.Popen([sys.executable, "-c", code], env=env,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                for process in processes:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                self.assertFalse(self.path.exists())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            results = []
            for process in processes:
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                results.append(json.loads(output))
            self.assertEqual(sum(result["changed"] for result in results), 1)
            self.assertEqual(json.loads(self.path.read_text())["hooks"]["UserPromptSubmit"], [hook_plan()["group"]])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
