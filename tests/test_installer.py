import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from tinkertom.optimization import suite_environment


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('tinkertom_installer', ROOT / 'scripts/install-tools.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_platform_aliases_and_release_matrix(self):
        pins = json.loads((ROOT / 'tools.lock.json').read_text())
        for system, machine, target in (
            ('Darwin', 'arm64', 'darwin-arm64'), ('Darwin', 'x86_64', 'darwin-x86_64'),
            ('Linux', 'aarch64', 'linux-arm64'), ('Linux', 'x86_64', 'linux-x86_64'),
            ('Linux', 'AMD64', 'linux-x86_64'), ('Darwin', 'aarch64', 'darwin-arm64'),
        ):
            self.assertEqual(installer.platform_key(system, machine), target)
            with patch.object(installer.platform, 'system', return_value=system), \
                    patch.object(installer.platform, 'machine', return_value=machine):
                self.assertEqual(installer.platform_key(), target)
            for tool in ('rtk', 'beads'):
                pin = pins[tool]['binaries'][target]
                self.assertTrue(pin['binary_url'].startswith('https://github.com/'))
                for key in ('archive_sha256', 'binary_sha256'):
                    self.assertRegex(pin[key], r'^[0-9a-f]{64}$')
        for system, machine in (('Windows', 'AMD64'), ('Linux', 'armv7l'), ('FreeBSD', 'x86_64')):
            with self.assertRaises(SystemExit):
                installer.platform_key(system, machine)

    def test_binary_install_checks_both_hashes_preserves_old_file_and_repairs_mode(self):
        contents = b'fixture executable'
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode='w:gz') as archive:
            member = tarfile.TarInfo('release/rtk')
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
        data = archive_bytes.getvalue()
        pin = {'binary_url': 'https://example.invalid/rtk.tar.gz',
               'archive_sha256': hashlib.sha256(data).hexdigest(),
               'binary_sha256': hashlib.sha256(contents).hexdigest()}
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            binary = directory / 'rtk'
            binary.write_bytes(b'previous executable')
            for key in ('archive_sha256', 'binary_sha256'):
                with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                    with self.assertRaisesRegex(SystemExit, 'checksum mismatch'):
                        installer.install_binary(directory, 'rtk', dict(pin, **{key: '0' * 64}))
                self.assertEqual(binary.read_bytes(), b'previous executable')
            with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                installer.install_binary(directory, 'rtk', pin)
            self.assertEqual(binary.read_bytes(), contents)
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            binary.chmod(0o644)
            with patch.object(installer.urllib.request, 'urlopen') as download:
                installer.install_binary(directory, 'rtk', pin)
                download.assert_not_called()
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            # Replacing an old install must not follow its symlink.
            original = directory / 'original'
            binary.rename(original)
            binary.symlink_to(original)
            with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                installer.install_binary(directory, 'rtk', pin)
            self.assertFalse(binary.is_symlink())
            self.assertEqual(original.read_bytes(), contents)

    def test_main_selects_each_platform_without_source_builds(self):
        pins = json.loads((ROOT / 'tools.lock.json').read_text())
        for target in ('darwin-arm64', 'darwin-x86_64', 'linux-arm64', 'linux-x86_64'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'tools.lock.json').write_text(json.dumps(pins))
                # Avoid network for the optional tokenizer cache.
                cache = root / '.tools/tokenizer-cache'
                cache.mkdir(parents=True)
                url = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
                (cache / hashlib.sha1(url.encode()).hexdigest()).touch()
                with patch.object(installer, 'ROOT', root), \
                        patch.object(installer.sys, 'prefix', '/venv'), \
                        patch.object(installer.sys, 'base_prefix', '/base'), \
                        patch.object(installer.subprocess, 'run') as run, \
                        patch.object(installer.subprocess, 'check_output', side_effect=[pins[k]['commit'] for k in ('rtk', 'headroom', 'beads')]), \
                        patch.object(installer, 'install_binary') as binary, \
                        patch.object(installer, 'install_python_packages') as packages, \
                        patch.object(installer.platform, 'mac_ver', return_value=('26.0', (), '')), \
                        patch.object(installer, 'write_launcher'), \
                        patch.object(installer, 'configure_shell'), contextlib.redirect_stdout(io.StringIO()):
                    installer.main(['--platform', target])
                self.assertEqual([c.args[0][0] for c in run.call_args_list], ['git'] * 3)
                self.assertEqual(binary.call_args_list[0].args, (root / '.tools/bin', 'rtk', pins['rtk']['binaries'][target]))
                self.assertEqual(binary.call_args_list[1].args, (root / '.tools/bin', 'bd', pins['beads']['binaries'][target]))
                packages.assert_called_once_with(pins, False)

    def test_macos_minimum_selects_build_only_on_older_releases(self):
        pin = {'minimum_macos': '26.0'}
        for version, expected in (('15.7.1', True), ('14.0', True), ('26', False), ('26.0', False), ('27.1', False)):
            self.assertEqual(installer.needs_macos_build('darwin-arm64', pin, version), expected)
        self.assertFalse(installer.needs_macos_build('linux-arm64', pin, '15.0'))
        self.assertFalse(installer.needs_macos_build('darwin-arm64', {}, '15.0'))
        with patch.object(installer.platform, 'mac_ver', return_value=('', (), '')):
            with self.assertRaises(SystemExit):
                installer.needs_macos_build('darwin-arm64', pin)

    def test_macos_source_build_preserves_embedded_backend_and_reuses_verified_receipt(self):
        pins = json.loads((ROOT / 'tools.lock.json').read_text())
        for target, arch in (('darwin-arm64', 'arm64'), ('darwin-x86_64', 'amd64')):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as d:
                directory = Path(d)
                source = directory / 'source'
                source.mkdir()
                (directory / 'bd').write_bytes(b'old executable')
                def run(command, **kwargs):
                    if command[:2] == ['/fixture/go', 'build']:
                        self.assertIn('-mod=readonly', command)
                        self.assertEqual(command[command.index('-tags') + 1], 'gms_pure_go netgo')
                        self.assertEqual(kwargs['env']['CGO_ENABLED'], '1')
                        self.assertEqual(kwargs['env']['GOARCH'], arch)
                        self.assertEqual(kwargs['env']['MACOSX_DEPLOYMENT_TARGET'], '15.7')
                        self.assertEqual(kwargs['cwd'], source)
                        Path(command[command.index('-o') + 1]).write_bytes(b'built executable')
                with patch.object(installer.platform, 'mac_ver', return_value=('15.7.1', (), '')), \
                        patch.object(installer.shutil, 'which', return_value='/fixture/go'), \
                        patch.object(installer.subprocess, 'check_output', return_value=''), \
                        patch.object(installer.subprocess, 'run', side_effect=run) as calls, \
                        contextlib.redirect_stdout(io.StringIO()):
                    installer.build_beads(directory, source, pins['beads'], target)
                    self.assertEqual((directory / 'bd').read_bytes(), b'built executable')
                    self.assertTrue(any(c.args[0][0] == 'codesign' for c in calls.call_args_list))
                    calls.reset_mock()
                    installer.build_beads(directory, source, pins['beads'], target)
                    calls.assert_not_called()
                    (directory / 'bd').write_bytes(b'corrupted executable')
                    calls.side_effect = subprocess.CalledProcessError(1, 'xcrun')
                    with self.assertRaisesRegex(SystemExit, 'Command Line Tools'):
                        installer.build_beads(directory, source, pins['beads'], target)
                    self.assertEqual((directory / 'bd').read_bytes(), b'corrupted executable')

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
            uname = root / 'uname'
            uname.write_text('#!/bin/sh\ncase "$1" in -s) echo "${TT_TEST_OS:-Linux}";; -m) echo "${TT_TEST_ARCH:-x86_64}";; esac\n')
            uname.chmod(0o755)
            env = dict(os.environ, SHELL='/usr/bin/zsh', TT_SETUP_TEST_LOG=str(log), PATH=str(root) + os.pathsep + os.environ['PATH'])
            command = ['sh', str(root / 'setup.sh'), '--python', str(python)]
            system = subprocess.run([*command, '--system'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(system.returncode, 0, system.stderr)
            self.assertFalse((root / '.venv').exists())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls[-1], [str(root / 'scripts/install-tools.py'), '--platform', 'linux-x86_64', '--system', '--shell', 'zsh'])
            normal = subprocess.run([*command, '--no-shell'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(normal.returncode, 0, normal.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(['-m', 'venv', str(root / '.venv')], calls)
            self.assertEqual(calls[-1], [str(root / 'scripts/install-tools.py'), '--platform', 'linux-x86_64', '--shell', 'none'])
            self.assertTrue((root / '.venv/bin/python').exists())
            for system, arch, target in (('Darwin', 'arm64', 'darwin-arm64'), ('Darwin', 'x86_64', 'darwin-x86_64'),
                                         ('Linux', 'aarch64', 'linux-arm64')):
                result = subprocess.run(command, env=dict(env, TT_TEST_OS=system, TT_TEST_ARCH=arch), capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(calls[-1], [str(root / 'scripts/install-tools.py'), '--platform', target, '--shell', 'zsh'])
            before = log.read_text()
            for system, arch in (('Linux', 'armv7l'), ('MINGW64_NT', 'x86_64')):
                result = subprocess.run(command, env=dict(env, TT_TEST_OS=system, TT_TEST_ARCH=arch), capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsupported', result.stderr)
                self.assertEqual(log.read_text(), before)
            # Auto-select a newer Python when Apple's python3 is too old.
            old_python = root / 'python3'
            old_python.write_text('#!/bin/sh\nexit 1\n')
            old_python.chmod(0o755)
            newer_python = root / 'python3.13'
            newer_python.symlink_to(python)
            auto_command = ['sh', str(root / 'setup.sh'), '--no-shell']
            mac_env = dict(env, TT_TEST_OS='Darwin', TT_TEST_ARCH='arm64')
            result = subprocess.run(auto_command, env=mac_env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('darwin-arm64', result.stdout)
            newer_python.unlink()
            for name in ('python3.13', 'python3.12', 'python3.11'):
                (root / name).symlink_to(old_python)
            # Simulate Homebrew installing Python without touching the host.
            brew = root / 'brew'
            brew.write_text('#!/bin/sh\ncase "$1" in\n--prefix) echo "$TT_TEST_BREW_PREFIX";;\ninstall) mkdir -p "$TT_TEST_BREW_PREFIX/bin"; ln -s "$TT_TEST_PYTHON" "$TT_TEST_BREW_PREFIX/bin/python3.13";;\nesac\n')
            brew.chmod(0o755)
            brew_env = dict(mac_env, TT_TEST_BREW_PREFIX=str(root / 'brew-python'), TT_TEST_PYTHON=str(python))
            result = subprocess.run(auto_command, env=brew_env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Installing Python 3.13 through Homebrew', result.stdout)
            result = subprocess.run(auto_command, env=brew_env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Installing Python', result.stdout)
            # Explicit bad Python selections must fail rather than install another.
            result = subprocess.run([*auto_command, '--python', str(old_python)], env=brew_env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Installing Python', result.stdout)

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
