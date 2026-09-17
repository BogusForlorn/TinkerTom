import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tinkertom.compact import compact
from tinkertom.config import Config
from tinkertom.process import run_process
from tinkertom.runner import Runner
from tinkertom.storage import Store


ROOT = Path(__file__).resolve().parent.parent


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = Store(self.workspace)
        self.executable = self.workspace / "fake-agent"
        shutil.copy(ROOT / "tests" / "fake_cli.py", self.executable)
        self.executable.chmod(0o755)

    def scenario(self, provider, steps, **options):
        (self.workspace / "scenario.json").write_text(json.dumps(dict(provider=provider, steps=steps, **options)))
        return self.store.create("Implement a feature", Config(provider=provider, executable=str(self.executable), reset_buffer_seconds=0,
                                 verify=["test -f feature.txt"], retry_seconds=0.05, turn_timeout_seconds=5).to_dict())

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "tinkertom", "-C", str(self.workspace), *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=10)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.025)
        self.fail("Timed out waiting for fixture process")

    def test_codex_full_loop_large_output_and_acceptance_repair(self):
        state = self.scenario("codex", ["rate_limit", "premature_complete", "continue", "complete"], huge_output=True)
        with contextlib.redirect_stdout(io.StringIO()):
            status = Runner(self.store, state["id"]).run()
        self.assertEqual(status, "completed")
        final = self.store.load(state["id"])
        self.assertEqual(final["attempts"], 4)
        self.assertEqual(final["usage"]["input_tokens"], 30)
        calls = [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]
        self.assertIn("fixture-session", calls[1]["argv"])
        self.assertIn("Acceptance command failed", calls[2]["prompt"])
        self.assertGreater((self.store.task_dir(state["id"]) / "attempts/000004/stdout.log").stat().st_size, 2200000)

    def test_claude_full_loop_preserves_session(self):
        state = self.scenario("claude", ["rate_limit", "continue", "complete"])
        result = self.cli("resume", state["id"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final = self.store.load(state["id"])
        self.assertEqual(final["status"], "completed")
        calls = [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]
        args = calls[1]["argv"]
        self.assertEqual(args[args.index("--resume") + 1], "fixture-session")
        self.assertIn("--settings", args)

    def test_detach_stop_and_resume_preserves_deadline(self):
        state = self.scenario("codex", ["rate_limit", "complete"], wait=1.5)
        result = self.cli("resume", state["id"], "--detach")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: self.store.load(state["id"])["status"] == "waiting")
        deadline = self.store.load(state["id"])["next_run_at"]
        self.assertEqual(self.cli("stop", state["id"]).returncode, 0)
        self.wait_for(lambda: self.store.load(state["id"])["status"] == "paused" and not self.store.is_running())
        self.assertEqual((self.workspace / "call-count").read_text(), "1")
        result = self.cli("resume", state["id"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertGreaterEqual(time.time(), deadline)

    def test_killed_supervisor_recovers_from_durable_wait_state(self):
        state = self.scenario("claude", ["rate_limit", "complete"], wait=0.5)
        proc = subprocess.Popen([sys.executable, "-m", "tinkertom", "-C", str(self.workspace), "resume", state["id"]],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_for(lambda: self.store.load(state["id"])["status"] == "waiting")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            saved = self.store.load(state["id"])
            self.assertEqual(saved["session_id"], "fixture-session")
            result = self.cli("resume", state["id"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_session_is_durable_before_process_exits_and_stop_kills_it(self):
        state = self.scenario("codex", ["sleep"])
        runner = Runner(self.store, state["id"])
        task_path = self.store.task_dir(state["id"])

        def stop_after_session():
            current = self.store.load(state["id"])
            return bool(current["session_id"])

        runner.stopped = stop_after_session
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.run()
        self.assertEqual(status, "paused")
        self.assertEqual(self.store.load(state["id"])["session_id"], "fixture-session")
        self.assertTrue((task_path / "attempts/000001/stdout.log").exists())
        self.assertFalse(self.store.is_running())

    def test_timeout_and_nonzero_compact_preserve_output_and_exit_code(self):
        result = run_process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.workspace,
                             prompt="", log_dir=self.workspace / "timeout", timeout=0.15, stopped=lambda: False)
        self.assertIn("timed out", result.error)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = compact([sys.executable, "-c", "import sys; print('A'*20000); sys.exit(7)"], self.workspace, max_bytes=1000)
        self.assertEqual(code, 7)
        self.assertIn("bytes omitted", stdout.getvalue())
        self.assertLess(len(stdout.getvalue()), 1500)
        logs = list((self.workspace / ".tinkertom/command-logs").glob("*/stdout.log"))
        self.assertEqual(logs[0].stat().st_size, 20001)

    def test_process_closing_pipes_still_has_timeout(self):
        result = run_process([sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(60)"], cwd=self.workspace,
                             prompt="", log_dir=self.workspace / "closed-pipes", timeout=0.15, stopped=lambda: False)
        self.assertIn("timed out", result.error)

    def test_cli_task_file_and_existing_init_protection(self):
        self.scenario("codex", ["complete"])
        objective = self.workspace / "task.md"
        objective.write_text("Implement feature")
        result = self.cli("start", "--task-file", str(objective), "--executable", str(self.executable), "--verify", "test -f feature.txt")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.cli("init", "--permissions", "yolo").returncode, 0)
        self.assertEqual(self.cli("init").returncode, 2)
        self.assertIn('permissions = "yolo"', (self.workspace / "tinkertom.toml").read_text())

    def test_enqueue_creates_pending_task_without_invoking_a_provider(self):
        result = self.cli("start", "Queued task", "--executable", "/does/not/exist", "--enqueue")
        self.assertEqual(result.returncode, 0, result.stderr)
        tasks = self.store.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "pending")
        self.assertEqual(tasks[0]["attempts"], 0)

    def test_send_to_completed_task_restarts_native_session(self):
        state = self.scenario("codex", ["complete", "complete"])
        self.assertEqual(self.cli("resume", state["id"]).returncode, 0)
        result = self.cli("send", state["id"], "Also document the feature")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: self.store.load(state["id"])["turns"] == 2 and not self.store.is_running())
        calls = [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]
        self.assertIn("Also document the feature", calls[1]["prompt"])
        self.assertIn("fixture-session", calls[1]["argv"])

    def test_send_during_detached_cooldown_does_not_wake_provider_early(self):
        state = self.scenario("claude", ["rate_limit", "complete"], wait=1.5)
        self.assertEqual(self.cli("resume", state["id"], "--detach").returncode, 0)
        self.wait_for(lambda: self.store.load(state["id"])["status"] == "waiting")
        result = self.cli("send", state["id"], "Preserve compatibility")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "call-count").read_text(), "1")
        self.wait_for(lambda: self.store.load(state["id"])["status"] == "completed" and not self.store.is_running())
        calls = [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]
        self.assertIn("Preserve compatibility", calls[1]["prompt"])

    def test_zsh_chat_queues_followup_and_detaches_while_worker_continues(self):
        self.scenario("codex", ["rate_limit", "complete"], wait=2)
        (self.workspace / "tinkertom.toml").write_text("reset_buffer_seconds = 0\n")
        master, slave = pty.openpty()
        env = dict(os.environ, PROMPT_TOOLKIT_NO_CPR="1", TERM="xterm-256color")
        proc = subprocess.Popen(["zsh", "-fc", 'source "$1"; cd -- "$2"; TinkerTom chat --provider codex "Implement feature" --executable "$3"',
                                 "test", str(ROOT / "shell/tinkertom.zsh"), str(self.workspace), str(self.executable)],
                                stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        transcript = bytearray()
        def wait_text(text):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if text in transcript:
                    return
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        transcript.extend(os.read(master, 65536))
                    except OSError:
                        break
            self.fail(f"Did not find {text!r} in terminal output: {bytes(transcript)!r}")
        try:
            wait_text(b"Current status:")
            os.write(master, b"Also add documentation\n")
            wait_text(b"Queued for the next available turn.")
            os.write(master, b"/detach\n")
            wait_text(b"Detached; worker continues.")
            self.assertEqual(proc.wait(timeout=5), 0)
            self.wait_for(lambda: not self.store.is_running(), timeout=8)
            active = [s for s in self.store.tasks() if s["attempts"] > 0]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["status"], "completed")
            calls = [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]
            self.assertIn("Also add documentation", calls[-1]["prompt"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
