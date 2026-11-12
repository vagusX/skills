"""Native names remain separate from work titles, dispatch and entry identity."""

import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.controller import Controller, managed_thread_name
from agent_manager_runtime.store import Store, encode, decode


class NamesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "example-project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "state")
        self.main = self.store.setup(self.project)
        self.store.db.execute("UPDATE chats SET entry_thread_id='actual-entry' WHERE role='main'")
        self.store.db.execute("""INSERT INTO chats(id,role,title,cwd,created_at)
            VALUES('work','worker','修复 页面布局',?,1)""", (str(self.project),))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def controller(self, failure=None):
        store = self.store

        class Client:
            def __init__(client):
                client.calls = []
                client.names = {}
                client.failure = failure
                client.process = SimpleNamespace(pid=os.getpid())
                client.started = "fixture process"

            async def call(client, method, params, **options):
                client.calls.append((method, params))
                if method == "thread/start":
                    row = store.one("SELECT * FROM chats WHERE binding='creating'")
                    return {"thread": {"id": "native-" + row["id"], "cwd": params["cwd"]}}
                if method == "thread/resume":
                    return {"thread": {"id": params["threadId"], "cwd": params["cwd"]}}
                if method == "thread/name/set":
                    if client.failure:
                        error, client.failure = client.failure, None
                        raise error
                    client.names[params["threadId"]] = params["name"]
                    return {}
                if method == "turn/start":
                    return {"turn": {"id": "accepted-turn"}}
                raise AssertionError("unexpected native method " + method)

        controller, client = Controller(store), Client()

        async def get_client(role):
            return client

        controller.client = get_client
        return controller, client

    def run_async(self, action):
        with redirect_stdout(io.StringIO()):
            return asyncio.run(action)

    def work(self):
        return self.store.one("SELECT * FROM chats WHERE id='work'")

    def test_new_managed_threads_have_prefix_without_changing_entry_or_original_titles(self):
        controller, client = self.controller()

        async def bind_both():
            await controller.bind(self.store.main())
            await controller.bind(self.work())

        self.run_async(bind_both())
        self.assertEqual(client.names, {
            "native-" + self.main["id"]: "agent-manager · 主控 · example-project",
            "native-work": "agent-manager · 修复 页面布局",
        })
        self.assertNotIn("actual-entry", client.names)
        self.assertEqual(self.store.main()["entry_thread_id"], "actual-entry")
        self.assertEqual(self.store.main()["title"], self.main["title"])
        self.assertEqual(self.work()["title"], "修复 页面布局")
        self.assertEqual(client.calls[0][0], "thread/start")
        self.assertEqual(client.calls[1][0], "thread/name/set")
        self.assertFalse(any(method == "turn/start" for method, _ in client.calls))

    def test_existing_thread_is_named_without_recreating_and_loaded_bind_is_idempotent(self):
        self.store.db.execute("UPDATE chats SET thread_id='existing',binding='linked' WHERE id='work'")
        controller, client = self.controller()

        async def bind_twice():
            await controller.bind(self.work())
            await controller.bind(self.work())

        self.run_async(bind_twice())
        self.assertEqual([method for method, _ in client.calls], ["thread/resume", "thread/name/set"])
        self.assertEqual(self.work()["thread_id"], "existing")
        self.assertEqual(client.names["existing"], "agent-manager · 修复 页面布局")

    def test_updated_work_title_is_applied_even_when_thread_is_already_loaded(self):
        controller, client = self.controller()
        self.run_async(controller.bind(self.work()))
        self.store.db.execute("UPDATE chats SET title='新的工作名称' WHERE id='work'")
        self.run_async(controller.bind(self.work()))
        self.assertEqual(client.names["native-work"], "agent-manager · 新的工作名称")
        self.assertEqual([method for method, _ in client.calls].count("thread/start"), 1)

    def test_name_failure_keeps_linked_identity_and_retries_only_metadata(self):
        controller, client = self.controller(RuntimeError("name API unavailable"))
        self.run_async(controller.bind(self.work()))
        self.assertEqual((self.work()["binding"], self.work()["thread_id"]), ("linked", "native-work"))
        self.run_async(controller.bind(self.work()))
        methods = [method for method, _ in client.calls]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("thread/resume"), 0)
        self.assertEqual(methods.count("thread/name/set"), 2)
        self.assertEqual(client.names["native-work"], "agent-manager · 修复 页面布局")

    def test_name_timeout_does_not_block_original_task_dispatch_or_change_its_text(self):
        job, _ = self.store.enqueue("task", "work", {"text": "Original authorized request\nDo not publish."})
        run = self.store.claim("work")
        controller, client = self.controller(asyncio.TimeoutError())
        self.run_async(controller.execute(run))
        actual = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual((actual["state"], actual["thread_id"], actual["turn_id"]),
                         ("running", "native-work", "accepted-turn"))
        turns = [params for method, params in client.calls if method == "turn/start"]
        envelope = decode(turns[0]["input"][0]["text"].split("\n", 1)[1])
        self.assertEqual(envelope["payload"], decode(job["payload"]))
        self.assertEqual(len(turns), 1)

    def test_prefix_is_not_duplicated_and_empty_title_uses_real_work_id(self):
        chat = self.work()
        chat["title"] = "agent-manager · 已有名称"
        self.assertEqual(managed_thread_name(chat), chat["title"])
        chat["title"] = " \n "
        self.assertEqual(managed_thread_name(chat), "agent-manager · work")


if __name__ == "__main__":
    unittest.main()
