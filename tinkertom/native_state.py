"""Durable native-session cooldown state, shared by hooks and the launcher."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import time

from .providers import reset_time
from .storage import atomic_write


CONTINUE = ("Continue the interrupted task from the existing conversation and working files. "
            "Inspect progress before retrying actions. Work until complete or a concrete blocker requires my input. "
            "Do not query /usage, claim/reset quota, enable extra usage, buy credits, or switch billing, accounts, models or providers to evade a limit.")


class NativeState:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "state.json"

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def activate(self, session: str):
        def path(identifier):
            return self.directory / "sessions" / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")
        with self.edit() as data:
            previous = data.get("session_id")
            if previous == session:
                return
            if previous:
                atomic_write(path(previous), json.dumps(data))
            saved = path(session)
            restored = json.loads(saved.read_text()) if saved.exists() else {"session_id": session, "status": "ready"}
            data.clear()
            data.update(restored)

    @contextmanager
    def edit(self):
        with (self.directory / "state.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            previous = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != previous:
                atomic_write(self.path, json.dumps(data, indent=2) + "\n")


def schedule(data: dict, session: str, error: dict, config, now=None):
    now = time.time() if now is None else now
    # Duplicate error + turn-completed notifications must not extend the wait.
    if data.get("status") == "waiting" and data.get("session_id") == session:
        return
    explicit = reset_time(error, now)
    data.update(session_id=session, status="waiting", detected_at=now,
                next_run_at=(explicit or now + config.cooldown_seconds) + config.reset_buffer_seconds)


def wait_message(data):
    deadline = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(data["next_run_at"]))
    return f"TinkerTom is waiting for the normal quota reset until {deadline}. Continuation is automatic while this launcher stays open. No reset credits or extra usage are requested."
