"""Loopback-only human interface, independent of the native session owner."""

import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import webbrowser

from .approvals import pending_questions, queue_answer
from .activation import background_environment
from .codex import process_alive, process_start
from .store import Store, encode


ASSETS = Path(__file__).resolve().parents[2] / "assets"


def panel_record(store):
    try:
        record = json.loads((store.directory / "panel.json").read_text())
        if not isinstance(record["port"], int) or not 0 < record["port"] < 65536 or record["url"] != f"http://127.0.0.1:{record['port']}/":
            return None
        if not isinstance(record["token"], str) or not record["token"].isascii() or len(record["token"]) < 32:
            return None
        return record if process_alive(record["pid"], record["started"]) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def public_record(record):
    return {k: record[k] for k in ("pid", "started", "port", "url")} if record else None


def panel_url(record):
    return record["url"] + "#" + record["token"]


def open_panel(record):
    url = panel_url(record)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", url], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not webbrowser.open(url):
        raise RuntimeError("cannot open a browser; run panel to get its local URL")


def notify(record, count):
    # Notification text never includes commands, answers, credentials, or issue content.
    message = f"有 {count} 项审批或问题等待你的回答。需要时请手动打开本地面板处理。"
    notifier = shutil.which("terminal-notifier")
    if sys.platform == "darwin" and notifier:
        subprocess.run([notifier, "-title", "Agent Manager", "-message", message,
                        "-open", panel_url(record)], timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "darwin":
        subprocess.run(["/usr/bin/osascript", "-e", "display notification " +
                        json.dumps(message, ensure_ascii=False) + ' with title "Agent Manager"'],
                       timeout=10, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "Agent Manager", message], timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_panel(store, project, *, open_now=False):
    record = panel_record(store)
    if not record:
        launcher = Path(__file__).resolve().parents[1] / "agent_manager.py"
        with (store.directory / "panel.log").open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, str(launcher), "--project", project,
                "--state-dir", str(store.directory), "panel-run"], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, env=background_environment())
        store.panel_child = child
        for _ in range(100):
            record = panel_record(store)
            if record:
                break
            if child.poll() is not None:
                raise RuntimeError("local panel failed to start; read panel.log")
            time.sleep(.05)
        else:
            raise RuntimeError("local panel startup not confirmed; inspect panel.log before retrying")
    if open_now:
        try:
            open_panel(record)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            record = {**record, "open_error": "无法自动打开浏览器，请复制本地访问地址。"}
    return record


def stop_panel(store):
    record = panel_record(store)
    if not record:
        return {"running": False}
    os.kill(record["pid"], signal.SIGTERM)
    for _ in range(100):
        child = getattr(store, "panel_child", None)
        if child and child.pid == record["pid"]:
            child.poll()
        if not process_alive(record["pid"], record["started"]):
            return {"stopped": True, "pid": record["pid"]}
        time.sleep(.05)
    return {"stop_requested": True, "confirmed_stopped": False, "pid": record["pid"]}


def notify_pending(store, record, notified):
    pending = {q["id"] for q in pending_questions(store) if q["status"] == "awaiting_confirmation"}
    if store.main()["entry_thread_id"]:
        return pending
    if pending and not notified:
        try:
            notify(record, len(pending))
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(encode({"event": "panel_notification_failed", "operation": "desktop_notification", "error_type": type(error).__name__}), flush=True)
    return pending


class PanelServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, directory):
        self.directory = directory
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), PanelHandler)
        self.timeout = .5
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No request, URL token, or human answer logging.

    def respond(self, status, value, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else encode(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, *, mutate=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.respond(403, {"error": "unexpected host"})
            return False
        if mutate and self.headers.get("Origin") != self.server.origin:
            self.respond(403, {"error": "unexpected origin"})
            return False
        expected = "Bearer " + self.server.token
        if not secrets.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self.respond(403, {"error": "请重新运行 panel 命令打开面板。"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        assets = {"/": ("panel.html", "text/html; charset=utf-8"),
                  "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
                  "/panel.css": ("panel.css", "text/css; charset=utf-8")}
        if path in assets:
            if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
                self.respond(403, {"error": "unexpected host"})
                return
            name, mime = assets[path]
            self.respond(200, (ASSETS / name).read_bytes(), mime)
            return
        if path != "/api/questions":
            self.respond(404, {"error": "not found"})
            return
        if not self.authorized():
            return
        store = Store(self.server.directory)
        try:
            self.respond(200, {"project": store.main()["cwd"], "questions": pending_questions(store)})
        finally:
            store.close()

    def do_POST(self):
        if urlsplit(self.path).path != "/api/answer":
            self.respond(404, {"error": "not found"})
            return
        if not self.authorized(mutate=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or length < 1 or length > 65536:
                self.respond(400, {"error": "expected bounded JSON input"})
                return
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict) or set(value) != {"question_id", "response"} or not isinstance(value["question_id"], str):
                raise ValueError("expected question_id and response")
            store = Store(self.server.directory)
            try:
                result = queue_answer(store, value["question_id"], value["response"])
            finally:
                store.close()
            self.respond(200, result)
        except (ValueError, KeyError, TypeError) as error:
            self.respond(409, {"error": str(error)})


def serve_panel(store):
    with (store.directory / "panel.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("a local panel already owns this state directory") from None
        server = PanelServer(store.directory)
        record = {"pid": os.getpid(), "started": process_start(os.getpid()), "port": server.server_port,
                  "url": server.origin + "/", "token": server.token}
        metadata = store.directory / "panel.json"
        with os.fdopen(os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(encode(record))
        # An outstanding batch gets one notification; opening a browser is explicit.
        notified = set()
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        next_check = 0
        try:
            while not stopping:
                server.handle_request()
                if time.monotonic() < next_check:
                    continue
                next_check = time.monotonic() + 1
                updated = notify_pending(store, record, notified)
                notified = updated
        finally:
            server.server_close()
            metadata.unlink(missing_ok=True)
