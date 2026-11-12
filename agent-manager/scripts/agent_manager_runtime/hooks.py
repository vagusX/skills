"""显式 setup 时注册提醒 hook；不修改 Codex 的原生信任记录。"""

import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


MAX_HOOKS_BYTES = 1024 * 1024
EVENT_NAME = "UserPromptSubmit"
REVIEW_MESSAGE = (
    "Confirm this hook through /hooks or the current host's Hooks UI. "
    "Native trust was not inspected or changed. An external CLI probe does not "
    "confirm that the current Desktop chat has loaded the hook."
)


def hook_plan():
    """只返回安装位置与定义，不创建目录或读取其他 hook。"""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    script = Path(__file__).resolve().parents[1] / "agent_manager_prompt.py"
    command = shlex.join([str(Path(sys.executable).resolve()), str(script)])
    group = {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
    return {"config_path": str(home / "hooks.json"), "event_name": EVENT_NAME,
            "group": group, "command": command}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("hooks.json has duplicate object keys; refusing to rewrite it")
        result[key] = value
    return result


def _read_config(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("hooks.json must be a regular file")
        content = source.read(MAX_HOOKS_BYTES + 1)
    if len(content) > MAX_HOOKS_BYTES:
        raise ValueError("hooks.json exceeds the 1 MiB installation limit")
    try:
        config = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("hooks.json is not valid JSON; no changes written") from error
    if not isinstance(config, dict):
        raise ValueError("hooks.json must contain a JSON object")
    return config


def _event_groups(config):
    if "hooks" not in config:
        config["hooks"] = {}
    hooks = config["hooks"]
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json hooks must be an object")
    if EVENT_NAME not in hooks:
        hooks[EVENT_NAME] = []
    groups = hooks[EVENT_NAME]
    if not isinstance(groups, list):
        raise ValueError("hooks.json UserPromptSubmit must be an array")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("hooks.json UserPromptSubmit groups must have a hooks array")
        if not all(isinstance(handler, dict) for handler in group["hooks"]):
            raise ValueError("hooks.json UserPromptSubmit handlers must be objects")
    return groups


def _atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-manager-hooks-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def install_hook():
    """锁内重读并追加独立 group；相同命令保留原设置，交宿主确认。"""
    plan = hook_plan()
    path = Path(plan["config_path"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.parent / ".agent-manager-hooks.lock", flags, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("hook installation lock must be a regular file")
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        config = _read_config(path)
        groups = _event_groups(config)
        existing = [group for group in groups if any(
            handler.get("command") == plan["command"] for handler in group["hooks"])]
        changed = not existing
        matches = len(existing) == 1 and existing[0] == plan["group"]
        if changed:
            groups.append(plan["group"])
            content = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(content) > MAX_HOOKS_BYTES:
                raise ValueError("merged hooks.json exceeds the 1 MiB installation limit")
            _atomic_write(path, content)
        review = REVIEW_MESSAGE
        if existing and not matches:
            review = "Existing command retained with different settings; review its definition. " + review
        return {**plan, "changed": changed, "reused": bool(existing),
                "existing_settings_match": matches if existing else None,
                "requires_native_review": True, "review_message": review}
