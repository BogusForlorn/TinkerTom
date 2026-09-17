import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tinkertom.config import Config, load_config
from tinkertom.providers import Outcome
from tinkertom.runner import Runner
from tinkertom.storage import Store


class FakeClock:
    def __init__(self):
        self.current = 1000
        self.sleeps = []

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.clock = FakeClock()
        self.calls = []

    def create(self, **config):
        return self.store.create("Implement feature and verify it", Config(**config).to_dict())

    def execute_sequence(self, sequence):
        steps = iter(sequence)

        def execute(argv, **kwargs):
            self.calls.append((argv, kwargs, self.clock.now()))
            step = next(steps)
            if isinstance(step, Outcome):
                return step
            nonce = kwargs["env"]["TINKERTOM_ATTEMPT"]
            report = {"attempt": nonce, "status": step, "summary": "Implemented and tested", "next_steps": [] if step == "complete" else ["next"]}
            kwargs["on_session"]("saved-session")
            return Outcome(session_id="saved-session", success=True, final_text=json.dumps(report), usage={"input_tokens": 10})
        return execute

    def run_task(self, state, sequence, **kwargs):
        runner = Runner(self.store, state["id"], now=self.clock.now, sleep=self.clock.sleep, execute=self.execute_sequence(sequence))
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.run(**kwargs)
        return status, self.store.load(state["id"])

    def test_limit_wait_then_resume_same_session(self):
        state = self.create()
        failure = Outcome(session_id="saved-session", error_kind="rate_limit", reset_at=1100, error="quota")
        status, final = self.run_task(state, [failure, "continue", "complete"])
        self.assertEqual(status, "completed")
        self.assertEqual(self.calls[1][2], 1130)
        self.assertIn("saved-session", self.calls[1][0])
        self.assertEqual(final["turns"], 2)
        self.assertEqual(final["usage"]["input_tokens"], 20)
        self.assertIsNone(final["runner_pid"])

    def test_five_hour_fallback_and_persisted_deadline(self):
        state = self.create(reset_buffer_seconds=0)
        status, final = self.run_task(state, [Outcome(error_kind="rate_limit"), "complete"])
        self.assertEqual(self.calls[1][2], 19000)
        self.assertEqual(status, "completed")
        state = self.create(reset_buffer_seconds=0)
        state.update(status="waiting", next_run_at=20000, session_id="original-session")
        self.store.save(state)
        self.calls = []
        self.run_task(state, ["complete"], resume=True)
        self.assertEqual(self.calls[0][2], 20000)
        self.assertIn("original-session", self.calls[0][0])

    def test_longer_limit_is_never_clamped_to_five_hours(self):
        state = self.create(reset_buffer_seconds=0)
        self.run_task(state, [Outcome(error_kind="rate_limit", reset_at=605800), "complete"])
        self.assertEqual(self.calls[1][2], 605800)

    def test_missing_session_recovers_from_checkpoint(self):
        state = self.create(retry_seconds=1)
        state["session_id"] = "lost-session"
        self.store.save(state)
        (self.store.task_dir(state["id"]) / "checkpoint.md").write_text("Completed database migration. Pending API tests.")
        status, _ = self.run_task(state, [Outcome(error_kind="session_missing"), "complete"])
        self.assertEqual(status, "completed")
        self.assertNotIn("resume", self.calls[1][0])
        self.assertIn("Completed database migration", self.calls[1][1]["prompt"])

    def test_failed_verification_returns_to_agent(self):
        state = self.create(verify=["test -f done"])
        status, final = self.run_task(state, ["complete", Outcome(returncode=1), "complete", Outcome(returncode=0)])
        self.assertEqual(status, "completed")
        self.assertIn("Acceptance command failed", self.calls[2][1]["prompt"])
        self.assertEqual(final["completion_basis"], "acceptance_commands")

    def test_auth_and_repeated_network_errors_block(self):
        state = self.create()
        status, _ = self.run_task(state, [Outcome(error_kind="fatal", error="401")])
        self.assertEqual(status, "blocked")
        state = self.create(max_failures=3, retry_seconds=2)
        self.calls = []
        status, _ = self.run_task(state, [Outcome(error_kind="transient")] * 3)
        self.assertEqual(status, "blocked")
        self.assertEqual([x[2] for x in self.calls], [1000, 1002, 1006])

    def test_stale_report_and_stall_circuit_breaker(self):
        state = self.create(max_stalls=2)
        status, _ = self.run_task(state, [Outcome(success=True, final_text="complete")] * 2)
        self.assertEqual(status, "blocked")
        state = self.create(max_stalls=2)
        status, _ = self.run_task(state, ["continue"] * 3)
        self.assertEqual(status, "blocked")

    def test_stop_marker_prevents_calls(self):
        state = self.create()
        (self.store.task_dir(state["id"]) / "stop").touch()
        status, _ = self.run_task(state, [])
        self.assertEqual(status, "paused")
        self.assertEqual(self.calls, [])

    def test_context_rotation_uses_checkpoint_and_last_report(self):
        state = self.create(max_context_turns=1)
        self.run_task(state, ["continue", "complete"])
        self.assertNotIn("resume", self.calls[1][0])
        self.assertIn("Last turn report", self.calls[1][1]["prompt"])

    def test_manual_provider_switch_carries_task_and_clears_old_model(self):
        state = self.create(model="codex-model")
        state.update(status="paused", session_id="codex-session")
        self.store.save(state)
        status, final = self.run_task(state, ["complete"], resume=True, overrides={"provider": "claude"})
        self.assertEqual(status, "completed")
        self.assertEqual(self.calls[0][0][0], "claude")
        self.assertNotIn("--resume", self.calls[0][0])
        self.assertNotIn("--model", self.calls[0][0])
        self.assertEqual(final["previous_sessions"][0]["session_id"], "codex-session")

    def test_total_turn_budget_can_be_increased_on_resume(self):
        state = self.create(max_turns=1)
        status, final = self.run_task(state, ["continue"])
        self.assertEqual(status, "paused")
        status, _ = self.run_task(final, ["complete"], resume=True, overrides={"max_turns": 2})
        self.assertEqual(status, "completed")

    def test_lease_and_task_path_validation(self):
        with self.store.lease():
            self.assertTrue(self.store.is_running())
            with self.assertRaises(ValueError):
                with self.store.lease():
                    pass
        self.assertFalse(self.store.is_running())
        with self.assertRaises(ValueError):
            self.store.task_dir("../../etc")

    def test_config_rejects_typos_invalid_ranges_and_types(self):
        (self.store.workspace / "tinkertom.toml").write_text("cooldown_second = 4\n")
        with self.assertRaises(ValueError):
            load_config(self.store.workspace)
        for args in ({"max_stalls": 0}, {"cooldown_seconds": float("nan")}, {"max_turns": True}, {"verify": "pytest"}):
            with self.assertRaises(ValueError):
                Config(**args).validate()

    def test_invalid_resume_override_does_not_corrupt_saved_configuration(self):
        state = self.create()
        with self.assertRaises(ValueError):
            Runner(self.store, state["id"]).run(resume=True, overrides={"max_turns": -1})
        self.assertEqual(self.store.load(state["id"])["config"]["max_turns"], 0)

    def test_messages_survive_rate_limit_and_are_acknowledged_only_on_valid_report(self):
        state = self.create(reset_buffer_seconds=0)
        message_id = self.store.send(state["id"], "Also cover IPv6")
        status, final = self.run_task(state, [Outcome(error_kind="rate_limit", reset_at=1100), "complete"])
        self.assertEqual(status, "completed")
        self.assertIn("Also cover IPv6", self.calls[0][1]["prompt"])
        self.assertIn("Also cover IPv6", self.calls[1][1]["prompt"])
        self.assertIn(message_id, final["acknowledged_messages"])
        self.assertEqual(self.calls[1][2], 1100)

    def test_message_arriving_during_verification_prevents_early_completion(self):
        state = self.create(verify=["true"])
        execute = self.execute_sequence(["complete", Outcome(returncode=0), "complete", Outcome(returncode=0)])
        injected = []
        def wrapper(argv, **kwargs):
            if argv[0] == "/bin/sh" and not injected:
                injected.append(self.store.send(state["id"], "Also update the docs"))
            return execute(argv, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            status = Runner(self.store, state["id"], execute=wrapper).run()
        self.assertEqual(status, "completed")
        self.assertEqual(len(self.calls), 4)
        self.assertIn("Also update the docs", self.calls[2][1]["prompt"])

    def test_completed_task_accepts_followup_in_same_session(self):
        state = self.create()
        _, completed = self.run_task(state, ["complete"])
        self.store.send(state["id"], "Add an example")
        self.calls = []
        status, final = self.run_task(completed, ["complete"], resume=True)
        self.assertEqual(status, "completed")
        self.assertIn("saved-session", self.calls[0][0])
        self.assertIn("Add an example", self.calls[0][1]["prompt"])

    def test_resume_prompt_is_shorter_and_keeps_wait_only_policy(self):
        state = self.create()
        runner = Runner(self.store, state["id"])
        initial = runner.prompt(state, Config(), "attempt")
        state.update(session_id="saved", session_turns=1)
        subsequent = runner.prompt(state, Config(), "attempt2")
        self.assertLess(len(subsequent), len(initial) // 2)
        self.assertIn("WAIT ONLY", subsequent)
        self.assertIn("never invoke /usage", subsequent)


if __name__ == "__main__":
    unittest.main()
