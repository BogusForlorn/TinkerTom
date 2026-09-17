"""Bounded local access to the pinned Beads CLI and embedded Dolt store."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .optimization import suite_environment


@contextmanager
def local_lock(workspace, name):
    directory = workspace / ".tinkertom"
    directory.mkdir(exist_ok=True, mode=0o700)
    with (directory / name).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def bead_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}", value):
        raise ValueError("Supply an explicit Beads issue ID")
    return value


def text_arg(args, key, limit=16000):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must contain 1..{limit} characters")
    return value


def beads_call(workspace: Path, args: dict):
    env = suite_environment()
    binary = shutil.which("bd", path=env["PATH"])
    if not binary:
        raise ValueError("Beads is missing. Run scripts/install-tools.py with the suite Python.")
    env.update(BD_NON_INTERACTIVE="1", BEADS_DIR=str(workspace / ".beads"), BEADS_ACTOR="tinkertom")
    action = args.get("action", "ready")
    limit = args.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if action in {"init", "ready", "list", "recall"}:
        command = (["recall", bead_id(args["key"])] if args.get("key") else ["memories"]) if action == "recall" else [action]
        if action in {"ready", "list"}:
            command += ["--limit", str(limit)]
    elif action == "create":
        command = ["create", "--title", text_arg(args, "title", 300), "--type", "task"]
        if args.get("description"):
            command += ["--description", text_arg(args, "description")]
    elif action in {"show", "claim", "close", "update"}:
        command = ["update" if action in {"claim", "update"} else action, bead_id(args.get("id"))]
        if action == "claim":
            command.append("--claim")
        elif action == "update":
            status = args.get("status")
            if status not in {"open", "in_progress", "blocked", "deferred"}:
                raise ValueError("status must be open, in_progress, blocked or deferred")
            command += ["--status", status]
            if args.get("notes"):
                command += ["--append-notes", text_arg(args, "notes")]
        elif action == "close":
            command += ["--reason", text_arg(args, "notes") if args.get("notes") else "Verified by the coordinator"]
    elif action == "depend":
        command = ["dep", "add", bead_id(args.get("id")), bead_id(args.get("depends_on"))]
    elif action == "remember":
        notes = text_arg(args, "notes")
        key = bead_id(args["key"]) if args.get("key") else "tt-" + hashlib.sha256(notes.encode()).hexdigest()[:12]
        command = ["remember", "--key", key, "--", notes]
    else:
        raise ValueError(f"Unsupported Beads action: {action}")
    with local_lock(workspace, "beads.lock"):
        def execute(argv):
            result = subprocess.run([binary, "--sandbox", "--json", *argv], cwd=workspace, env=env,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise ValueError("Beads: " + (result.stderr or result.stdout)[-3000:])
            return result.stdout
        if not (workspace / ".beads/metadata.json").exists():
            execute(["init", "--stealth", "--skip-agents", "--skip-hooks", "--non-interactive", "--quiet"])
        output = json.dumps({"initialized": True, "directory": str(workspace / ".beads")}) if action == "init" else execute(command)
    if len(output) > 24000:
        output = output[:24000] + "\n[Truncated; narrow the query or use bd show ID.]"
    return output, False
