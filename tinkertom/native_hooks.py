"""Claude native lifecycle hooks; deadlines live outside the model process."""
import json
import os
from pathlib import Path
import sys
import time

from .codex_proxy import CLAUDE_POLICY, coordinator_policy, INSTRUCTIONS
from .config import Config
from .hooks import rewrite_event
from .handover import handover_context
from .native_state import NativeState, schedule, wait_message


def handle(event, state, config):
    kind, session = event.get("hook_event_name"), event.get("session_id")
    if kind == "PreToolUse":
        return rewrite_event(event)
    # Subagent hooks may rewrite Bash, but must not mutate supervisor state.
    if event.get("agent_id"):
        return None
    if not isinstance(session, str) or not session:
        return None
    if kind == "SessionStart":
        state.activate(session)
    with state.edit() as data:
        if kind == "SessionStart":
            directory = state.directory
            workspace = (directory.parents[2]
                         if len(directory.parents) >= 3
                         and directory.parent.name == "native"
                         and directory.parent.parent.name == ".tinkertom"
                         else None)
            context = handover_context(workspace, "claude") if workspace is not None else ""
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": INSTRUCTIONS + coordinator_policy(config.coordinator_edits) + CLAUDE_POLICY + context}}
        if data.get("session_id") != session:
            return None  # Never let subagents or stale hooks schedule the main UI.
        if kind == "PostCompact" and data.get("status") == "waiting":
            pending = data.get("pending_prompts") or []
            if not pending:
                data.update(status="ready", pending_prompts=[])
                for key in ("detected_at", "next_run_at", "recovery_reason"):
                    data.pop(key, None)
            elif data.get("recovery_reason") != "post_compact":
                data.update(next_run_at=time.time(), recovery_reason="post_compact")
        elif kind == "StopFailure" and event.get("error") != "rate_limit":
            if data.get("status") in {"waiting", "resuming"} and not data.get("pending_prompts"):
                data.update(status="ready", pending_prompts=[])
                for key in ("detected_at", "next_run_at", "recovery_reason"):
                    data.pop(key, None)
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
