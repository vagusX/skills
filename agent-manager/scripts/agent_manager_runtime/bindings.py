"""One registered entry per canonical project cwd, shared across state dirs."""

from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

from .store import data_directory, encode


class Bindings:
    def __init__(self, directory=None):
        base = (Path.home() / "Library/Application Support" if sys.platform == "darwin"
                else Path.home() / ".local/state")
        self.directory = Path(directory) if directory is not None else base / "agent-manager"
        self.path = self.directory / "entry-bindings.json"

    def read(self):
        try:
            if self.path.is_symlink():
                raise ValueError("symlink")
            with self.path.open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
            if len(raw) > 1024 * 1024 or set(data) != {"version", "bindings"} or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["bindings"], dict):
                raise ValueError("schema")
            for cwd, record in data["bindings"].items():
                if not isinstance(cwd, str) or not Path(cwd).is_absolute() or set(record) != {"state_directory", "entry_thread_id", "entry_cwd"}:
                    raise ValueError("record")
                if any(not isinstance(record[k], str) or not Path(record[k]).is_absolute() for k in ("state_directory", "entry_cwd")):
                    raise ValueError("path")
                if record["entry_thread_id"] is not None:
                    uuid.UUID(record["entry_thread_id"])
            return data["bindings"]
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise RuntimeError("invalid entry binding index; preserve it and reconcile before binding") from error

    @staticmethod
    def metadata(directory):
        path = Path(directory).expanduser().resolve() / "state.sqlite3"
        if not path.is_file():
            return None
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.row_factory = sqlite3.Row
                main = db.execute("SELECT * FROM chats WHERE role='main'").fetchone()
                if main:
                    result = dict(main)
                    entry = result.get("entry_thread_id")
                    result["entry_is_managed"] = bool(entry and db.execute("SELECT 1 FROM chats WHERE thread_id=?", (entry,)).fetchone())
                    return result
                return None
        except sqlite3.Error as error:
            raise RuntimeError("cannot verify registered entry binding") from error

    def live(self, project, record):
        if not record["entry_thread_id"] or not Path(project).is_dir() or not Path(record["entry_cwd"]).is_dir():
            return False
        main = self.metadata(record["state_directory"])
        return bool(main and main.get("cwd") == project and main.get("entry_thread_id") == record["entry_thread_id"]
                    and not main.get("entry_is_managed"))

    def state_directory(self, project):
        project = str(Path(project).resolve())
        record = self.read().get(project)
        if record:
            main = self.metadata(record["state_directory"])
            if main and main.get("cwd") == project:
                return Path(record["state_directory"])
        return None

    def allows(self, project, directory, entry):
        record = self.read().get(project)
        # Legacy bindings remain readable until their first explicit enrollment.
        return record is None or (record["state_directory"] == str(Path(directory).resolve())
                                  and record["entry_thread_id"] == entry)

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        with os.fdopen(os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600), "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    def write(self, records):
        value = (encode({"version": 1, "bindings": records}) + "\n").encode()
        if len(value) > 1024 * 1024:
            raise ValueError("entry binding index is full")
        fd, temporary = tempfile.mkstemp(prefix=".entry-bindings-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def claim(self, store, entry, entry_cwd):
        project, state = store.main()["cwd"], str(store.directory)
        entry_cwd = str(Path(entry_cwd).resolve(strict=True))
        if not Path(entry_cwd).is_dir():
            raise ValueError("entry cwd must be a real directory")
        with self.locked():
            records = self.read()
            current = records.get(project)
            for cwd, record in records.items():
                if not self.live(cwd, record):
                    continue
                if cwd == project and (record["state_directory"] != state or record["entry_thread_id"] != entry):
                    raise ValueError("this cwd already has a registered manager entry; unbind it there first")
                if record["entry_cwd"] == entry_cwd and record["entry_thread_id"] != entry:
                    raise ValueError("this entry cwd is already bound to another session; unbind it there first")
            if current is None:
                legacy = self.metadata(data_directory(project))
                if legacy and legacy.get("entry_thread_id") and str(data_directory(project).resolve()) != state:
                    raise ValueError("this cwd has a legacy entry in its original state directory; enroll or unbind there first")
            previous = dict(records)
            records[project] = {"state_directory": state, "entry_thread_id": entry, "entry_cwd": entry_cwd}
            self.write(records)  # A hook must also verify SQLite before accepting this reservation.
            try:
                yield
            except BaseException:
                self.write(previous)
                raise

    @contextmanager
    def release(self, store):
        if not self.path.exists():
            yield
            return
        with self.locked():
            records = self.read()
            project = store.main()["cwd"]
            record = records.get(project)
            yield  # SQLite unbinding commits before the index becomes inactive.
            if record and record["state_directory"] == str(store.directory):
                records[project] = {**record, "entry_thread_id": None}
                self.write(records)


def prompt_context(payload, bindings=None):
    if (not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit"
            or "agent_id" in payload or "agent_type" in payload):
        return None
    entry = payload.get("session_id")
    if not isinstance(entry, str) or not isinstance(payload.get("cwd"), str) or not isinstance(payload.get("turn_id"), str) or not payload["turn_id"]:
        return None
    uuid.UUID(entry)
    if not Path(payload["cwd"]).is_absolute():
        return None
    caller = os.environ.get("CODEX_THREAD_ID")
    if caller and caller != entry:
        return None
    cwd = str(Path(payload["cwd"]).resolve(strict=True))
    bindings = bindings or Bindings()
    projects = []
    for project, record in bindings.read().items():
        if record["entry_thread_id"] == entry and record["entry_cwd"] == cwd and bindings.live(project, record):
            projects.append({"project": project, "state_directory": record["state_directory"]})
    if not projects:
        return None
    skill = str(Path(__file__).resolve().parents[2] / "SKILL.md")
    text = ("agent-manager entry binding verified for the current prompt/turn only, not a permanent role. "
            "Use the manager skill at the path below for these registered projects. "
            "Read its instructions if they are absent after context compaction. "
            "Before management actions recheck CLI context; older reminders and missing current binding do not activate a chat. "
            "Dispatch only explicitly authorized work, reuse original workers, confirm startup and return; "
            "do not auto-approve, release human holds or wait for completion unless asked. "
            "This identity reminder grants no new permission. Other chats, forks and workers do not inherit it.\n" +
            encode({"entry_thread_id": entry, "turn_id": payload["turn_id"], "entry_cwd": cwd, "projects": projects, "skill_path": skill}))
    return text if len(text) <= 2400 else None
