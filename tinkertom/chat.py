"""A terminal chat view attached to a durable background task."""
import json
import sys
import threading

from .storage import Store, atomic_write


def attach(store: Store, task_id: str, send_message, resume_task) -> int:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError as exc:
        raise ValueError("Chat requires prompt-toolkit; run scripts/install-tools.py with the suite virtualenv") from exc
    if not sys.stdin.isatty():
        raise ValueError("Chat needs a terminal; use start --detach and send TASK_ID MESSAGE for scripts")
    state = store.load(task_id)
    print(f"TinkerTom · {state['provider']} · {task_id} · {store.workspace}")
    print("Type a follow-up any time. It is delivered at the next turn, after any cooldown.")
    print("/detach leaves the worker running; /stop pauses it; /status shows state; /resume restarts it.")
    print(f"Current status: {state['status']}")
    if state.get("last_report"):
        print(state["last_report"]["summary"])
    finished = threading.Event()
    path = store.task_dir(task_id) / "events.jsonl"
    offset = path.stat().st_size if path.exists() else 0

    def watch():
        nonlocal offset
        while not finished.wait(0.25):
            if not path.exists():
                continue
            with path.open() as stream:
                stream.seek(offset)
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith("\n"):
                        offset = position
                        break
                    offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    name = event.get("event")
                    if name in {"report", "waiting", "blocked", "paused", "completed", "running", "retrying"}:
                        print(f"\n[{name}] {event.get('message', '')}")

    with patch_stdout():
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        session = PromptSession()
        try:
            while True:
                try:
                    message = session.prompt("You › ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("Detached. The worker keeps running; attach again with TinkerTom attach " + task_id)
                    return 0
                if not message:
                    continue
                if message in {"/detach", "/quit", "/exit"}:
                    print("Detached; worker continues.")
                    return 0
                if message == "/stop":
                    atomic_write(store.task_dir(task_id) / "stop", "Stop requested from chat\n")
                    print("Stop requested. Use /resume when ready.")
                elif message == "/resume":
                    resume_task()
                elif message == "/status":
                    state = store.load(task_id)
                    print(json.dumps({key: state.get(key) for key in ("status", "provider", "turns", "next_run_at", "feedback")}, indent=2))
                elif message.startswith("/"):
                    print("Commands: /detach /stop /resume /status. Provider quota/credit commands are never forwarded.")
                else:
                    send_message(message)
                    print("Queued for the next available turn.")
        finally:
            finished.set()
            watcher.join(timeout=1)
