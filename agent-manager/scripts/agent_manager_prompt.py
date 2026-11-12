#!/usr/bin/env python3
"""Read-only UserPromptSubmit hook: ordinary sessions produce no output."""

import json
import sys

from agent_manager_runtime.bindings import prompt_context


def main(bindings=None):
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return 0
        context = prompt_context(json.loads(raw), bindings)
        if context:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                      "additionalContext": context}}))
    except (ValueError, TypeError, OSError, RuntimeError):
        pass  # No blocking, logging prompt text, setup, mutation or fallback injection.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
