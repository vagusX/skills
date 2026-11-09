"""A JSON-RPC client for the user's native `codex app-server`."""

import asyncio
import json
import shutil
import subprocess

from .store import encode


class RpcError(RuntimeError):
    pass


def process_start(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def process_alive(pid, started):
    if not pid or not started:
        return False
    return process_start(pid) == started


class Codex:
    def __init__(self, role, directory, cwd, on_request, on_event):
        self.role, self.directory, self.cwd = role, directory, cwd
        self.on_request, self.on_event = on_request, on_event
        self.process = None
        self.pending, self.sequence = {}, 0
        self.write_lock = asyncio.Lock()
        self.events = asyncio.Queue()
        self.handlers = set()
        self.reader = self.consumer = None
        self.stderr = None
        self.started = None
        self.closed = False

    async def start(self):
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("codex is not on PATH")
        self.stderr = (self.directory / (self.role + ".stderr.log")).open("ab", buffering=0)
        args = [binary, "app-server", "--listen", "stdio://"]
        self.process = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self.stderr, limit=8 * 1024 * 1024)
        self.started = process_start(self.process.pid)
        self.reader = asyncio.create_task(self._read())
        self.consumer = asyncio.create_task(self._consume())
        result = await self.call("initialize", {
            "clientInfo": {"name": "agent_manager", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized", "params": {}})
        return result

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise RpcError("Codex app-server is not running")
        async with self.write_lock:
            self.process.stdin.write((encode(message) + "\n").encode())
            await self.process.stdin.drain()

    async def call(self, method, params, timeout=60):
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)

    async def respond(self, request_id, result):
        await self.send({"id": request_id, "result": result})

    async def _read(self):
        failure = "Codex app-server closed its output"
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        task = asyncio.create_task(self._request(message))
                        self.handlers.add(task)
                        task.add_done_callback(self.handlers.discard)
                    else:
                        await self.events.put(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(encode(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            return
        except Exception as error:
            failure = str(error)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RpcError(failure))
            if not self.closed:
                await self.events.put({"method": "agent-manager/disconnected", "params": {"error": failure}})

    async def _request(self, message):
        try:
            await self.on_request(self, message)
        except Exception as error:
            # A rejected business call is a tool failure, never an approval.
            if message["method"] == "item/tool/call":
                try:
                    await self.respond(message["id"], {
                        "contentItems": [{"type": "inputText", "text": encode({"error": str(error)})}],
                        "success": False})
                except (RpcError, BrokenPipeError, ConnectionResetError):
                    pass
            else:
                await self.events.put({"method": "agent-manager/request-error", "params": {"error": str(error)}})

    async def _consume(self):
        while True:
            message = await self.events.get()
            try:
                await self.on_event(self, message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.on_event(self, {"method": "agent-manager/disconnected", "params": {"error": "event processing failed: " + str(error)}})
                return
            finally:
                self.events.task_done()

    async def history(self, thread_id):
        turns, cursor = [], None
        while True:
            params = {"threadId": thread_id, "limit": 100, "itemsView": "full", "sortDirection": "asc"}
            if cursor:
                params["cursor"] = cursor
            response = await self.call("thread/turns/list", params)
            turns.extend(response.get("data", []))
            cursor = response.get("nextCursor")
            if not cursor:
                break
        return turns

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        for task in [self.reader, self.consumer, *self.handlers]:
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in [self.reader, self.consumer, *self.handlers] if t),
                             return_exceptions=True)
        if self.stderr:
            self.stderr.close()
