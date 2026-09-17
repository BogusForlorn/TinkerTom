#!/usr/bin/env python3
"""Scripted CLI fixture. Never connects to a model provider."""
import json
import os
from pathlib import Path
import sys
import time


workspace = Path.cwd()
scenario = json.loads((workspace / "scenario.json").read_text())
count_path = workspace / "call-count"
count = int(count_path.read_text()) if count_path.exists() else 0
count_path.write_text(str(count + 1))
step = scenario["steps"][min(count, len(scenario["steps"]) - 1)]
provider = scenario["provider"]
prompt = sys.stdin.read()
with (workspace / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")


def emit(data):
    print(json.dumps(data), flush=True)


if provider == "codex":
    emit({"type": "thread.started", "thread_id": "fixture-session"})
else:
    emit({"type": "system", "subtype": "init", "session_id": "fixture-session"})

if step == "rate_limit":
    reset = time.time() + scenario.get("wait", 0.15)
    if provider == "codex":
        emit({"type": "turn.failed", "error": {"message": "Usage limit reached", "resets_at": reset}})
    else:
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": reset}})
        emit({"type": "result", "is_error": True, "result": "Usage limit reached"})
    sys.exit(1)
if step == "sleep":
    time.sleep(60)
if step == "auth":
    print("401 authentication failed", file=sys.stderr)
    sys.exit(1)
if step == "complete":
    (workspace / "feature.txt").write_text("implemented\n")
status = "complete" if step in {"complete", "premature_complete"} else "continue"
report = json.dumps({"attempt": os.environ["TINKERTOM_ATTEMPT"], "status": status,
                     "summary": f"Finished fixture step {count}", "next_steps": [] if status == "complete" else ["implement feature"]})
if provider == "codex":
    # A huge tool event exercises the bounded-memory parser; control events follow.
    if scenario.get("huge_output"):
        emit({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x" * 2200000}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": report}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5}})
else:
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": "fixture-session",
          "result": report, "usage": {"input_tokens": 10, "cache_read_input_tokens": 4, "output_tokens": 5}})
