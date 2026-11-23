---
name: agent-scratch
description: 统一管理各 Agent 的临时目录：优先落盘到 `~/agent-scratch/<agent>/<session-id>/`（沙箱无权写 `~` 时自动回退到 `/tmp/agent-scratch/<agent>/<session-id>/`），并在首次创建时写入带创建时间与简短用途（≤50字）的 `readme.txt`；同时提供跨两个根目录的占用盘点与按天数清理脚本。触发短语：agent-scratch / 临时目录 / 清理 agent 临时文件 / 清理 tmp / 清理 scratch / 创建临时工作目录。**注意：须在全局 `AGENTS.md` 加一行常驻规则，否则 Agent 随手写临时文件前不会主动加载本 skill。**
---

# Agent Scratch — 统一临时目录与清理

把所有 Agent 主动创建的临时文件收口到统一目录，避免散落在 `/tmp`（`/private/tmp`）里越积越大且分不清是谁、何时、为何创建的。

> **前置要求（必配）**：仅安装本 skill 不够（skill 按需加载，写临时文件时通常不会被触发）。必须在全局 `AGENTS.md`（以及独立的 `CLAUDE.md` 等常驻规则文件）中加一行：
> ```markdown
> - 临时文件禁止散落写入 `/tmp`：优先写入 `~/agent-scratch/<agent>/<session-id>/`（沙箱无权写 `~` 时回退到 `/tmp/agent-scratch/<agent>/<session-id>/`），首次创建时写入 `readme.txt` 注明创建时间及简短用途（≤50字）；盘点或清理走 `agent-scratch` skill。
> ```

## 路径规范与沙箱回退

1. **首选根目录**：`~/agent-scratch/<agent>/<session-id>/`
2. **沙箱回退根目录**：若当前沙箱不允许写 `~`，自动回退到 `/tmp/agent-scratch/<agent>/<session-id>/`，保持完全相同的层级与 `readme.txt` 规范，避免散落到 `/tmp` 根目录。
3. **范围边界**：只约束 Agent 主动创建的临时脚本、导出数据、中间产物与日志；子进程或构建工具自行写入 `/private/tmp` 的缓存不做强行重定向。

示例：
```text
~/agent-scratch/
├── codex/
│   └── session-20261123-174500-a1b2/
│       ├── readme.txt
│       └── ...
├── claude/
│   └── session-f1a2b3c4/
│       └── readme.txt
└── agy/
    └── 0925924a-fd9b-4d5f-9127-2f63a3e440c3/
        └── readme.txt
```

## `readme.txt` 规范

首次创建 session 目录时写入 `readme.txt`（已存在则不覆盖）：

```text
created: 2026-11-23 17:45:00 +0800
agent: codex
session: session-xxx-xxx
cwd: /path/to/project
note: 对比脚本输出与临时日志（可选，≤50字）
```

- `created`：创建时间（必填）
- `agent`：Agent 标识（`codex` / `claude` / `agy` / `cursor` 等，必填）
- `session`：当前会话 ID（拿不到时由脚本生成 `session-<YYYYMMDD-HHMMSS>-<4hex>`，必填）
- `cwd`：发起时的工作目录（可选，方便清理时判断所属项目）
- `note`：一句话说明临时目录用途（可选，**严格 ≤50 字**；用途显而易见时可省略）

## 脚本用法

脚本位于本 skill 的 `scripts/` 目录。

### 1. 创建 / 复用当前会话临时目录

```bash
SCRATCH_DIR="$("$SKILL_DIR/scripts/init.sh" <agent> [session-id] [note])"
```

- 自动检测 `~/agent-scratch` 是否可写，无权写 `~` 时自动回退到 `/tmp/agent-scratch`。
- 幂等：同 `<agent>/<session-id>` 重复调用不会覆盖原有 `readme.txt`。
- `stdout` 仅输出创建的目录绝对路径，方便直接捕获使用。
- 若不方便调脚本，也可直接 `mkdir -p` 后按上述格式写入 `readme.txt`。

各 Agent 获取 `session-id` 参考：
- **Antigravity (`agy`)**：直接使用系统上下文中的 `Conversation ID`
- **Claude Code**：优先用环境变量 `CLAUDE_SESSION_ID`，无则留空由脚本生成
- **Codex**：优先用 `CODEX_THREAD_ID` 或当前 session 标识，无则留空由脚本生成
- **其他 Agent**：传入自身 session 标识，或留空自动生成

### 2. 盘点占用（同时扫描 `~/agent-scratch` 与 `/tmp/agent-scratch`）

```bash
python3 "$SKILL_DIR/scripts/clean.py" list
python3 "$SKILL_DIR/scripts/clean.py" list --agent codex
```

输出各根目录下的 `agent / session`、占用大小、创建时间、项目路径与 `note`，并汇总总占用；同时标出缺少 `readme.txt` 的异常目录。

### 3. 按天数清理（默认 dry-run，安全防误删）

```bash
# 先预览超过 7 天的目录（不删除）
python3 "$SKILL_DIR/scripts/clean.py" prune --days 7

# 确认后真正删除
python3 "$SKILL_DIR/scripts/clean.py" prune --days 7 --yes

# 仅清理指定 agent（如超过 3 天）
python3 "$SKILL_DIR/scripts/clean.py" prune --days 3 --agent codex --yes
```

- 过期时间优先解析 `readme.txt` 中的 `created` 字段，解析不到时回退为目录 mtime。
- 删除后会顺手清理变空的 `<agent>` 父目录，不会触碰 `agent-scratch` 之外的任何文件。
