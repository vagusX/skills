"""The four-table scheduling store. Conversation history stays in Codex."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value, default=None):
    return json.loads(value) if value is not None else default


def identifier():
    return str(uuid.uuid4())


def data_directory(project):
    base = (Path.home() / "Library/Application Support" if sys.platform == "darwin"
            else Path.home() / ".local/state")
    key = hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:20]
    legacy = base / "super-agent" / key
    if (legacy / "state.sqlite3").is_file():
        return legacy
    return base / "agent-manager" / key


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "state.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.resources = {}
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, config TEXT NOT NULL,
            cursor TEXT, last_poll REAL, next_poll REAL NOT NULL DEFAULT 0,
            error TEXT, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('main','worker')),
            title TEXT NOT NULL, cwd TEXT NOT NULL, thread_id TEXT UNIQUE,
            binding TEXT NOT NULL DEFAULT 'unbound', source_id TEXT REFERENCES sources(id),
            source_key TEXT, request_job_id TEXT, hold TEXT, report_state TEXT,
            report_ref TEXT, closed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_main ON chats(role) WHERE role='main';
        CREATE UNIQUE INDEX IF NOT EXISTS one_source_owner ON chats(source_id,source_key)
            WHERE source_id IS NOT NULL AND source_key IS NOT NULL AND closed=0;
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, chat_id TEXT REFERENCES chats(id),
            source_id TEXT REFERENCES sources(id), source_key TEXT,
            request_key TEXT UNIQUE, request_hash TEXT, status TEXT NOT NULL DEFAULT 'pending',
            payload TEXT, native_ref TEXT, result_ref TEXT, parent_id TEXT REFERENCES jobs(id),
            created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_source_event ON jobs(source_id,source_key)
            WHERE source_id IS NOT NULL AND source_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id),
            job_id TEXT NOT NULL REFERENCES jobs(id), thread_id TEXT, turn_id TEXT,
            state TEXT NOT NULL CHECK(state IN ('prepared','running','exited','unknown')),
            outcome TEXT, server_pid INTEGER, server_start TEXT,
            started_at REAL NOT NULL, ended_at REAL, error TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_run ON runs(chat_id)
            WHERE state IN ('prepared','running','unknown');
        """)
        with self.transaction():
            if "entry_thread_id" not in {r["name"] for r in self.all("PRAGMA table_info(chats)")}:
                self.db.execute("ALTER TABLE chats ADD COLUMN entry_thread_id TEXT")
        os.chmod(self.directory / "state.sqlite3", 0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def setup(self, cwd):
        cwd = str(Path(cwd).resolve(strict=True))
        with self.transaction():
            main = self.one("SELECT * FROM chats WHERE role='main'")
            if main:
                if main["cwd"] != cwd:
                    raise ValueError("state directory is already bound to a different project")
                return main
            key = identifier()
            self.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES(?,?,?,?,?)",
                            (key, "main", "Agent Manager", cwd, time.time()))
            return self.one("SELECT * FROM chats WHERE id=?", (key,))

    def main(self):
        row = self.one("SELECT * FROM chats WHERE role='main'")
        if not row:
            raise ValueError("run setup first")
        return row

    def enqueue(self, kind, chat_id, payload, *, source_id=None, source_key=None,
                request_key=None, parent_id=None, status="pending", result=None, digest=None):
        key, now = identifier(), time.time()
        try:
            self.db.execute("""INSERT INTO jobs(id,kind,chat_id,source_id,source_key,
                request_key,request_hash,status,payload,result_ref,parent_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, kind, chat_id, source_id, source_key, request_key, digest, status,
                 encode(payload) if payload is not None else None,
                 encode(result) if result is not None else None, parent_id, now, now))
        except sqlite3.IntegrityError:
            row = (self.one("SELECT * FROM jobs WHERE request_key=?", (request_key,))
                   if request_key else self.one(
                       "SELECT * FROM jobs WHERE source_id=? AND source_key=?", (source_id, source_key)))
            if row is None:
                raise
            return row, False
        return self.one("SELECT * FROM jobs WHERE id=?", (key,)), True

    def submit(self, text, *, kind="task", chat_id=None):
        if not text.strip():
            raise ValueError("empty input")
        chat_id = chat_id or self.main()["id"]
        with self.transaction():
            if not self.one("SELECT id FROM chats WHERE id=?", (chat_id,)):
                raise ValueError("unknown chat")
            return self.enqueue(kind, chat_id, {"text": text})[0]

    def claim(self, chat_id):
        with self.transaction():
            chat = self.one("SELECT * FROM chats WHERE id=?", (chat_id,))
            if not chat or chat["closed"] or chat["hold"]:
                return None
            if self.one("SELECT id FROM runs WHERE chat_id=? AND state!='exited'", (chat_id,)):
                return None
            jobs = self.all("""SELECT * FROM jobs WHERE chat_id=? AND status='pending'
                AND kind IN ('task','chat','followup','wake','recover','compact')
                ORDER BY (kind='compact') DESC,created_at,id""", (chat_id,))
            job = None
            for candidate in jobs:
                from .pull_requests import review_gate
                gate = review_gate(self, candidate)
                if gate == "skipped":
                    self.db.execute("UPDATE jobs SET status='done',result_ref=?,error=? WHERE id=?",
                        (encode({"pr_review": {"outcome": "skipped"}}), "PR source disabled or original work closed", candidate["id"]))
                elif gate != "deferred":
                    job = candidate
                    break
            if not job:
                return None
            # A shared checkout is a shared write resource, even with different threads.
            if chat["role"] == "worker":
                for active in self.all("""SELECT c.cwd FROM runs r JOIN chats c ON c.id=r.chat_id
                        WHERE r.state!='exited' AND c.role='worker'"""):
                    a, b = self.write_resource(active["cwd"]), self.write_resource(chat["cwd"])
                    if a == b or a in b.parents or b in a.parents:
                        return None
            run_id = identifier()
            self.db.execute("UPDATE jobs SET status='inflight',updated_at=? WHERE id=?",
                            (time.time(), job["id"]))
            self.db.execute("""INSERT INTO runs(id,chat_id,job_id,thread_id,state,started_at)
                VALUES(?,?,?,?,?,?)""", (run_id, chat_id, job["id"], chat["thread_id"], "prepared", time.time()))
            return self.one("SELECT * FROM runs WHERE id=?", (run_id,))

    def write_resource(self, cwd):
        if cwd not in self.resources:
            result = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=5)
            self.resources[cwd] = Path(result.stdout.strip() if result.returncode == 0 else cwd).resolve()
        return self.resources[cwd]

    def finish(self, run_id, outcome, error=None):
        with self.transaction():
            run = self.one("SELECT * FROM runs WHERE id=?", (run_id,))
            if not run or run["state"] == "exited":
                return
            self.db.execute("UPDATE runs SET state='exited',outcome=?,ended_at=?,error=? WHERE id=?",
                            (outcome, time.time(), error, run_id))
            self.db.execute("UPDATE jobs SET status='done',updated_at=?,error=? WHERE id=?",
                            (time.time(), error, run["job_id"]))

    def status(self):
        return {"state_directory": str(self.directory),
                "chats": self.all("SELECT * FROM chats ORDER BY created_at"),
                "runs": self.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 40"),
                "jobs": self.all("SELECT id,kind,chat_id,status,result_ref,native_ref,error,created_at FROM jobs ORDER BY created_at DESC LIMIT 80"),
                "sources": self.all("SELECT * FROM sources ORDER BY id")}

    def dispatch_receipt(self, job_id):
        """Read only: acknowledge this dispatch, never wait for its work to finish."""
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("unknown job")
        if job["kind"] not in {"task", "followup", "recover"}:
            raise ValueError("wait-start is for task dispatch or followup, not conversation or results")
        owner = self.one("SELECT * FROM chats WHERE id=?", (job["chat_id"],))
        result = {"job_id": job_id, "job_status": job["status"],
                  "main_thread_id": self.main()["thread_id"],
                  "phase": "queued", "work_id": None, "thread_id": None,
                  "turn_id": None, "run_id": None, "error": job["error"]}
        work_id = owner["id"] if owner and owner["role"] == "worker" else decode(job["result_ref"], {}).get("work_id")
        work = self.one("SELECT * FROM chats WHERE id=? AND role='worker'", (work_id,)) if work_id else None
        if work is None:
            # Routing to an existing busy worker is already persisted before its new run starts.
            work = self.one("""SELECT c.* FROM chats c JOIN jobs j ON j.chat_id=c.id
                WHERE c.role='worker' AND j.parent_id=? AND j.kind IN ('task','followup','recover')
                ORDER BY j.created_at,j.id LIMIT 1""", (job_id,))
        # A new followup must not be acknowledged using the previous worker turn.
        run = self.one("""SELECT r.*,j.id AS delivery_job_id FROM runs r
            JOIN jobs j ON j.id=r.job_id JOIN chats c ON c.id=r.chat_id
            WHERE c.role='worker' AND (j.id=? OR j.parent_id=?)
            ORDER BY r.started_at DESC,r.id DESC LIMIT 1""", (job_id, job_id))
        if run and work is None:
            work = self.one("SELECT * FROM chats WHERE id=?", (run["chat_id"],))
        if work:
            result.update(work_id=work["id"], thread_id=work["thread_id"], cwd=work["cwd"],
                          phase="dispatching", report_state=work["report_state"])
            if work["binding"] == "unknown":
                result.update(phase="unknown", error="worker creation result is unknown; do not resubmit")
        if run:
            result.update(run_id=run["id"], turn_id=run["turn_id"], run_state=run["state"],
                          native_outcome=run["outcome"])
            if run["state"] == "unknown":
                result.update(phase="unknown", error=run["error"])
            elif run["turn_id"] and result["thread_id"]:
                result["phase"] = "started"
                if run["state"] == "exited" and run["outcome"] != "completed":
                    result.update(phase="attention", error=run["error"] or f"worker turn ended: {run['outcome']}")
                report_ref = decode(work["report_ref"], {})
                if (report_ref.get("turn_id") == run["turn_id"] and work["report_state"] in {"blocked", "failed", "dropped"}):
                    result["phase"] = "attention"
                questions = self.all("SELECT id FROM jobs WHERE kind='question' AND parent_id=? AND status!='done'",
                                     (run["delivery_job_id"],))
                if questions:
                    result.update(phase="attention", question_ids=[q["id"] for q in questions])
        elif work:
            previous = self.one("SELECT state,turn_id FROM runs WHERE chat_id=? AND state!='exited' ORDER BY started_at DESC LIMIT 1", (work["id"],))
            if previous:
                if previous["state"] == "unknown":
                    result.update(phase="unknown", error="previous worker execution is unresolved; followup remains queued")
                else:
                    result.update(phase="queued", waiting_for_previous_turn=True,
                                  active_turn_id=previous["turn_id"])
        elif not work:
            dispatch = self.one("SELECT state,error FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (job_id,))
            if dispatch and dispatch["state"] == "unknown":
                result.update(phase="unknown", error=dispatch["error"])
            elif job["status"] == "done":
                result.update(phase="attention", error=job["error"] or "dispatch turn ended without a worker; read the main thread for its question or result")
        if work and work["hold"]:
            result.update(phase="attention", hold=decode(work["hold"]))
        return result
