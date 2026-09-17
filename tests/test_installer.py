import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tinkertom.optimization import suite_environment


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('tinkertom_installer', ROOT / 'scripts/install-tools.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_system_install_requires_explicit_choice_before_any_mutation(self):
        with patch.object(installer.sys, 'prefix', '/system'), patch.object(installer.sys, 'base_prefix', '/system'), \
                patch.object(installer.subprocess, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.main([])
            run.assert_not_called()
            self.assertTrue(installer.installation_mode(['--system']).system)
        with patch.object(installer.sys, 'prefix', '/venv'), patch.object(installer.sys, 'base_prefix', '/system'), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(installer.installation_mode([]).system)
            with self.assertRaises(SystemExit):
                installer.installation_mode(['--system'])

    def test_system_pip_override_is_scoped_and_package_install_is_editable(self):
        pins = json.loads((ROOT / 'tools.lock.json').read_text())
        with patch.object(installer.subprocess, 'run') as run:
            installer.install_python_packages(pins, False)
            self.assertNotIn('--break-system-packages', run.call_args.args[0])
            installer.install_python_packages(pins, True)
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [sys.executable, '-m', 'pip', 'install'])
            self.assertIn('--break-system-packages', command)
            self.assertEqual(command[command.index('-e') + 1], str(ROOT))

    def test_launcher_replaces_symlink_without_changing_target_and_runs_without_activation(self):
        with tempfile.TemporaryDirectory(prefix='tt install ') as d:
            directory = Path(d)
            previous = directory / 'previous-script'
            previous.write_text('preserve me\n')
            launcher = directory / 'tinkertom'
            launcher.symlink_to(previous)
            installer.write_launcher(directory, 'tinkertom', 'tinkertom')
            self.assertEqual(previous.read_text(), 'preserve me\n')
            self.assertFalse(launcher.is_symlink())
            env = dict(os.environ, PATH='/usr/bin:/bin')
            env.pop('VIRTUAL_ENV', None)
            result = subprocess.run([str(launcher), '--version'], cwd=directory, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('0.1.0', result.stdout)
            installer.write_launcher(directory, 'tinkertom', 'tinkertom')
            self.assertEqual(previous.read_text(), 'preserve me\n')

    @unittest.skipUnless(shutil.which('zsh'), 'zsh not installed')
    def test_shell_source_uses_clone_location_and_preserves_project_cwd(self):
        with tempfile.TemporaryDirectory(prefix='tt shell ') as d:
            root = Path(d) / 'clone with spaces'
            shell = root / 'shell/tinkertom.zsh'
            shell.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'shell/tinkertom.zsh', shell)
            binary = root / '.tools/bin/tinkertom'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!' + sys.executable + '\nimport os,sys,json\nprint(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:]}))\n')
            binary.chmod(0o755)
            project = Path(d) / 'another project'
            project.mkdir()
            result = subprocess.run(['zsh', '-fc',
                'source "$1"; source "$1"; TinkerTom codex; eval "tinkertom-claude --model sonnet"; eval "tinkertom-codex --model example"',
                'test', str(shell)], cwd=project, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([call['cwd'] for call in calls], [str(project)] * 3)
            self.assertEqual(calls[0]['args'], ['-C', str(project), 'codex'])
            self.assertEqual(calls[1]['args'], ['claude', '--model', 'sonnet'])
            self.assertEqual(calls[2]['args'], ['codex', '--model', 'example'])

    def test_system_runtime_does_not_inject_an_old_project_venv(self):
        with patch('tinkertom.optimization.sys.executable', '/usr/bin/python3'), patch.dict(os.environ, PATH='/usr/local/bin:/usr/bin'):
            paths = suite_environment()['PATH'].split(os.pathsep)
            self.assertEqual(paths[1], '/usr/bin')
            self.assertNotIn(str(ROOT / '.venv/bin'), paths)

    def test_shell_setup_is_idempotent_preserves_user_content_and_updates_moved_clone(self):
        with tempfile.TemporaryDirectory() as d, patch.object(installer.Path, 'home', return_value=Path(d)), \
                contextlib.redirect_stdout(io.StringIO()):
            home = Path(d)
            profile = home / '.zshrc'
            original = '# User settings\nexport EXAMPLE="unchanged"\n'
            profile.write_text(original)
            with patch.object(installer, 'ROOT', home / "clone's directory"):
                installer.configure_shell('zsh')
                first = profile.read_text()
                installer.configure_shell('zsh')
                self.assertEqual(profile.read_text(), first)
            with patch.object(installer, 'ROOT', home / 'moved clone'):
                installer.configure_shell('zsh')
            updated = profile.read_text()
            self.assertTrue(updated.startswith(original))
            self.assertEqual(updated.count('# >>> TinkerTom setup >>>'), 1)
            self.assertIn('moved clone/shell/tinkertom.zsh', updated)
            self.assertNotIn("clone's directory", updated)
            installer.configure_shell('bash')
            self.assertIn('tinkertom.bash', (home / '.bashrc').read_text())
            profile.write_text(original + '# >>> TinkerTom setup >>>\nincomplete\n')
            before = profile.read_text()
            with self.assertRaises(SystemExit):
                installer.configure_shell('zsh')
            self.assertEqual(profile.read_text(), before)

    def test_setup_script_dispatches_system_and_venv_modes_without_touching_host(self):
        with tempfile.TemporaryDirectory(prefix='tt setup ') as d:
            root = Path(d)
            shutil.copyfile(ROOT / 'setup.sh', root / 'setup.sh')
            python = root / 'fixture-python'
            log = root / 'calls.jsonl'
            python.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
with Path(os.environ['TT_SETUP_TEST_LOG']).open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['-m', 'venv']:
    binary = Path(sys.argv[3]) / 'bin/python'
    binary.parent.mkdir(parents=True)
    binary.symlink_to(Path(__file__).resolve())
''')
            python.chmod(0o755)
            env = dict(os.environ, SHELL='/usr/bin/zsh', TT_SETUP_TEST_LOG=str(log))
            command = ['sh', str(root / 'setup.sh'), '--python', str(python)]
            system = subprocess.run([*command, '--system'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(system.returncode, 0, system.stderr)
            self.assertFalse((root / '.venv').exists())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls[-1], [str(root / 'scripts/install-tools.py'), '--system', '--shell', 'zsh'])
            normal = subprocess.run([*command, '--no-shell'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(normal.returncode, 0, normal.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(['-m', 'venv', str(root / '.venv')], calls)
            self.assertEqual(calls[-1], [str(root / 'scripts/install-tools.py'), '--shell', 'none'])
            self.assertTrue((root / '.venv/bin/python').exists())

    @unittest.skipUnless(shutil.which('bash'), 'bash not installed')
    def test_bash_aliases_and_capitalized_launcher_preserve_arguments(self):
        with tempfile.TemporaryDirectory(prefix='tt bash ') as d:
            root = Path(d)
            shell = root / 'shell/tinkertom.bash'
            shell.parent.mkdir()
            shutil.copyfile(ROOT / 'shell/tinkertom.bash', shell)
            binary = root / '.tools/bin/tinkertom'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            binary.chmod(0o755)
            result = subprocess.run(['bash', '-O', 'expand_aliases', '-c',
                'source "$1"; TinkerTom claude; eval "tinkertom-codex --model example"',
                'test', str(shell)], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(calls, [['-C', str(root), 'claude'], ['codex', '--model', 'example']])
