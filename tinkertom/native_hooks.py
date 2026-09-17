"""Claude native lifecycle hooks; deadlines live outside the model process."""
import json
import os
from pathlib import Path
import sys

from .codex_proxy import INSTRUCTIONS
from .config import Config
from .hooks import rewrite_event
from .native_state import NativeState, schedule, wait_message


def handle(event, state, config):
    kind, session = event.get("hook_event_name"), event.get("session_id")
    if kind == "PreToolUse":
        return rewrite_event(event)
    if not isinstance(session, str) or not session:
        return None
    if kind == "SessionStart":
        state.activate(session)
    with state.edit() as data:
        if kind == "SessionStart":
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": INSTRUCTIONS}}
        if data.get("session_id") != session:
            return None  # Never let subagents or stale hooks schedule the main UI.
        if kind == "StopFailure" and event.get("error") == "rate_limit":
            schedule(data, session, event, config)
        elif kind == "UserPromptSubmit" and data.get("status") == "waiting":
            pending = data.setdefault("pending_prompts", [])
            prompt = event.get("prompt", "")
            if sum(map(len, pending)) + len(prompt) > 128000:
                return {"decision": "block", "reason": "TinkerTom queue is full; save additional instructions in a file."}
            pending.append(prompt)
            return {"decision": "block", "reason": "Follow-up queued locally. " + wait_message(data)}
        elif kind == "Stop":
            data.update(status="ready", pending_prompts=[])
    return None


def main():
    directory = os.environ.get("TINKERTOM_NATIVE_DIR")
    if not directory:
        return
    config = Config(**json.loads((Path(directory) / "config.json").read_text())).validate()
    result = handle(json.load(sys.stdin), NativeState(Path(directory)), config)
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
