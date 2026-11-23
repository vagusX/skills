#!/usr/bin/env python3
"""盘点并清理 ~/agent-scratch 与 /tmp/agent-scratch 下的 Agent 临时目录。

用法：
  clean.py list [--agent <name>]
  clean.py prune [--days <N>] [--agent <name>] [--yes]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def get_roots() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = [
        ("~/agent-scratch", Path.home() / "agent-scratch"),
        ("/tmp/agent-scratch", Path("/tmp/agent-scratch")),
    ]
    seen: set[Path] = set()
    roots: list[tuple[str, Path]] = []
    for label, path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append((label, path))
    return roots


def parse_readme(readme: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not readme.is_file():
        return meta
    try:
        for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            meta[key.strip().lower()] = val.strip()
    except OSError:
        pass
    return meta


def parse_created(meta: dict[str, str], path: Path) -> datetime:
    raw = meta.get("created", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        if not raw:
            break
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
    except OSError:
        return datetime.now().astimezone()


def dir_size_kb(path: Path) -> int:
    try:
        out = subprocess.check_output(
            ["du", "-sk", str(path)], text=True, stderr=subprocess.DEVNULL
        )
        return int(out.split()[0])
    except Exception:
        total = 0
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total // 1024


def fmt_size(kb: int) -> str:
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.2f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def scan_entries(agent_filter: str | None = None) -> list[dict]:
    entries: list[dict] = []
    for root_label, root_path in get_roots():
        if not root_path.is_dir():
            continue
        for agent_dir in sorted(root_path.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name
            if agent_filter and agent != agent_filter:
                continue
            for session_dir in sorted(agent_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                readme = session_dir / "readme.txt"
                meta = parse_readme(readme)
                created = parse_created(meta, session_dir)
                entries.append(
                    {
                        "root": root_label,
                        "agent": agent,
                        "session": session_dir.name,
                        "path": session_dir,
                        "size_kb": dir_size_kb(session_dir),
                        "created": created,
                        "has_readme": readme.is_file(),
                        "cwd": meta.get("cwd", ""),
                        "note": meta.get("note", ""),
                    }
                )
    entries.sort(key=lambda e: e["created"], reverse=True)
    return entries


def cmd_list(agent_filter: str | None) -> int:
    entries = scan_entries(agent_filter)
    if not entries:
        print("没有找到任何 agent-scratch 目录（已扫描 ~/agent-scratch 与 /tmp/agent-scratch）。")
        return 0

    total_kb = sum(e["size_kb"] for e in entries)
    print(f"共 {len(entries)} 个临时目录，合计 {fmt_size(total_kb)}：\n")
    print(f"{'SIZE':>9}  {'CREATED':<16}  {'ROOT':<18}  {'AGENT/SESSION':<36}  {'NOTE / CWD'}")
    print("-" * 104)
    for e in entries:
        created_s = e["created"].strftime("%Y-%m-%d %H:%M")
        target = f"{e['agent']}/{e['session']}"
        flag = "" if e["has_readme"] else "[缺readme] "
        info = e["note"] or e["cwd"] or "-"
        if e["note"] and e["cwd"]:
            info = f"{e['note']} ({Path(e['cwd']).name})"
        print(f"{fmt_size(e['size_kb']):>9}  {created_s:<16}  {e['root']:<18}  {target:<36}  {flag}{info}")
    return 0


def cmd_prune(days: float, agent_filter: str | None, yes: bool) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    entries = [e for e in scan_entries(agent_filter) if e["created"] < cutoff]
    if not entries:
        print(f"没有超过 {days:g} 天的 agent-scratch 目录需要清理。")
        return 0

    total_kb = sum(e["size_kb"] for e in entries)
    mode = "执行删除" if yes else "预览模式（加 --yes 才会真正删除）"
    print(f"[{mode}] 命中 {len(entries)} 个超过 {days:g} 天的目录，预计释放 {fmt_size(total_kb)}：\n")
    for e in entries:
        created_s = e["created"].strftime("%Y-%m-%d %H:%M")
        note = f"  # {e['note']}" if e["note"] else ""
        print(f"  {fmt_size(e['size_kb']):>9}  {created_s}  {e['path']}{note}")
        if yes:
            shutil.rmtree(e["path"])
            parent = e["path"].parent
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    if yes:
        print(f"\n已清理完成，共释放约 {fmt_size(total_kb)}。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="盘点并清理 ~/agent-scratch 与 /tmp/agent-scratch")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出所有临时目录与占用大小")
    p_list.add_argument("--agent", help="仅查看指定 agent")

    p_prune = sub.add_parser("prune", help="按天数清理过期临时目录（默认 dry-run）")
    p_prune.add_argument("--days", type=float, default=7, help="清理超过 N 天的目录（默认 7）")
    p_prune.add_argument("--agent", help="仅清理指定 agent")
    p_prune.add_argument("--yes", action="store_true", help="真正执行删除")

    args = parser.parse_args()
    if args.command == "list":
        return cmd_list(args.agent)
    return cmd_prune(args.days, args.agent, args.yes)


if __name__ == "__main__":
    sys.exit(main())
