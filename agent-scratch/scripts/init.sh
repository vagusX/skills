#!/usr/bin/env bash
# 创建或复用当前 agent 会话的临时目录，并写入 readme.txt。
# 用法：init.sh <agent> [session-id] [note]
# 优先写入 ~/agent-scratch/<agent>/<session-id>/；沙箱无权写 ~ 时自动回退到 /tmp/agent-scratch/<agent>/<session-id>/。
# stdout 仅输出最终目录的绝对路径。

set -euo pipefail

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "Usage: $(basename "$0") <agent> [session-id] [note]" >&2
  exit 1
fi

RAW_AGENT="$1"
RAW_SESSION="${2:-}"
RAW_NOTE="${3:-}"

# 清洗目录名中的非法字符
AGENT="$(printf '%s' "$RAW_AGENT" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]/-/g; s/^-*//; s/-*$//')"
if [[ -z "$AGENT" ]]; then
  AGENT="unknown"
fi

if [[ -n "$RAW_SESSION" ]]; then
  SESSION="$(printf '%s' "$RAW_SESSION" | sed 's/[^a-zA-Z0-9._-]/-/g; s/^-*//; s/-*$//')"
else
  RAND_HEX="$(LC_ALL=C od -An -N2 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || printf '%04x' "$$")"
  SESSION="session-$(date '+%Y%m%d-%H%M%S')-${RAND_HEX}"
fi

# 截断 note 到 50 个字符以内（支持 UTF-8 中文），并压平换行
NOTE=""
if [[ -n "$RAW_NOTE" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    NOTE="$(python3 -c 'import sys; s = " ".join(sys.argv[1].split()); print(s[:50], end="")' "$RAW_NOTE")"
  else
    NOTE="$(printf '%s' "$RAW_NOTE" | tr '\r\n' '  ' | cut -c1-50)"
  fi
fi

# 选择可写根目录：优先 $AGENT_SCRATCH_ROOT 或 ~/agent-scratch，不可写则回退 /tmp/agent-scratch
select_base() {
  local preferred="${AGENT_SCRATCH_ROOT:-$HOME/agent-scratch}"
  if mkdir -p "$preferred/$AGENT" 2>/dev/null && [[ -w "$preferred/$AGENT" ]]; then
    printf '%s' "$preferred"
    return 0
  fi
  local fallback="/tmp/agent-scratch"
  mkdir -p "$fallback/$AGENT"
  printf '%s' "$fallback"
}

BASE="$(select_base)"
TARGET_DIR="$BASE/$AGENT/$SESSION"
mkdir -p "$TARGET_DIR"

README="$TARGET_DIR/readme.txt"
if [[ ! -f "$README" ]]; then
  CREATED="$(date '+%Y-%m-%d %H:%M:%S %z')"
  CWD_PATH="$(pwd 2>/dev/null || printf 'unknown')"
  {
    printf 'created: %s\n' "$CREATED"
    printf 'agent: %s\n' "$AGENT"
    printf 'session: %s\n' "$SESSION"
    printf 'cwd: %s\n' "$CWD_PATH"
    if [[ -n "$NOTE" ]]; then
      printf 'note: %s\n' "$NOTE"
    fi
  } > "$README"
fi

printf '%s\n' "$TARGET_DIR"
