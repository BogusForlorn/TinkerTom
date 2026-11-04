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

from tinkertom.cli import doctor
from tinkertom.compact import compact, preview
from tinkertom.hooks import rewrite_event
from tinkertom.navigation import read_file, symbols
from tinkertom.publication import classify_command
from tinkertom.optimization import deduplicate, headroom_available, headroom_compress, rtk_available, suite_environment, provider_environment


class SuiteTests(unittest.TestCase):
    def test_doctor_native_ui_mcp_summary_includes_handover(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = doctor(Path(directory))["native_ui"]["mcp"]
        self.assertEqual(summary, "per-invocation run/read/symbols/wait_status/rubber_duck/code_worker/beads/handover tools")

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

    def test_publication_classifier_denies_mutations_at_command_position(self):
        denied = [
            "git push", "git add .", "/usr/bin/git commit -m x", "git -C /x push", "git --git-dir=/x/.git push",
            "git --git-dir /x --work-tree /y add .", "git --namespace n tag v1", "git -c a=b tag v1",
            "ls && git push", "ls\ngit commit", "bash -c 'git push'", "sh -lc \"git add .\"",
            "env FOO=1 git push", "sudo -u x git reset --hard", "timeout 5 git push", "echo $(git push)",
            'echo "$(git add .)"', "`git push`", "(git push)", "xargs -n1 git push", "eval 'git push'",
            "git fetch", "git pull", "git clone x", "git init", "git st", "git branch foo", "git branch -D x",
            "git tag v1", "git tag -d v1", "git remote add a b", "git remote update", "git config user.name x",
            "git config --add a b", "git stash", "git stash pop", "git checkout x", "git push https://x/y main:z",
            "cargo publish", "twine upload dist/*", "python -m twine upload dist/*", "npm publish",
            "yarn npm publish", "gh pr create", "docker push img", "bash <<EOF\ngit push\nEOF",
        ]
        allowed = [
            "echo git push", "grep 'git push' f", "git log --grep='git push'", "git diff 'git push.py'", "git status",
            "git log -n 3 | head", "git -C /x status", "git tag --list", "git tag -l 'v*'", "git tag", "git remote -v",
            "git remote show origin", "git remote get-url origin", "git config --get user.name", "git config --list",
            "git config get x", "git config show x", "git branch", "git branch --list", "git branch --contains abc",
            "bash -c", "bash -c 'ls'", "ls 2>&1", "git diff --stat", "git stash list", "git reflog", "git worktree list",
            "git ls-remote origin", 'echo "$(git status)"', "command -v git", "git --version",
            "cat > f <<EOF\ngit push\nEOF", "pytest -q && ruff check",
        ]
        for command in denied:
            with self.subTest(denied=command):
                self.assertIsNotNone(classify_command(command))
        for command in allowed:
            with self.subTest(allowed=command):
                self.assertIsNone(classify_command(command))

    def test_hook_denies_publication_without_rewrite_even_when_compression_is_off(self):
        for compression in ("1", "0"):
            with patch.dict(os.environ, {"TINKERTOM_MANAGED": "1", "TINKERTOM_COMMAND_COMPRESSION": compression}):
                result = rewrite_event({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
                output = result["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "PreToolUse")
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertTrue(output["permissionDecisionReason"])
                self.assertNotIn("updatedInput", output)
        with patch.dict(os.environ, {"TINKERTOM_MANAGED": "1", "TINKERTOM_COMMAND_COMPRESSION": "0"}):
            self.assertIsNone(rewrite_event({"tool_name": "Bash", "tool_input": {"command": "git status"}}))

    def test_managed_run_rejects_publication_but_unmanaged_does_not_check(self):
        from tinkertom.mcp import response
        request = {"id": 1, "method": "tools/call", "params": {"name": "run", "arguments": {"command": "git push"}}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_MANAGED": "1"}):
            result = response(request, Path(directory))
            self.assertTrue(result["result"]["isError"])
            self.assertIn("exact terminal commands", result["result"]["content"][0]["text"])

    def test_handover_blocks_worker_and_keeps_read_only_beads_available(self):
        from tinkertom.handover import handover_call
        from tinkertom.mcp import call
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            with patch("tinkertom.beads.beads_call", return_value=("[]", False)):
                output, error = handover_call(workspace, {"action": "begin", "source": "codex", "target": "claude"})
            self.assertFalse(error)
            facts = json.loads(output)
            self.assertEqual(list(facts)[:7], ["provider", "source", "target", "branch", "HEAD", "dirty", "status"])
            with self.assertRaisesRegex(ValueError, "active handover"):
                call("code_worker", {"task": "x", "files": ["x"]}, workspace)
            with patch("tinkertom.beads.beads_call", return_value=("[]", False)):
                self.assertEqual(call("beads", {"action": "list"}, workspace)[0], "[]")

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
