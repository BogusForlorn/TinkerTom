import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .config import Config
from .optimization import rtk_available, provider_environment
from .hooks import settings_file
from .process import run_process
from .providers import command, parse_report
from .storage import Store, atomic_write


def read_bounded(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        data = stream.read(limit * 4 + 1)
    text = data.decode(errors="replace")
    return text[:limit] + ("\n[truncated; read checkpoint file for remainder]" if len(text) > limit else "")


def progress_key(workspace: Path, checkpoint: str, summary: str) -> str:
    digest = hashlib.sha256((checkpoint + summary).encode())
    # Include tracked diffs and untracked file identities. This is a stall heuristic,
    # not proof of semantic progress. Bound output outside model context.
    for args in (["git", "diff", "HEAD", "--", ".", ":(exclude).tinkertom"],
                 ["git", "ls-files", "--others", "--exclude-standard", "--", ".", ":(exclude).tinkertom"]):
        try:
            with subprocess.Popen(args, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                for chunk in iter(lambda: proc.stdout.read(65536), b""):
                    digest.update(chunk)
                proc.wait()
        except OSError:
            pass
    return digest.hexdigest()


class Runner:
    def __init__(self, store: Store, task_id: str, *, now=time.time, sleep=time.sleep, execute=run_process):
        self.store = store
        self.task_id = task_id
        self.directory = store.task_dir(task_id)
        self.now = now
        self.sleep = sleep
        self.execute = execute
        self.stop_requested = False
        self.lease_fd = None

    def stopped(self):
        return self.stop_requested or (self.directory / "stop").exists()

    def transition(self, state: dict, status: str, message: str, **fields):
        state.update(status=status, **fields)
        self.store.save(state)
        self.store.event(self.task_id, status, message=message, **fields)
        print(f"[{self.task_id}] {status}: {message}", flush=True)

    def prompt(self, state: dict, config: Config, attempt: str) -> str:
        checkpoint = read_bounded(self.directory / "checkpoint.md", config.checkpoint_chars)
        if state.get("session_id") and state.get("session_turns", 0) > 0:
            # Native history already contains the objective and instructions. A
            # short suffix avoids replaying them and leaves cached prefixes intact.
            return f"""Continue the existing TinkerTom objective and apply any new user messages below.
Complete remaining work autonomously. Inspect actual state after interruptions.
Update {self.directory / 'checkpoint.md'} after milestones. Use focused reads and
`tinkertom compact --rtk -- COMMAND` for noisy output; --raw preserves exact output.
Quota policy remains WAIT ONLY: never invoke /usage, /usage-credits, /extra-usage,
claim/reset quotas, buy credits, enable overage or switch billing/accounts/providers.
Feedback: {state.get('feedback', '')[-6000:]}
New user messages: {json.dumps(state.get('inflight_messages', []))}
Return ONLY JSON: {{"attempt":"{attempt}","status":"continue","summary":"progress and checks","next_steps":["remaining work"]}}
Use complete only when all requirements/checks pass, with next_steps: []. Use blocked
only for a prerequisite requiring external input, explaining it in summary.
"""
        instructions = f"""You are working under TinkerTom's persistent task supervisor.
Complete the assigned objective autonomously within its scope. Make reasonable
implementation choices. Do not stop merely because one response ended. Follow
repository instructions. Do not start another TinkerTom runner.

Quota policy: WAIT ONLY for the provider's normal reset. Never invoke /usage,
/usage-credits, /extra-usage, a manual reset/claim, or buy credits. Never enable
overage, switch accounts, models, providers or billing routes to bypass a limit.
If a weekly allowance is exhausted, wait for its normal reset too. The supervisor
handles these waits without model calls. Do not change account usage settings.

Objective:
{state['objective']}

Checkpoint file: {self.directory / 'checkpoint.md'}
Update this file after each meaningful milestone, BEFORE expensive work and before
ending this turn. Keep it concise: requirements, completed work, decisions, changed
files, verification results, pending steps, blockers. Never store credentials.
If interrupted mid-tool, inspect actual files/process results before retrying an
operation; an interrupted command may already have made changes.

Use targeted searches/file ranges and focused checks. Avoid repeating exploration
or passing tests without a reason. Keep summaries short. Preserve native session
context; do not reread entire logs unless diagnosing an error.
"""
        instructions += "\nUse the TinkerTom MCP code_worker for bounded implementation on Luna/Sonnet. You own planning, review and acceptance checks. Use rubber_duck for an independent opposite-provider critique at significant milestones. Track durable work and blockers with beads. Helpers are authorized CLI subprocesses; don't launch another supervisor.\n"
        if config.rtk and rtk_available():
            instructions += """
RTK is available: prefer `rtk git status`, `rtk git diff`, `rtk git log`,
`rtk read`, and supported RTK test commands for concise output. Use `rtk --help`
for syntax. Inspect raw output whenever filtering hides relevant details.
"""
        instructions += "\nFor noisy commands use `tinkertom compact --rtk -- <command> <args>` to apply RTK where supported, then local Headroom compression, duplicate-line reduction and bounded output. Full logs and compression metrics are saved. Use --raw when exact output is necessary.\nUse `tinkertom read FILE --start N --end M` for targeted reads, `tinkertom symbols FILE.py` to list Python definitions, and `tinkertom read FILE.py --symbol NAME` to fetch just a definition.\n"
        if state.get("session_id"):
            instructions += "\nContinue your existing session. Read the checkpoint if you need to recover context.\n"
        else:
            instructions += f"\nRecover from this persisted checkpoint:\n{checkpoint}\n"
            if state.get("last_report"):
                instructions += "\nLast turn report:\n" + json.dumps(state["last_report"])[:6000] + "\n"
            if state.get("acknowledged_messages"):
                instructions += f"\nPrior user messages are preserved in {self.directory / 'inbox'}. When recovering a fresh session, read these JSON files in filename order to recover requirements/constraints, including acknowledged messages. Do not repeat already completed actions.\n"
        if state.get("feedback"):
            instructions += f"\nSupervisor feedback:\n{state['feedback'][-6000:]}\n"
        messages = state.get("inflight_messages", [])
        if messages:
            instructions += "\nNew user messages (in order; apply these updates to the task):\n"
            instructions += "\n".join(json.dumps(message) for message in messages) + "\n"
        instructions += "\nAcceptance commands the supervisor will run on completion:\n" + json.dumps(config.verify) + "\n"
        instructions += f"""
Your final response must be ONLY a JSON object with this shape:
{{"attempt":"{attempt}","status":"continue","summary":"What changed and was checked","next_steps":["Next concrete action"]}}
Use status "continue" when work remains. Use "complete" ONLY when the entire
objective is satisfied and relevant checks pass, with next_steps: []. Use "blocked"
only when missing credentials, a necessary user decision, or an external dependency
prevents further useful work; explain precisely in summary. Usage limits are handled
by the supervisor. Never claim completion just to stop the loop.
"""
        return instructions

    def run(self, *, resume: bool = False, overrides: dict | None = None) -> str:
        old_handlers = {}
        with self.store.lease() as lease_fd:
            self.lease_fd = lease_fd
            state = self.store.load(self.task_id)
            if state["status"] == "completed" and not self.store.pending_messages(state):
                return "completed"
            if state["status"] in {"blocked", "paused"} and not resume:
                return state["status"]
            if resume:
                changed = {k: v for k, v in (overrides or {}).items() if v is not None}
                switching = changed.get("provider", state["provider"]) != state["provider"]
                updated_config = dict(state["config"])
                if switching:
                    updated_config.update(model="", executable="")
                updated_config.update(changed)
                updated_config = Config(**updated_config).to_dict()
                (self.directory / "stop").unlink(missing_ok=True)
                state["failure_count"] = state["stall_count"] = 0
                if switching:
                    state.setdefault("previous_sessions", []).append({"provider": state["provider"], "session_id": state["session_id"]})
                    state["session_id"] = None
                    state["session_turns"] = 0
                    state["next_run_at"] = None
                    state["provider"] = changed["provider"]
                state["config"] = updated_config
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: setattr(self, "stop_requested", True))
            state["runner_pid"] = os.getpid()
            atomic_write(self.store.root / "active-task", self.task_id)
            self.store.save(state)
            try:
                return self.loop(state)
            except (OSError, ValueError) as exc:
                self.transition(state, "blocked", str(exc), feedback=str(exc))
                return "blocked"
            finally:
                state["runner_pid"] = None
                self.store.save(state)
                (self.store.root / "active-task").unlink(missing_ok=True)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)

    def loop(self, state: dict) -> str:
        config = Config(**state["config"]).validate()
        while True:
            if self.stopped():
                self.transition(state, "paused", "Stopped; session and checkpoint preserved")
                return "paused"
            deadline = state.get("next_run_at")
            if deadline and deadline > self.now():
                # No CLI, model call or token use while waiting. Persisted wall-clock
                # deadlines survive supervisor restarts and machine reboots.
                self.sleep(min(1, deadline - self.now()))
                continue
            if config.max_turns and state["turns"] >= config.max_turns:
                self.transition(state, "paused", "Configured total turn budget reached")
                return "paused"
            state["next_run_at"] = None
            batch = []
            message_chars = 0
            for message in self.store.pending_messages(state):
                if batch and message_chars + len(message["text"]) > 32000:
                    break
                batch.append(message)
                message_chars += len(message["text"])
            state["inflight_messages"] = batch
            if config.max_context_turns and state["session_turns"] >= config.max_context_turns:
                state["session_id"] = None
                state["session_turns"] = 0
            attempt = uuid.uuid4().hex
            state["attempts"] += 1
            log_dir = self.directory / "attempts" / f"{state['attempts']:06d}"
            log_dir.mkdir(parents=True, exist_ok=True)
            prompt = self.prompt(state, config, attempt)
            atomic_write(log_dir / "prompt.txt", prompt)
            self.transition(state, "running", f"{state['provider']} attempt {state['attempts']}", attempt=attempt)

            def on_session(session_id):
                state["session_id"] = session_id
                self.store.save(state)

            environment = dict(provider_environment(config.provider, config.permissions), TINKERTOM_TASK_DIR=str(self.directory), TINKERTOM_ATTEMPT=attempt,
                               TINKERTOM_MANAGED="1", TINKERTOM_PROVIDER=config.provider, TINKERTOM_RTK=str(int(config.rtk)), TINKERTOM_HEADROOM=str(int(config.headroom)))
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
            argv = command(config, state.get("session_id"))
            from .native import codex_settings, mcp_config
            if config.provider == "codex":
                argv[-1:-1] = codex_settings(self.store.workspace)
            else:
                environment["MCP_TOOL_TIMEOUT"] = "604800000"
                argv += ["--mcp-config", json.dumps({"mcpServers": {"tinkertom": mcp_config(self.store.workspace)}})]
            if config.provider == "claude" and config.claude_hook:
                argv += ["--settings", str(settings_file(self.directory))]
            from .agents import helpers_waiting
            result = self.execute(argv, cwd=self.store.workspace,
                                  prompt=prompt, log_dir=log_dir, timeout=config.turn_timeout_seconds,
                                  stopped=self.stopped, provider=state["provider"], on_session=on_session,
                                  lease_fd=self.lease_fd, env=environment,
                                  timeout_paused=lambda: helpers_waiting(self.store.workspace, attempt))
            if result.session_id:
                state["session_id"] = result.session_id
            for key, value in result.usage.items():
                if type(value) in (int, float):
                    state["usage"][key] = state["usage"].get(key, 0) + value
            atomic_write(log_dir / "result.json", json.dumps(vars(result), indent=2) + "\n")
            if result.interrupted or self.stopped():
                self.transition(state, "paused", "Interrupted; resume will inspect the last checkpoint")
                return "paused"
            if result.error_kind == "rate_limit":
                deadline = max(self.now(), result.reset_at or (self.now() + config.cooldown_seconds)) + config.reset_buffer_seconds
                self.transition(state, "waiting", "Provider usage limit; waiting without model calls",
                                next_run_at=deadline, feedback=result.error)
                continue
            if result.error_kind == "fatal":
                self.transition(state, "blocked", "CLI requires a configuration/authentication fix", feedback=result.error)
                return "blocked"
            if result.error_kind in {"session_missing", "context_full"}:
                state["session_id"] = None
                state["session_turns"] = 0
            if not result.success and result.error_kind != "turn_cap":
                state["failure_count"] += 1
                if state["failure_count"] >= config.max_failures:
                    self.transition(state, "blocked", "Repeated CLI failures; inspect attempt logs", feedback=result.error)
                    return "blocked"
                delay = min(config.retry_seconds * 2 ** (state["failure_count"] - 1), 900)
                self.transition(state, "retrying", result.error[-500:], next_run_at=self.now() + delay, feedback=result.error)
                continue
            state["failure_count"] = 0
            state["turns"] += 1
            state["session_turns"] += 1
            report = parse_report(result.final_text, attempt)
            if not report:
                state["stall_count"] += 1
                state["feedback"] = "No valid final JSON report for the current attempt. Inspect checkpoint, finish remaining work, then return the required JSON."
            else:
                state.setdefault("acknowledged_messages", []).extend(message["id"] for message in state.get("inflight_messages", []))
                state["inflight_messages"] = []
                state["feedback"] = ""
                state["last_report"] = report
                atomic_write(self.directory / "last-report.json", json.dumps(report, indent=2) + "\n")
                self.store.event(self.task_id, "report", message=report["summary"], status=report["status"])
                checkpoint = read_bounded(self.directory / "checkpoint.md", config.checkpoint_chars)
                key = progress_key(self.store.workspace, checkpoint, report["summary"])
                state["stall_count"] = state["stall_count"] + 1 if key == state["last_progress"] else 0
                state["last_progress"] = key
                # Independent fallback handoff if the agent forgot its checkpoint.
                if not state.get("session_id"):
                    state["feedback"] = "Previous turn report: " + json.dumps(report)
                if report["status"] == "blocked":
                    with self.store.mailbox(self.task_id):
                        if not self.store.pending_messages(state):
                            self.transition(state, "blocked", report["summary"], feedback=report["summary"])
                            return "blocked"
                if report["status"] == "complete":
                    self.transition(state, "verifying", "Checking completion criteria")
                    verified = self.verify(state, config, log_dir)
                    if self.stopped():
                        self.transition(state, "paused", "Stopped during verification")
                        return "paused"
                    if verified:
                        with self.store.mailbox(self.task_id):
                            if not self.store.pending_messages(state):
                                self.transition(state, "completed", report["summary"], next_run_at=None,
                                                completion_basis="acceptance_commands" if config.verify else "agent_report")
                                return "completed"
            if state["stall_count"] >= config.max_stalls:
                self.transition(state, "blocked", "Repeated missing reports or unchanged progress", feedback=state["feedback"] or "No observable progress; inspect logs before resuming")
                return "blocked"
            self.transition(state, "pending", "Continuing unfinished work")

    def verify(self, state: dict, config: Config, log_dir: Path) -> bool:
        for index, cmd in enumerate(config.verify):
            path = log_dir / f"verify-{index + 1}"
            result = self.execute(["/bin/sh", "-c", cmd], cwd=self.store.workspace, prompt="", log_dir=path,
                                  timeout=config.verify_timeout_seconds, stopped=self.stopped, lease_fd=self.lease_fd)
            if result.returncode != 0 or result.error_kind or result.interrupted:
                state["feedback"] = f"Acceptance command failed: {cmd}\nRead full logs in {path}\n" + read_bounded(path / "stdout.log", 2500) + read_bounded(path / "stderr.log", 2500)
                self.store.save(state)
                return False
        return True
