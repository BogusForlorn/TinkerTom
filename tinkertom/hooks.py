"""Per-invocation Claude hook. Does not alter the user's global settings."""
import json
import os
from pathlib import Path
import re
import shlex
import sys

from .storage import atomic_write


def settings_file(directory: Path) -> Path:
    invocation = shlex.join([sys.executable, "-m", "tinkertom.hooks"])
    path = directory / "claude-hooks.json"
    atomic_write(path, json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": invocation, "timeout": 5}
    ]}]}}))
    return path


def rewrite_event(event: dict) -> dict | None:
    if not os.environ.get("TINKERTOM_MANAGED") or event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not command:
        return None
    # Only wrap simple commands. Compound shells, redirects, substitutions and
    # shell state changes must keep their original semantics in Claude's shell.
    if re.search(r"[\n|&;<>`$]", command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or argv[0] not in {"git", "rg", "grep", "ls", "cat", "head", "tail", "find", "pytest", "python", "python3", "npm", "pnpm", "cargo", "go", "ruff", "docker", "kubectl", "make"}:
        return None
    wrapper = [sys.executable, "-m", "tinkertom", "compact"]
    if os.environ.get("TINKERTOM_RTK", "1") == "1":
        wrapper.append("--rtk")
    if os.environ.get("TINKERTOM_HEADROOM", "1") != "1":
        wrapper.append("--no-headroom")
    wrapper += ["--shell", command]
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {
        **tool_input, "command": shlex.join(wrapper)
    }}}


if __name__ == "__main__":
    try:
        result = rewrite_event(json.load(sys.stdin))
        if result:
            print(json.dumps(result))
    except (ValueError, TypeError):
        pass  # An optional optimizer must not break execution on a bad hook payload.
