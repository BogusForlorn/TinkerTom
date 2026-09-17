"""Atomic task records and a workspace-wide single-writer lease (POSIX)."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {self.workspace}")
        self.root = self.workspace / ".tinkertom"
        self.root.mkdir(mode=0o700, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", task_id):
            raise ValueError("Task ID must be the 12-character ID returned by start")
        return self.root / "tasks" / task_id

    def load(self, task_id: str) -> dict:
        return json.loads((self.task_dir(task_id) / "state.json").read_text())

    def save(self, state: dict) -> None:
        state["updated_at"] = time.time()
        atomic_write(self.task_dir(state["id"]) / "state.json", json.dumps(state, indent=2) + "\n")

    def create(self, objective: str, config: dict, session: str | None = None) -> dict:
        task_id = uuid.uuid4().hex[:12]
        self.task_dir(task_id).mkdir(parents=True, mode=0o700)
        state = {
            "id": task_id, "workspace": str(self.workspace), "objective": objective,
            "config": config, "provider": config["provider"], "session_id": session,
            "status": "pending", "created_at": time.time(), "attempts": 0,
            "turns": 0, "session_turns": 0, "next_run_at": None,
            "failure_count": 0, "stall_count": 0, "last_progress": None,
            "feedback": "", "usage": {}, "runner_pid": None,
        }
        self.save(state)
        atomic_write(self.task_dir(task_id) / "checkpoint.md", "No milestones completed yet.\n")
        return state

    def event(self, task_id: str, event: str, **fields) -> None:
        path = self.task_dir(task_id) / "events.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps({"at": time.time(), "event": event, **fields}) + "\n")

    def tasks(self) -> list[dict]:
        return [json.loads(path.read_text()) for path in sorted(self.root.glob("tasks/*/state.json"))]

    @contextmanager
    def mailbox(self, task_id: str):
        with (self.task_dir(task_id) / "mailbox.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def send(self, task_id: str, message: str) -> str:
        if not message.strip():
            raise ValueError("Message cannot be empty")
        if len(message) > 32000:
            raise ValueError("Message exceeds 32000 characters; save longer instructions in a file")
        self.load(task_id)
        with self.mailbox(task_id):
            message_id = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
            atomic_write(self.task_dir(task_id) / "inbox" / f"{message_id}.json",
                         json.dumps({"id": message_id, "text": message, "at": time.time()}))
        return message_id

    def pending_messages(self, state: dict) -> list[dict]:
        acknowledged = set(state.get("acknowledged_messages", []))
        return [json.loads(path.read_text()) for path in sorted((self.task_dir(state["id"]) / "inbox").glob("*.json"))
                if path.stem not in acknowledged]

    def active_task(self) -> str | None:
        path = self.root / "active-task"
        return path.read_text().strip() if path.exists() else None

    @contextmanager
    def lease(self):
        with (self.root / "runner.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("A runner already owns this workspace; use a separate worktree for parallel tasks") from exc
            # Closing releases the lease. Do not LOCK_UN: children inherit it, so a
            # supervisor crash cannot allow another runner alongside a living child.
            yield stream.fileno()

    def is_running(self) -> bool:
        try:
            with self.lease():
                return False
        except ValueError:
            return True
