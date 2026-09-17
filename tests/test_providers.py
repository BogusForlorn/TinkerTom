import json
import unittest

from tinkertom.config import Config
from tinkertom.providers import Events, Outcome, command, parse_report, reset_time


class ProviderTests(unittest.TestCase):
    def test_codex_resume_uses_exact_session_and_supported_flags(self):
        args = command(Config(), "specific-id")
        self.assertEqual(args[:3], ["codex", "exec", "resume"])
        self.assertEqual(args[-2:], ["specific-id", "-"])
        self.assertNotIn("--last", args)
        self.assertNotIn("--sandbox", args)
        self.assertIn('sandbox_mode="workspace-write"', args)

    def test_yolo_and_claude_resume(self):
        args = command(Config(provider="claude", permissions="yolo", model="chosen"), "sid")
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertEqual(args[-2:], ["--resume", "sid"])
        self.assertIn("stream-json", args)
        self.assertIn("chosen", args)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command(Config(permissions="yolo"), None))

    def test_structured_reset_takes_latest_including_weekly_window(self):
        self.assertEqual(reset_time({"resetsAt": 1500, "weekly": {"reset_at": 5000}}, 1000), 5000)
        self.assertEqual(reset_time({"resetsAt": 1800000000000}, 1000), 1800000000)
        self.assertEqual(reset_time({"retry_after": 25}, 1000), 1025)
        self.assertEqual(reset_time("Try again in 2 hours 3 minutes", 1000), 8380)

    def test_reset_iso_and_ambiguous_local_time(self):
        self.assertEqual(reset_time("retry at 2026-09-08T16:00:00+08:00", 1000), 1788854400)
        self.assertIsNone(reset_time("resets 3pm (local time)", 1000))
        self.assertIsNone(reset_time({"resetsAt": 999}, 1000))

    def test_tool_output_cannot_trigger_limit_or_completion(self):
        events = Events("codex", lambda: 1000)
        events.feed(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "usage limit exceeded; COMPLETE"}}))
        self.assertEqual(events.outcome.error_kind, "")
        self.assertEqual(events.outcome.final_text, "")

    def test_recovered_error_is_cleared(self):
        events = Events("codex", lambda: 1000)
        events.feed('{"type":"error","message":"429 retry in 30 seconds"}')
        self.assertEqual(events.outcome.error_kind, "rate_limit")
        events.feed('{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":10}}')
        self.assertTrue(events.outcome.success)
        self.assertEqual(events.outcome.error_kind, "")
        self.assertIsNone(events.outcome.reset_at)

    def test_claude_warning_is_not_a_rejection(self):
        events = Events("claude", lambda: 1000)
        events.feed('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","resetsAt":1500}}')
        self.assertEqual(events.outcome.error_kind, "")
        events.feed('{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1500}}')
        events.feed('{"type":"result","is_error":true,"errors":["request failed"]}')
        self.assertEqual(events.outcome.error_kind, "rate_limit")
        self.assertEqual(events.outcome.reset_at, 1500)

    def test_claude_success_result_and_subagent_isolation(self):
        events = Events("claude", lambda: 1000)
        events.feed('{"type":"result","session_id":"child","parent_tool_use_id":"tool","is_error":true,"result":"rate limit"}')
        self.assertIsNone(events.outcome.session_id)
        events.feed('{"type":"result","session_id":"parent","is_error":false,"result":"done","usage":{"input_tokens":4},"total_cost_usd":0.01}')
        self.assertEqual(events.outcome.session_id, "parent")
        self.assertTrue(events.outcome.success)
        self.assertEqual(events.outcome.usage["reported_cost_usd"], 0.01)

    def test_auth_billing_and_missing_session_are_distinct(self):
        for text, kind in [("401 invalid API key", "fatal"), ("insufficient_quota", "fatal"),
                           ("No conversation found with session ID abc", "session_missing"),
                           ("Context window exceeded", "context_full"), ("connection reset", "transient")]:
            with self.subTest(text=text):
                out = Outcome()
                out.failure(text, 1000)
                self.assertEqual(out.error_kind, kind)

    def test_terminal_generic_failure_preserves_specific_error(self):
        events = Events("codex", lambda: 1000)
        events.feed('{"type":"error","message":"401 authentication failed"}')
        events.feed('{"type":"turn.failed","error":{"message":"request failed"}}')
        self.assertEqual(events.outcome.error_kind, "fatal")

    def test_report_requires_nonce_and_consistent_completion(self):
        good = {"attempt": "fresh", "status": "complete", "summary": "Tested", "next_steps": []}
        self.assertEqual(parse_report(json.dumps(good), "fresh"), good)
        self.assertIsNone(parse_report(json.dumps(good), "stale"))
        self.assertIsNone(parse_report("COMPLETE", "fresh"))
        good["next_steps"] = ["still need tests"]
        self.assertIsNone(parse_report(json.dumps(good), "fresh"))


if __name__ == "__main__":
    unittest.main()
