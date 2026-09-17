import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tinkertom.compact import compact, preview
from tinkertom.hooks import rewrite_event
from tinkertom.navigation import read_file, symbols
from tinkertom.optimization import deduplicate, headroom_available, headroom_compress, rtk_available, suite_environment, provider_environment


class SuiteTests(unittest.TestCase):
    def test_root_claude_yolo_environment_is_scoped_to_provider_launches(self):
        with patch.dict(os.environ), patch('tinkertom.optimization.os.geteuid', return_value=0):
            os.environ.pop('IS_SANDBOX', None)
            self.assertEqual(provider_environment('claude', 'yolo')['IS_SANDBOX'], '1')
            self.assertNotIn('IS_SANDBOX', provider_environment('claude', 'standard'))
            self.assertNotIn('IS_SANDBOX', provider_environment('codex', 'yolo'))
            self.assertNotIn('IS_SANDBOX', os.environ)

    def test_dedup_preserves_counts_and_unique_errors(self):
        output = deduplicate("ok\n" * 100 + "FATAL unique error\n")
        self.assertIn("100 times total", output)
        self.assertIn("FATAL unique error", output)
        self.assertLess(len(output), 100)

    def test_bounded_preview_retains_failure_in_middle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            path.write_text("ordinary output\n" * 500 + "FATAL unique integrity error\n" + "ordinary output\n" * 500)
            output = preview(path, 1000)
            self.assertIn("FATAL unique integrity error", output)
            self.assertLess(len(output), 1400)

    def test_python_symbols_and_exact_decorated_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.py"
            path.write_text("import os\n\n@decorator\ndef wanted():\n    return 42\n\ndef unrelated():\n    pass\n")
            self.assertIn("4-5: FunctionDef wanted", symbols(path))
            output = read_file(path, symbol="wanted")
            self.assertIn("3: @decorator", output)
            self.assertIn("return 42", output)
            self.assertNotIn("unrelated", output)

    def test_hook_keeps_shell_quoting_and_does_not_wrap_compounds(self):
        with patch.dict(os.environ, {"TINKERTOM_MANAGED": "1"}):
            command = 'ls "directory with spaces" *.py'
            result = rewrite_event({"tool_name": "Bash", "tool_input": {"command": command, "timeout": 1000}})
            updated = result["hookSpecificOutput"]["updatedInput"]
            self.assertEqual(shlex.split(updated["command"])[-2:], ["--shell", command])
            self.assertEqual(updated["timeout"], 1000)
            for unsafe in ("cd somewhere && ls", "export X=1", "cat file | jq .", 'echo "$(git status)"', "tinkertom compact -- ls", "rtk git status"):
                self.assertIsNone(rewrite_event({"tool_name": "Bash", "tool_input": {"command": unsafe}}))

    def test_hook_respects_disabled_optimizers(self):
        with patch.dict(os.environ, {"TINKERTOM_MANAGED": "1", "TINKERTOM_RTK": "0", "TINKERTOM_HEADROOM": "0"}):
            result = rewrite_event({"tool_name": "Bash", "tool_input": {"command": "git status"}})
            command = result["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertNotIn("--rtk", command)
            self.assertIn("--no-headroom", command)

    def test_raw_mode_is_exact_and_preserves_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = compact([sys.executable, "-c", "import sys; print('raw'); sys.exit(9)"], Path(directory), raw=True)
            self.assertEqual(result, 9)
            self.assertEqual(stdout.getvalue(), "raw\n")
            self.assertEqual(stderr.getvalue(), "")

    @unittest.skipUnless(headroom_available(), "Headroom optional suite not installed")
    def test_real_headroom_compresses_json_without_network_and_preserves_anomaly(self):
        rows = [{"id": i, "status": "ok", "latency": 20, "message": "Request completed successfully"} for i in range(200)]
        rows[123].update(status="FATAL", message="unique database integrity error", latency=9999)
        original = json.dumps(rows)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Compression must be local")):
            compressed, metrics = headroom_compress(original)
        self.assertTrue(metrics["applied"])
        self.assertLess(len(compressed), len(original))
        self.assertIn("unique database integrity error", compressed)

    @unittest.skipUnless(rtk_available(), "RTK optional suite not installed")
    def test_real_rtk_wrapper_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = compact(["git", "status"], workspace, use_rtk=True, headroom=False)
            self.assertEqual(result, 0)
            metrics = json.loads(next(workspace.glob(".tinkertom/command-logs/*/metrics.json")).read_text())
            self.assertEqual(metrics["rtk_command"], "rtk git status")
            self.assertGreater(metrics["stdout"]["raw_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
