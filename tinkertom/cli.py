import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__
from .compact import compact
from .chat import attach
from .config import load_config
from .navigation import read_file, symbols
from .optimization import rtk_path, suite_environment
from .runner import Runner
from .storage import Store, atomic_write


CONFIG_TEMPLATE = '''# TinkerTom task defaults; each task saves its own snapshot.
provider = "codex"                 # codex or claude
permissions = "{permissions}"      # standard or yolo
# model = "your-model-id"          # omitted: use your CLI's configured model
cooldown_seconds = 18000            # fallback when no unambiguous reset is supplied
reset_buffer_seconds = 30
retry_seconds = 30
max_failures = 5                    # repeated CLI/network failures
max_stalls = 5                      # repeated reports with no observable progress
max_turns = 0                       # 0: continue until complete or blocked
turn_timeout_seconds = 3600
verify_timeout_seconds = 900
max_context_turns = 0               # 0: native session resume/compaction
checkpoint_chars = 12000
rtk = true                         # optional; requires Rust Token Killer on PATH
headroom = true                    # local output compression, no API proxy
claude_hook = true                 # optimize simple Bash tool calls automatically
beads = true                       # persistent local tasks, dependencies and memory
delegate_coding = true             # smaller-model CLI implementation workers
rubber_duck = true                 # opposite-provider CLI review
codex_worker_model = "gpt-5.6-luna"
claude_worker_model = "sonnet"
codex_duck_model = "gpt-5.6-sol"
claude_duck_model = "claude-opus-4-6"
agent_timeout_seconds = 900        # per helper CLI invocation; cooldown excluded
worker_max_turns = 8               # successful helper turns, not quota retries
verify = []                        # e.g. ["python -m pytest", "npm run build"]
'''


def add_overrides(parser):
    parser.add_argument("--provider", choices=["codex", "claude"])
    parser.add_argument("--permissions", choices=["standard", "yolo"])
    parser.add_argument("--model")
    parser.add_argument("--executable", help="Override the CLI binary path")
    parser.add_argument("--max-turns", type=int, help="Total completed-turn limit; 0 is unlimited")


def overrides(args):
    return {key: getattr(args, key, None) for key in ("provider", "permissions", "model", "executable", "max_turns")}


def parser():
    cli = argparse.ArgumentParser(prog="tinkertom", description="Persistent coding tasks across Codex/Claude usage resets")
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument("-C", "--workspace", type=Path, default=Path.cwd())
    sub = cli.add_subparsers(dest="command", required=True)
    for provider in ("codex", "claude"):
        native = sub.add_parser(provider, add_help=False, help=f"Open the native {provider} UI with the TinkerTom suite")
        native.add_argument("provider_args", nargs=argparse.REMAINDER)
    sub.add_parser("mcp", help="Serve compression, coding workers, rubber duck and Beads over MCP stdio")
    sub.add_parser("native-status", help="Show saved native UI cooldowns")
    init = sub.add_parser("init", help="Create project configuration")
    init.add_argument("--permissions", choices=["standard", "yolo"], default="standard")
    start = sub.add_parser("start", help="Create and run an objective")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("objective", nargs="?")
    source.add_argument("--task-file", type=Path)
    add_overrides(start)
    start.add_argument("--session", help="Adopt an existing session ID belonging to the selected provider")
    start.add_argument("--verify", action="append", help="Acceptance shell command (repeatable); overrides configuration")
    launch_mode = start.add_mutually_exclusive_group()
    launch_mode.add_argument("--detach", action="store_true", help="Run in background with logs on disk")
    launch_mode.add_argument("--enqueue", action="store_true", help="Save a pending task for a service or later resume")
    chat = sub.add_parser("chat", help="Start an autonomous task with an interactive chat view")
    chat.add_argument("objective", nargs="?")
    chat.add_argument("--task-file", type=Path)
    chat.add_argument("--session")
    chat.add_argument("--verify", action="append")
    add_overrides(chat)
    attached = sub.add_parser("attach", help="Attach a chat view to a saved task")
    attached.add_argument("task_id")
    send = sub.add_parser("send", help="Queue a follow-up; continue automatically when the provider is available")
    send.add_argument("task_id")
    send.add_argument("message")
    resume = sub.add_parser("resume", help="Resume a task, preserving its cooldown")
    resume.add_argument("task_id")
    resume.add_argument("--detach", action="store_true")
    add_overrides(resume)
    for name in ("stop", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task_id")
    sub.add_parser("list", help="List saved tasks")
    sub.add_parser("doctor", help="Check installed CLI binaries and RTK")
    compact_parser = sub.add_parser("compact", help="Run a command with bounded output and full log files")
    compact_parser.add_argument("--max-bytes", type=int, default=8000)
    compact_parser.add_argument("--timeout", type=float, default=900)
    compact_parser.add_argument("--rtk", action="store_true", help="Use RTK's supported-command rewrite first")
    compact_parser.add_argument("--no-headroom", action="store_true")
    compact_parser.add_argument("--raw", action="store_true", help="Exact uncompressed output without a footer")
    compact_parser.add_argument("--shell", help="Execute a Bash command string, preserving its quoting")
    compact_parser.add_argument("argv", nargs=argparse.REMAINDER)
    read = sub.add_parser("read", help="Read a file range or Python symbol")
    read.add_argument("path", type=Path)
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int)
    read.add_argument("--symbol")
    symbol = sub.add_parser("symbols", help="List Python function/class names and line spans")
    symbol.add_argument("path", type=Path)
    sub.add_parser("savings", help="Show locally measured command-output reductions")
    wake = sub.add_parser("_wake", help="Internal message delivery worker")
    wake.add_argument("task_id")
    worker = sub.add_parser("_worker", help="Internal background/service worker")
    worker.add_argument("task_id")
    worker.add_argument("--resume", action="store_true")
    add_overrides(worker)
    return cli


def launch(store: Store, task_id: str, *, resume: bool, changes: dict, wake: bool = False) -> int:
    argv = [sys.executable, "-m", "tinkertom", "-C", str(store.workspace), "_wake" if wake else "_worker", task_id]
    if resume:
        argv.append("--resume")
    for key, value in changes.items():
        if value is not None:
            argv += ["--" + key.replace("_", "-"), str(value)]
    # Also works directly from a source checkout, without an installed entry point.
    environment = suite_environment()
    package_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
    with (store.task_dir(task_id) / "runner.log").open("ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, cwd=store.workspace, env=environment)
    print(json.dumps({"task_id": task_id, "worker_pid": proc.pid, "log": str(store.task_dir(task_id) / "runner.log")}))
    return 0


def doctor(workspace: Path) -> dict:
    config = load_config(workspace)
    checks = {}
    for name in ("codex", "claude", "rtk", "headroom", "bd"):
        path = rtk_path() if name == "rtk" else shutil.which(config.executable if name == config.provider and config.executable else name, path=suite_environment()["PATH"])
        item = {"installed": bool(path), "path": path}
        if path:
            try:
                result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, env=suite_environment())
                item["version"] = (result.stdout or result.stderr).strip()[:300]
                if name == "rtk":
                    gain = subprocess.run([path, "gain"], capture_output=True, timeout=10, env=suite_environment())
                    item["token_killer_verified"] = gain.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as exc:
                item["error"] = str(exc)
        checks[name] = item
    checks["platform"] = {"supported": os.name == "posix", "python": sys.version.split()[0]}
    checks["config"] = {"provider": config.provider, "permissions": config.permissions, "acceptance_commands": config.verify}
    checks["note"] = "Checks binary presence only, not live authentication, quota or model access."
    checks["quota_policy"] = "wait_only: no /usage, quota claims, paid extensions or automatic provider/account switching"
    checks["compression"] = {"headroom_mode": "local library, no API proxy", "claude_bash_hook": config.claude_hook,
                             "codex": "native MCP tools plus application context; headless prompt-guided tools; no forced interception"}
    checks["native_ui"] = {"commands": ["tinkertom codex", "tinkertom claude"],
                           "codex": "local app-server bridge; tested with 0.153.4 (experimental protocol)",
                           "claude": "requires StopFailure lifecycle hook; local fixture tests only",
                           "mcp": "per-invocation run/read/symbols/wait_status/rubber_duck/code_worker/beads tools",
                           "lifetime": "keep the launcher running; use tmux for native UI detachment"}
    checks["agents"] = {"transport": "installed CLI subprocesses using existing CLI authentication",
                        "coding_workers": {"codex": config.codex_worker_model, "claude": config.claude_worker_model},
                        "rubber_duck": {"from_claude": config.codex_duck_model, "from_codex": config.claude_duck_model},
                        "enabled": {"workers": config.delegate_coding, "rubber_duck": config.rubber_duck, "beads": config.beads},
                        "coordinator": "your selected native-session model; reviews worker edits and acceptance checks"}
    return checks


def send_message(store: Store, task_id: str, message: str) -> int:
    message_id = store.send(task_id, message)
    print(f"Queued message {message_id} for {task_id}")
    return launch(store, task_id, resume=False, changes={}, wake=True)


def wake_task(store: Store, task_id: str) -> int:
    while True:
        state = store.load(task_id)
        if not store.pending_messages(state):
            return 0
        if store.is_running():
            if store.active_task() == task_id and state["status"] not in {"completed", "blocked", "paused"} and not (store.task_dir(task_id) / "stop").exists():
                return 0  # Existing worker will pick up the inbox at a turn boundary.
            time.sleep(0.25)
            continue
        try:
            status = Runner(store, task_id).run(resume=True)
            return {"completed": 0, "blocked": 2, "paused": 3}.get(status, 1)
        except ValueError as exc:
            if "already owns" not in str(exc):
                raise
            time.sleep(0.25)


def main(argv=None) -> int:
    # Everything after a provider name belongs to that provider, including its
    # help, slash commands and resume syntax. Only consume our leading -C option.
    argv = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(argv):
        if argv[index] in {"-C", "--workspace"}:
            index += 2
        elif argv[index].startswith("--workspace=") or (argv[index].startswith("-C") and len(argv[index]) > 2):
            index += 1
        else:
            break
    if index > len(argv):
        parser().error("-C/--workspace requires a directory")
    if index == len(argv):
        argv.append("codex")
    if argv[index] in {"codex", "claude"}:
        argv.insert(index + 1, "--")
    args = parser().parse_args(argv)
    try:
        workspace = args.workspace.resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        if args.command in {"codex", "claude"}:
            from .native import run_native
            return run_native(workspace, args.command, args.provider_args[1:])
        if args.command == "mcp":
            from .mcp import serve
            return serve(workspace)
        if args.command == "native-status":
            for path in sorted((workspace / ".tinkertom/native").glob("*/state.json")):
                print(path.read_text())
            return 0
        if args.command == "init":
            path = workspace / "tinkertom.toml"
            # Exclusive creation avoids clobbering an existing user's configuration.
            with path.open("x") as stream:
                stream.write(CONFIG_TEMPLATE.format(permissions=args.permissions))
            print(path)
            return 0
        if args.command == "doctor":
            print(json.dumps(doctor(workspace), indent=2))
            return 0
        if args.command == "compact":
            return compact(args.argv, workspace, args.max_bytes, args.timeout, use_rtk=args.rtk and os.environ.get("TINKERTOM_RTK", "1") == "1",
                           headroom=not args.no_headroom and os.environ.get("TINKERTOM_HEADROOM", "1") == "1", raw=args.raw, shell=args.shell)
        if args.command in {"read", "symbols"}:
            path = args.path if args.path.is_absolute() else workspace / args.path
            print(symbols(path) if args.command == "symbols" else read_file(path, args.start, args.end, args.symbol), end="\n")
            return 0
        if args.command in {"start", "resume", "_worker", "_wake", "chat"} and os.environ.get("TINKERTOM_MANAGED"):
            raise ValueError("A managed agent cannot recursively start a supervisor")
        store = Store(workspace)
        if args.command == "savings":
            totals = {"commands": 0, "captured_bytes": 0, "displayed_bytes": 0, "rtk_commands": 0, "headroom_streams": 0}
            for path in store.root.glob("**/command-logs/*/metrics.json"):
                data = json.loads(path.read_text())
                totals["commands"] += 1
                totals["rtk_commands"] += bool(data.get("rtk_command"))
                for name in ("stdout", "stderr"):
                    metrics = data.get(name, {})
                    totals["captured_bytes"] += metrics.get("raw_bytes", 0)
                    totals["displayed_bytes"] += metrics.get("displayed_bytes", 0)
                    totals["headroom_streams"] += bool(metrics.get("headroom", {}).get("applied"))
            print(json.dumps({**totals, "note": "Captured bytes are after any RTK filtering; output bytes are not provider tokens or billing. Use rtk gain for its separate estimates."}, indent=2))
            return 0
        if args.command == "send":
            return send_message(store, args.task_id, args.message)
        if args.command == "_wake":
            return wake_task(store, args.task_id)
        if args.command == "attach":
            return attach(store, args.task_id, lambda message: send_message(store, args.task_id, message),
                          lambda: launch(store, args.task_id, resume=True, changes={}))
        if args.command == "list":
            for state in store.tasks():
                print(f"{state['id']}  {state['status']:10}  {state['provider']:6}  {state['objective'][:100]}")
            return 0
        if args.command == "status":
            state = store.load(args.task_id)
            state["workspace_runner_active"] = store.is_running()
            print(json.dumps(state, indent=2))
            return 0
        if args.command == "stop":
            store.load(args.task_id)
            atomic_write(store.task_dir(args.task_id) / "stop", "Stop requested\n")
            print(f"Stop requested for {args.task_id}; any active process will be terminated")
            return 0
        if args.command in {"start", "chat"}:
            changes = overrides(args)
            changes["verify"] = args.verify
            config = load_config(workspace, changes)
            objective = args.task_file.read_text() if args.task_file else args.objective
            if args.command == "chat":
                if not sys.stdin.isatty():
                    raise ValueError("Chat needs a terminal; use start --detach for scripts")
                if not objective:
                    objective = input(f"Task for {config.provider}: ")
            if not objective.strip():
                raise ValueError("Objective cannot be empty")
            with store.lease():
                state = store.create(objective, config.to_dict(), args.session)
            args.task_id = state["id"]
            print(f"Task: {args.task_id}", flush=True)
            if getattr(args, "enqueue", False):
                return 0
            if args.command == "chat":
                launch(store, args.task_id, resume=False, changes={})
                return attach(store, args.task_id, lambda message: send_message(store, args.task_id, message),
                              lambda: launch(store, args.task_id, resume=True, changes={}))
        else:
            store.load(args.task_id)
        resuming = args.command == "resume" or (args.command == "_worker" and args.resume)
        changes = overrides(args) if resuming else {}
        if getattr(args, "detach", False):
            return launch(store, args.task_id, resume=resuming, changes=changes)
        status = Runner(store, args.task_id).run(resume=resuming, overrides=changes)
        return {"completed": 0, "blocked": 2, "paused": 3}.get(status, 1)
    except (OSError, ValueError, TypeError) as exc:
        print(f"tinkertom: {exc}", file=sys.stderr)
        return 2
