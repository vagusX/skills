"""Current Codex chat boundaries; an entry binding is not a project-wide mode."""

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import uuid


CONTROL_COMMANDS = frozenset({
    "start", "run", "stop", "panel", "panel-run", "panel-stop", "unbind-entry",
    "submit", "chat", "followup", "pause", "continue", "answer", "ack",
    "source-add", "source-disable", "poll", "ingest",
    "heartbeat-plan", "heartbeat-bind",
    "memory-put", "memory-forget", "compact",
    "hook-plan", "hook-install",
    "watch-pr",
})


def _context(caller, entry, managed_role):
    if managed_role:
        role = managed_role
    elif caller and caller == entry:
        role = "entry"
    elif caller:
        role = "other" if entry else "unbound"
    else:
        role = "terminal"
    return {"current_thread_id": caller, "entry_thread_id": entry, "role": role,
            "active": role == "entry"}


def session_context(store=None):
    caller = os.environ.get("CODEX_THREAD_ID") or None
    main = store.one("SELECT * FROM chats WHERE role='main'") if store else None
    entry = main.get("entry_thread_id") if main else None
    managed = store.one("SELECT role FROM chats WHERE thread_id=?", (caller,)) if store and caller else None
    result = _context(caller, entry, managed["role"] if managed else None)
    return verify_registered_context(result, store.directory, main.get("cwd")) if store and main else result


def verify_registered_context(context, directory, cwd):
    if cwd and context["entry_thread_id"]:
        from .bindings import Bindings
        if not Bindings().allows(cwd, directory, context["entry_thread_id"]):
            return {**context, "active": False, "role": "other" if context["current_thread_id"] else "terminal",
                    "binding_conflict": True}
    return context


def read_session_context(directory):
    """Inspect even a legacy database without running Store migrations."""
    path = Path(directory).expanduser().resolve() / "state.sqlite3"
    if not path.is_file():
        return session_context()
    caller = os.environ.get("CODEX_THREAD_ID") or None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            main = db.execute("SELECT * FROM chats WHERE role='main'").fetchone()
            managed = db.execute("SELECT role FROM chats WHERE thread_id=?", (caller,)).fetchone() if caller else None
            entry = dict(main).get("entry_thread_id") if main else None
            result = _context(caller, entry, managed["role"] if managed else None)
            return verify_registered_context(result, path.parent, dict(main).get("cwd")) if main else result
    except sqlite3.Error as error:
        raise RuntimeError("cannot read manager context; state was not initialized or migrated") from error


def require_active(context, operation):
    # An explicit terminal command or an internal service has no Codex caller.
    # This is a workflow boundary, not an OS security or caller-authentication layer.
    if context.get("binding_conflict") or (context["current_thread_id"] and not context["active"]):
        raise ValueError(f"{operation}: current Codex chat is not this manager's active entry; "
                         "continue in the bound entry or explicitly invoke $setup-agent-manager. "
                         "Do not auto-activate, rebind, or resubmit the work.")


def require_entry(store, operation):
    require_active(session_context(store), operation)


def check_binding(store, thread_id):
    if not thread_id:
        raise ValueError("bind-entry requires a real current CODEX_THREAD_ID or an explicit terminal thread ID")
    uuid.UUID(thread_id)
    caller = os.environ.get("CODEX_THREAD_ID")
    if caller and thread_id != caller:
        raise ValueError("bind-entry must use the actual current CODEX_THREAD_ID, not a copied or parent chat ID")
    if store.one("SELECT id FROM chats WHERE thread_id=?", (thread_id,)):
        raise ValueError("a managed main or worker cannot activate itself as an entry chat")
    entry = store.main()["entry_thread_id"]
    if entry and entry != thread_id:
        raise ValueError("this manager already has another active entry; do not take it over. "
                         "Use that entry, or explicitly unbind there before activating this chat.")


def background_environment():
    # Services keep their existing authorization, independent of the launching chat.
    env = dict(os.environ)
    env.pop("CODEX_THREAD_ID", None)
    return env
