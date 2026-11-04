import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tinkertom.config import Config
from tinkertom.handover import _bead_candidates, _git_facts, handover_call, handover_context
from tinkertom.mcp import available_tools, call


def no_beads_config(**changes):
    return Config(**{"beads": False, **changes}).validate()


class HandoverTests(unittest.TestCase):
    def test_completed_target_context_is_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / ".tinkertom"
            root.mkdir()
            (root / "handover.json").write_text(json.dumps({
                "provider": "codex", "source": "codex", "target": "claude",
                "state": "completed", "expires_at": 9999999999,
            }))
            (root / "checkpoint.md").write_text("checkpoint\n" * 10000)
            context = handover_context(workspace, "claude")
            self.assertIn("TinkerTom handover checkpoint", context)
            self.assertIn("truncated", context)
            self.assertLessEqual(len(context), 12000 + len("\n\nTinkerTom handover checkpoint for this target provider:\n"))
            self.assertTrue((root / "handover.json").exists())

    def test_target_mismatch_expiry_and_symlinks_are_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / ".tinkertom"
            root.mkdir()
            marker = {"provider": "codex", "source": "codex", "target": "claude",
                      "state": "completed", "expires_at": 9999999999}
            (root / "handover.json").write_text(json.dumps(marker))
            (root / "checkpoint.md").write_text("checkpoint")
            self.assertEqual(handover_context(workspace, "codex"), "")
            marker["expires_at"] = 0
            (root / "handover.json").write_text(json.dumps(marker))
            self.assertEqual(handover_context(workspace, "claude"), "")
            marker["expires_at"] = 9999999999
            (root / "handover.json").unlink()
            (root / "handover.json").symlink_to(workspace / "missing-marker")
            self.assertEqual(handover_context(workspace, "claude"), "")
            (root / "handover.json").unlink()
            (root / "handover.json").write_text(json.dumps(marker))
            (root / "checkpoint.md").unlink()
            (root / "checkpoint.md").symlink_to(workspace / "outside-checkpoint")
            self.assertEqual(handover_context(workspace, "claude"), "")

    def test_provider_derivation_and_opposite_provider_takeover(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            with patch("tinkertom.agents.active_config", return_value=no_beads_config()):
                result, error = handover_call(workspace, {"action": "begin"})
            self.assertFalse(error)
            facts = json.loads(result)
            self.assertEqual((facts["source"], facts["target"]), ("codex", "claude"))
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=no_beads_config()):
                result, error = handover_call(workspace, {"action": "finalize", "checkpoint": "done"})
            self.assertFalse(error)
            self.assertEqual(json.loads(result)["state"], "completed")
            checkpoint = (workspace / ".tinkertom/checkpoint.md").read_text()
            self.assertIn("- Source: codex", checkpoint)
            self.assertIn("- Target: claude", checkpoint)
            with self.assertRaisesRegex(ValueError, "active handover"):
                call("code_worker", {"task": "x", "files": ["x"]}, workspace)
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "claude"}), \
                    patch("tinkertom.agents.active_config", return_value=no_beads_config()):
                result, error = handover_call(workspace, {"action": "begin"})
            self.assertFalse(error)
            self.assertEqual((json.loads(result)["source"], json.loads(result)["target"]), ("claude", "codex"))

    def test_checkpoint_is_plain_markdown_verified_first_and_uses_fresh_dirty_facts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            with patch("tinkertom.agents.active_config", return_value=no_beads_config()):
                handover_call(workspace, {"action": "begin"})
            (workspace / "new.txt").write_text("uncommitted")
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=no_beads_config()):
                handover_call(workspace, {"action": "finalize", "checkpoint": "narrative"})
            checkpoint = (workspace / ".tinkertom/checkpoint.md").read_text()
            self.assertTrue(checkpoint.startswith("# Handover checkpoint\n\n## Verified facts\n"))
            self.assertIn("- Dirty: yes", checkpoint)
            self.assertIn("new.txt", checkpoint)

    def test_oversize_complete_checkpoint_does_not_append_notes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            config = no_beads_config(beads=True, checkpoint_chars=100)
            with patch("tinkertom.agents.active_config", return_value=config):
                handover_call(workspace, {"action": "begin"})
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=config), \
                    patch("tinkertom.handover._bead_status") as status, \
                    patch("tinkertom.beads.append_bead_notes") as append:
                with self.assertRaisesRegex(ValueError, "complete checkpoint"):
                    handover_call(workspace, {"action": "finalize", "checkpoint": "x", "bead_id": "tt-1", "notes": "note"})
            status.assert_not_called()
            append.assert_not_called()

    def test_only_in_progress_candidates_are_returned(self):
        payload = json.dumps({"items": [
            {"id": "open", "status": "open"},
            {"id": "doing", "status": "in_progress"},
            {"id": "done", "status": "closed"},
        ]})
        with tempfile.TemporaryDirectory() as directory, patch("tinkertom.beads.beads_call", return_value=(payload, False)):
            result = _bead_candidates(Path(directory), True)
        self.assertEqual([item["id"] for item in result["items"]], ["doing"])

    def test_selected_bead_must_be_in_progress(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            config = no_beads_config(beads=True)
            with patch("tinkertom.agents.active_config", return_value=config):
                handover_call(workspace, {"action": "begin"})
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=config), \
                    patch("tinkertom.handover._bead_status", return_value={"id": "x", "status": "open"}), \
                    patch("tinkertom.beads.append_bead_notes") as append:
                with self.assertRaisesRegex(ValueError, "in_progress"):
                    handover_call(workspace, {"action": "finalize", "checkpoint": "ok", "bead_id": "x", "notes": "note"})
            append.assert_not_called()

    def test_notes_command_is_append_only(self):
        from tinkertom.beads import append_bead_notes
        with tempfile.TemporaryDirectory() as directory, \
                patch("tinkertom.beads.shutil.which", return_value="bd"), \
                patch("tinkertom.beads.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            append_bead_notes(Path(directory), "tt-1", "checkpoint note")
        command = run.call_args.args[0]
        self.assertIn("--append-notes", command)
        self.assertNotIn("--status", command)
        self.assertNotIn("close", command)

    def test_active_marker_blocks_mutations_but_allows_read_only_beads(self):
        from tinkertom.handover import begin
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            with patch("tinkertom.agents.active_config", return_value=Config().validate()), \
                    patch("tinkertom.beads.beads_call", return_value=("[]", False)):
                begin(workspace, {})
            with self.assertRaisesRegex(ValueError, "active handover"):
                call("code_worker", {"task": "x", "files": ["x"]}, workspace)
            with self.assertRaisesRegex(ValueError, "active handover"):
                call("beads", {"action": "update", "id": "x", "status": "open"}, workspace)
            with patch("tinkertom.beads.beads_call", return_value=("[]", False)):
                self.assertEqual(call("beads", {"action": "list"}, workspace)[0], "[]")

    def test_helper_excludes_handover_even_when_other_config_allows_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_HELPER": "1"}):
            names = {tool["name"] for tool in available_tools(Path(directory))}
        self.assertNotIn("handover", names)

    def test_beads_disabled_allows_checkpoint_only_and_rejects_bead_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            config = no_beads_config()
            with patch("tinkertom.agents.active_config", return_value=config):
                handover_call(workspace, {"action": "begin"})
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=config):
                with self.assertRaisesRegex(ValueError, "Beads are disabled"):
                    handover_call(workspace, {"action": "finalize", "checkpoint": "ok", "bead_id": "x", "notes": "n"})

    def test_expiry_and_abort(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            config = no_beads_config()
            with patch("tinkertom.agents.active_config", return_value=config):
                handover_call(workspace, {"action": "begin", "ttl_seconds": 1})
            marker = json.loads((workspace / ".tinkertom/handover.json").read_text())
            marker["expires_at"] = 0
            (workspace / ".tinkertom/handover.json").write_text(json.dumps(marker))
            with self.assertRaisesRegex(ValueError, "unexpired"):
                handover_call(workspace, {"action": "abort"})
            marker["expires_at"] = 9999999999
            (workspace / ".tinkertom/handover.json").write_text(json.dumps(marker))
            result, error = handover_call(workspace, {"action": "abort"})
            self.assertFalse(error)
            self.assertEqual(json.loads(result)["state"], "aborted")

    def test_checkpoint_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            workspace = Path(directory)
            config = no_beads_config()
            with patch("tinkertom.agents.active_config", return_value=config):
                handover_call(workspace, {"action": "begin"})
            (workspace / ".tinkertom/checkpoint.md").symlink_to(workspace / "outside")
            with patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}), \
                    patch("tinkertom.agents.active_config", return_value=config):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    handover_call(workspace, {"action": "finalize", "checkpoint": "ok"})

    def test_git_facts_cover_non_git_unborn_and_detached(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TINKERTOM_PROVIDER": "codex"}):
            plain = Path(directory) / "plain"
            plain.mkdir()
            self.assertEqual(_git_facts(plain, "codex", "claude")["branch"], "(non-git)")
            unborn = Path(directory) / "unborn"
            unborn.mkdir()
            subprocess.run(["git", "init", "-q", str(unborn)], check=True)
            facts = _git_facts(unborn, "codex", "claude")
            self.assertEqual(facts["HEAD"], "(unborn)")
            detached = Path(directory) / "detached"
            detached.mkdir()
            subprocess.run(["git", "init", "-q", str(detached)], check=True)
            (detached / "f").write_text("x")
            subprocess.run(["git", "-C", str(detached), "add", "f"], check=True)
            subprocess.run(["git", "-C", str(detached), "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
            subprocess.run(["git", "-C", str(detached), "checkout", "-q", "--detach"], check=True)
            self.assertEqual(_git_facts(detached, "codex", "claude")["branch"], "(detached)")


if __name__ == "__main__":
    unittest.main()
