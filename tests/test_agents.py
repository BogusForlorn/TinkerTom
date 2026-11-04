import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tinkertom.agents import active_config, agent_call, git_snapshot, git_snapshot_changed, helper_command, helpers_waiting
from tinkertom.beads import beads_call
from tinkertom.config import Config
from tinkertom.mcp import available_tools, response
from tinkertom.mcp import bootstrap_context
from tinkertom.process import run_process

ROOT = Path(__file__).resolve().parent.parent


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.bin = self.workspace / 'bin'
        self.bin.mkdir()
        for provider in ('claude', 'codex', 'copilot'):
            path = self.bin / provider
            path.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fake_helper.py').read_text())
            path.chmod(0o755)
        (self.workspace / 'tinkertom.toml').write_text('permissions="yolo"\nreset_buffer_seconds=0\nretry_seconds=0.01\n')
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   TT_FIXTURE_ROOT=str(self.workspace), TINKERTOM_PROVIDER='codex',
                   COPILOT_GITHUB_TOKEN='copilot-only-token', GH_TOKEN='gh-token',
                   GITHUB_TOKEN='github-token')
        for key in ('TINKERTOM_HELPER', 'TINKERTOM_NATIVE_DIR', 'TINKERTOM_TASK_DIR',
                    'TINKERTOM_CONFIG_PATH', 'TINKERTOM_PROFILE', 'TINKERTOM_MANAGED',
                    'TINKERTOM_ATTEMPT'):
            env.pop(key, None)
        self.env = env
        patched = patch.dict(os.environ, env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

    def scenario(self, steps, **kwargs):
        (self.workspace / 'scenario.json').write_text(json.dumps({'steps': steps, **kwargs}))
        (self.workspace / 'calls.jsonl').unlink(missing_ok=True)

    def calls(self):
        return [json.loads(x) for x in (self.workspace / 'calls.jsonl').read_text().splitlines()]

    def test_opposite_routing_readonly_and_review_cache_tracks_evidence(self):
        for parent, other, model in [('codex', 'claude', 'opus'), ('claude', 'codex', 'gpt-5.6-sol')]:
            with self.subTest(parent=parent), patch.dict(os.environ, TINKERTOM_PROVIDER=parent):
                self.scenario(['review'])
                source = self.workspace / 'source.py'
                source.write_text('initial evidence')
                args = {'proposal': 'Check boundaries', 'files': ['source.py']}
                value, error = agent_call('rubber_duck', args, self.workspace)
                result = json.loads(value)
                self.assertFalse(error)
                self.assertEqual((result['provider'], result['model']), (other, model))
                self.assertEqual(result['profile'], 'general')
                self.assertEqual(result['usage']['input_tokens'], 12)
                call = self.calls()[0]
                self.assertIn('You have no tools or filesystem access.', call['prompt'])
                self.assertIn('Do not request or invoke tools.', call['prompt'])
                self.assertIn('Review only the supplied proposal, context, and snapshots.', call['prompt'])
                self.assertIn('explicitly identify the missing evidence', call['prompt'])
                self.assertEqual(call['provider'], other)
                self.assertNotEqual(call['cwd'], str(self.workspace))
                self.assertEqual(call['helper'], '1')
                self.assertIsNone(call['native'])
                self.assertIn(model, call['argv'])
                if other == 'claude':
                    self.assertEqual(call['argv'][call['argv'].index('--tools') + 1], '')
                    self.assertIn('--strict-mcp-config', call['argv'])
                    self.assertNotIn('--dangerously-skip-permissions', call['argv'])
                else:
                    self.assertIn('sandbox_mode="read-only"', call['argv'])
                    self.assertIn('--ignore-user-config', call['argv'])
                cached, _ = agent_call('rubber_duck', args, self.workspace)
                self.assertTrue(json.loads(cached)['cached'])
                self.assertEqual(len(self.calls()), 1)
                source.write_text('changed evidence')
                agent_call('rubber_duck', args, self.workspace)
                self.assertEqual(len(self.calls()), 2)

    def test_authorized_security_profile_routes_and_persists(self):
        (self.workspace / 'tinkertom.toml').write_text('rubber_duck_profile="authorized_security"\nreset_buffer_seconds=0\nretry_seconds=0.01\n')
        for parent, other, model in [('codex', 'claude', 'claude-opus-4-6'), ('claude', 'codex', 'gpt-daybreak-blue-latest')]:
            with self.subTest(parent=parent), patch.dict(os.environ, TINKERTOM_PROVIDER=parent):
                self.scenario(['review'])
                value, error = agent_call('rubber_duck', {'proposal': 'Check scope'}, self.workspace)
                result = json.loads(value)
                self.assertFalse(error)
                self.assertEqual((result['provider'], result['profile'], result['model']), (other, 'authorized_security', model))
                request = json.loads(next((self.workspace / '.tinkertom/agents').glob('*/request.json')).read_text())
                state = json.loads(next((self.workspace / '.tinkertom/agents').glob('*/state.json')).read_text())
                self.assertEqual(request['profile'], 'authorized_security')
                self.assertEqual(state['profile'], 'authorized_security')

    def test_worker_prompt_forbids_publication_and_snapshot_ignores_working_tree_edits(self):
        with patch.dict(os.environ, {'TINKERTOM_PROVIDER': 'claude', 'ENABLE_TOOL_SEARCH': 'auto'}):
            self.scenario(['complete'])
            value, error = agent_call('code_worker', {'task': 'Implement', 'files': ['feature.txt']}, self.workspace)
            self.assertFalse(error)
            prompt = self.calls()[0]['prompt']
            self.assertIn('Never stage, commit or push.', prompt)
            self.assertIn('TinkerTom has no publication mechanism', prompt)

    def test_git_snapshot_detects_index_commit_tag_but_not_working_tree_edits(self):
        def git(*args):
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=self.workspace,
                           check=True, capture_output=True)
        self.assertIsNone(git_snapshot(self.workspace))
        git('init', '-q')
        (self.workspace / 'a.txt').write_text('one')
        git('add', 'a.txt')
        git('commit', '-qm', 'one')
        base = git_snapshot(self.workspace)
        (self.workspace / 'a.txt').write_text('two')
        (self.workspace / 'new.txt').write_text('untracked')
        self.assertFalse(git_snapshot_changed(base, git_snapshot(self.workspace)))
        git('add', 'a.txt')
        self.assertTrue(git_snapshot_changed(base, git_snapshot(self.workspace)))
        git('reset', '-q')
        self.assertFalse(git_snapshot_changed(base, git_snapshot(self.workspace)))
        git('tag', 'v1')
        self.assertTrue(git_snapshot_changed(base, git_snapshot(self.workspace)))
        git('tag', '-d', 'v1')
        git('commit', '-qam', 'two')
        self.assertTrue(git_snapshot_changed(base, git_snapshot(self.workspace)))
        self.assertFalse(git_snapshot_changed(None, base))

    def test_claude_worker_keeps_safety_hook_when_compression_is_disabled(self):
        from dataclasses import replace
        directory = self.workspace / 'task'
        directory.mkdir()
        config = replace(Config(), provider='claude', claude_hook=False)
        argv = helper_command(config, None, False, directory, self.workspace)
        self.assertIn('--settings', argv)
        settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
        self.assertEqual(settings['hooks']['PreToolUse'][0]['matcher'], 'Bash')

    def test_smaller_workers_wait_then_resume_same_cli_and_session(self):
        for provider, model in [('codex', 'gpt-5.6-luna'), ('claude', 'sonnet')]:
            environment = {'TINKERTOM_PROVIDER': provider}
            if provider == 'claude':
                environment['ENABLE_TOOL_SEARCH'] = 'auto'
            with self.subTest(provider=provider), patch.dict(os.environ, environment,
                    CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='parent-session', CLAUDE_CODE_CHILD_SESSION='1', CLAUDE_PID='123', CODEX_THREAD_ID='parent-thread'):
                self.scenario(['rate', 'complete'])
                value, error = agent_call('code_worker', {'task': 'Implement feature and check it', 'files': ['feature.txt']}, self.workspace)
                self.assertFalse(error)
                self.assertEqual(json.loads(value)['status'], 'complete')
                first, second = self.calls()
                if provider == 'claude':
                    self.assertEqual(first['enable_tool_search'], 'auto')
                self.assertGreaterEqual(second['at'] - first['at'], .5)
                self.assertIn('helper-exact', second['argv'])
                self.assertIn(model, second['argv'])
                self.assertEqual(second['cwd'], str(self.workspace))
                self.assertIn('tinkertom', ' '.join(second['argv']))
                if provider == 'claude':
                    self.assertEqual(second['enable_tool_search'], 'auto')
                self.assertTrue(all(value is None for value in second['parent_identity'].values()))
                if provider == 'claude' and os.geteuid() == 0:
                    self.assertEqual(second['is_sandbox'], '1')
                    self.assertEqual(second['mcp_timeout'], '604800000')
                self.assertNotIn('/usage', second['argv'])

    def test_general_codex_duck_falls_back_for_auth_and_http_with_full_prompt(self):
        source = self.workspace / 'evidence.py'
        source.write_text('important_snapshot = True\n')
        for failure in ('auth', 'oauth', 'http503'):
            with self.subTest(failure=failure):
                self.scenario([failure, 'review'])
                value, error = agent_call('rubber_duck', {
                    'proposal': 'Review fallback boundaries ' + failure,
                    'context': 'The caller supplied fallback context.',
                    'files': ['evidence.py'],
                    'force': True,
                }, self.workspace)
                result = json.loads(value)
                self.assertFalse(error, value)
                self.assertEqual((result['requested_primary_provider'], result['requested_primary_model']),
                                 ('claude', 'opus'))
                self.assertEqual((result['actual_provider'], result['actual_model']),
                                 ('copilot', 'claude-opus-5.5'))
                primary, fallback = self.calls()
                self.assertEqual((primary['provider'], fallback['provider']), ('claude', 'copilot'))
                self.assertEqual(primary['auth_env'], {'copilot': False, 'gh': True, 'github': True})
                self.assertEqual(fallback['auth_env'], {'copilot': True, 'gh': False, 'github': False})
                self.assertNotIn('-p', fallback['argv'])
                self.assertNotIn('--resume', fallback['argv'])
                self.assertEqual(fallback['argv'][fallback['argv'].index('--model') + 1], 'claude-opus-5.5')
                self.assertEqual(fallback['argv'][fallback['argv'].index('--reasoning-effort') + 1], 'high')
                self.assertEqual(fallback['argv'][fallback['argv'].index('--available-tools') + 1], '')
                self.assertIn('--disable-builtin-mcps', fallback['argv'])
                self.assertNotEqual(fallback['cwd'], str(self.workspace))
                self.assertFalse(Path(fallback['cwd']).exists())
                self.assertIsNone(fallback['task_dir'])
                self.assertIsNone(fallback['attempt'])
                self.assertIn('Act as an independent rubber duck reviewer.', fallback['prompt'])
                self.assertIn('Review fallback boundaries ' + failure, fallback['prompt'])
                self.assertIn('The caller supplied fallback context.', fallback['prompt'])
                self.assertIn('important_snapshot = True', fallback['prompt'])
                directory = Path(result['log_dir'])
                request = json.loads((directory / 'request.json').read_text())
                state = json.loads((directory / 'state.json').read_text())
                self.assertEqual((request['actual_provider'], state['actual_provider']), ('copilot', 'copilot'))

    def test_missing_claude_persists_primary_request_then_falls_back(self):
        self.scenario(['review'])
        with patch('tinkertom.agents.shutil.which',
                   side_effect=lambda name, **_: None if name == 'claude' else str(self.bin / name)):
            value, error = agent_call('rubber_duck', {'proposal': 'Review missing primary'}, self.workspace)
        result = json.loads(value)
        self.assertFalse(error, value)
        self.assertEqual(result['actual_provider'], 'copilot')
        self.assertIn('CLI is missing', result['primary_failure'])
        self.assertEqual([call['provider'] for call in self.calls()], ['copilot'])
        directory = Path(result['log_dir'])
        self.assertTrue((directory / 'request.json').exists())
        self.assertTrue((directory / 'config.json').exists())
        self.assertTrue((directory / 'state.json').exists())

    def test_quota_and_authorized_security_failures_never_fallback(self):
        self.scenario(['rate', 'review'], wait=.01)
        value, error = agent_call('rubber_duck', {'proposal': 'Review quota route'}, self.workspace)
        self.assertFalse(error, value)
        self.assertTrue(all(call['provider'] == 'claude' for call in self.calls()))

        (self.workspace / 'tinkertom.toml').write_text(
            'rubber_duck_profile="authorized_security"\nreset_buffer_seconds=0\nretry_seconds=0.01\n')
        for failure in ('auth', 'http503'):
            with self.subTest(security_failure=failure):
                self.scenario([failure])
                value, error = agent_call('rubber_duck', {
                    'proposal': 'Review authorized failure ' + failure, 'force': True}, self.workspace)
                self.assertTrue(error)
                result = json.loads(value)
                self.assertEqual((result['actual_provider'], result['requested_primary_model']),
                                 ('claude', 'claude-opus-4-6'))
                providers = [call['provider'] for call in self.calls()]
                self.assertGreaterEqual(len(providers), 1)
                self.assertTrue(all(provider == 'claude' for provider in providers))

        self.scenario(['review'])
        with patch('tinkertom.agents.shutil.which',
                   side_effect=lambda name, **_: None if name == 'claude' else str(self.bin / name)):
            with self.assertRaisesRegex(ValueError, 'claude CLI is missing'):
                agent_call('rubber_duck', {
                    'proposal': 'Review authorized missing CLI', 'force': True}, self.workspace)
        self.assertFalse((self.workspace / 'calls.jsonl').exists())

    def test_successful_refusal_and_claude_parent_never_fallback(self):
        self.scenario(['refusal'])
        value, error = agent_call('rubber_duck', {'proposal': 'Review a refusal'}, self.workspace)
        self.assertFalse(error, value)
        self.assertIn('cannot help', json.loads(value)['report'])
        self.assertEqual([call['provider'] for call in self.calls()], ['claude'])

        with patch.dict(os.environ, TINKERTOM_PROVIDER='claude'):
            self.scenario(['auth'])
            value, error = agent_call('rubber_duck', {'proposal': 'Review from Claude parent'}, self.workspace)
        self.assertTrue(error)
        self.assertEqual(json.loads(value)['actual_provider'], 'codex')
        self.assertEqual([call['provider'] for call in self.calls()], ['codex'])

    def test_fallback_failure_reports_both_blockers(self):
        self.scenario(['auth', 'auth'])
        value, error = agent_call('rubber_duck', {'proposal': 'Review double failure'}, self.workspace)
        result = json.loads(value)
        self.assertTrue(error)
        self.assertEqual([call['provider'] for call in self.calls()], ['claude', 'copilot'])
        self.assertIn('401 authentication failed', result['primary_failure'])
        self.assertIn('401 authentication failed', result['fallback_failure'])
        self.assertIn('Primary Claude failed:', result['reason'])
        self.assertIn('Copilot fallback failed:', result['reason'])

    def test_fallback_requires_dedicated_copilot_token(self):
        self.scenario(['auth', 'review'])
        for token in (None, '', '   '):
            self.scenario(['auth', 'review'])
            with self.subTest(token=repr(token)), patch.dict(os.environ, {}):
                if token is None:
                    os.environ.pop('COPILOT_GITHUB_TOKEN', None)
                else:
                    os.environ['COPILOT_GITHUB_TOKEN'] = token
                value, error = agent_call('rubber_duck', {
                    'proposal': 'Review without dedicated Copilot credentials', 'force': True}, self.workspace)
            result = json.loads(value)
            self.assertTrue(error)
            self.assertEqual([call['provider'] for call in self.calls()], ['claude'])
            self.assertIn('COPILOT_GITHUB_TOKEN is not set', result['fallback_failure'])
            self.assertIn('refusing to use gh auth or stored Copilot OAuth', result['fallback_failure'])

    def test_explicit_mcp_bootstrap_survives_sanitized_environment(self):
        snapshot = self.workspace / '.tinkertom/native/codex/config.json'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(Config(
            provider='codex', rubber_duck_profile='general',
            claude_security_duck_model='claude-opus-4-6').to_dict()))
        hostile = {'PATH': self.env['PATH'], 'TT_FIXTURE_ROOT': str(self.workspace),
                   'COPILOT_GITHUB_TOKEN': 'copilot-only-token',
                   'TINKERTOM_PROVIDER': 'claude', 'TINKERTOM_PROFILE': 'general',
                   'TINKERTOM_HELPER': '1'}
        with patch.dict(os.environ, hostile, clear=True):
            bootstrap_context(self.workspace, snapshot, 'codex', 'authorized_security',
                              helper=False, managed=True)
            self.assertEqual(os.environ['TINKERTOM_PROVIDER'], 'codex')
            self.assertEqual(os.environ['TINKERTOM_PROFILE'], 'authorized_security')
            self.assertNotIn('TINKERTOM_HELPER', os.environ)
            self.assertEqual(os.environ['TINKERTOM_MANAGED'], '1')
            config = active_config(self.workspace)
            self.assertEqual((config.provider, config.rubber_duck_profile),
                             ('codex', 'authorized_security'))
            self.scenario(['auth'])
            value, error = agent_call('rubber_duck', {
                'proposal': 'Review sanitized pentest bootstrap', 'force': True}, self.workspace)
            result = json.loads(value)
            self.assertTrue(error)
            self.assertEqual((result['profile'], result['requested_primary_model']),
                             ('authorized_security', 'claude-opus-4-6'))
            self.assertEqual([call['provider'] for call in self.calls()], ['claude'])

        with patch.dict(os.environ, hostile, clear=True):
            bootstrap_context(self.workspace, snapshot, 'codex', 'general',
                              helper=False, managed=True)
            self.scenario(['auth', 'review'])
            value, error = agent_call('rubber_duck', {
                'proposal': 'Review sanitized general bootstrap', 'force': True}, self.workspace)
            result = json.loads(value)
            self.assertFalse(error, value)
            self.assertEqual((result['profile'], result['requested_primary_model'], result['actual_provider']),
                             ('general', 'opus', 'copilot'))

    def test_helper_bootstrap_hides_delegation_and_invalid_context_fails_closed(self):
        snapshot = self.workspace / '.tinkertom/agents/worker/config.json'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(Config(provider='codex').to_dict()))
        with patch.dict(os.environ, {'PATH': self.env['PATH']}, clear=True):
            bootstrap_context(self.workspace, snapshot, 'codex', 'general',
                              helper=True, managed=True)
            names = {tool['name'] for tool in available_tools(self.workspace)}
            self.assertNotIn('rubber_duck', names)
            self.assertNotIn('code_worker', names)
            self.assertEqual(os.environ['TINKERTOM_MANAGED'], '1')
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            bootstrap_context(self.workspace, self.workspace / '.tinkertom/missing.json',
                              'codex', 'authorized_security')
        outside = Path(self.tmp.name).parent / 'outside-tinkertom-config.json'
        outside.write_text(json.dumps(Config().to_dict()))
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(ValueError, 'must stay under'):
            bootstrap_context(self.workspace, outside, 'codex', 'general')
        with self.assertRaisesRegex(ValueError, 'Invalid MCP bootstrap provider'):
            bootstrap_context(self.workspace, snapshot, 'copilot', 'general')

    def test_role_scope_and_configuration_checks(self):
        with patch.dict(os.environ, TINKERTOM_HELPER='1'):
            self.assertNotIn('code_worker', {t['name'] for t in available_tools(self.workspace)})
            self.assertTrue(response({'id': 1, 'method': 'tools/call', 'params': {'name': 'rubber_duck', 'arguments': {'proposal': 'x'}}}, self.workspace)['result']['isError'])
        for args in ({'task': 'x', 'files': []}, {'task': 'x', 'files': ['../escape']}):
            with self.assertRaises(ValueError):
                agent_call('code_worker', args, self.workspace)
        (self.workspace / 'tinkertom.toml').write_text('beads=false\ndelegate_coding=false\nrubber_duck=false\n')
        self.assertEqual(len(available_tools(self.workspace)), 5)

    def test_headless_timeout_excludes_live_helper_wait_only(self):
        directory = self.workspace / '.tinkertom/agents/test'
        directory.mkdir(parents=True)
        (directory / 'state.json').write_text(json.dumps({'parent_attempt': 'a', 'status': 'waiting', 'pid': os.getpid()}))
        self.assertTrue(helpers_waiting(self.workspace, 'a'))
        self.assertFalse(helpers_waiting(self.workspace, 'b'))
        began = time.monotonic()
        outcome = run_process([sys.executable, '-c', 'import time; time.sleep(0.8)'], cwd=self.workspace,
            prompt='', log_dir=self.workspace / 'logs', timeout=.5, stopped=lambda: False,
            timeout_paused=lambda: time.monotonic() - began < .65)
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.error_kind)


class BeadsTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / '.tools/bin/bd').exists(), 'Pinned Beads not installed')
    def test_init_preserves_existing_index_history_and_gitignore(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            def git(*args):
                return subprocess.check_output(['git', '-C', d, *args], stderr=subprocess.DEVNULL)
            git('init', '-q')
            (workspace / '.gitignore').write_text('node_modules/\n')
            (workspace / 'staged.txt').write_text('user work\n')
            git('add', 'staged.txt', '.gitignore')
            before = git('ls-files', '--stage')
            beads_call(workspace, {'action': 'init'})
            self.assertEqual(git('ls-files', '--stage'), before)
            self.assertEqual((workspace / '.gitignore').read_text(), 'node_modules/\n')
            self.assertNotEqual(subprocess.run(['git', '-C', d, 'rev-parse', '--verify', 'HEAD'], capture_output=True).returncode, 0)

    @unittest.skipUnless((ROOT / '.tools/bin/bd').exists(), 'Pinned Beads not installed')
    def test_real_embedded_dependencies_progress_and_memory(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            def call(**args):
                output, error = beads_call(workspace, args)
                self.assertFalse(error)
                return json.loads(output)
            call(action='init')
            self.assertFalse((workspace / 'AGENTS.md').exists())
            self.assertFalse((workspace / '.claude').exists())
            first = call(action='create', title='Implement parsing')['id']
            second = call(action='create', title='Verify parsing')['id']
            call(action='depend', id=second, depends_on=first)
            ready = call(action='ready')
            self.assertIn(first, [v['id'] for v in ready])
            self.assertNotIn(second, [v['id'] for v in ready])
            call(action='claim', id=first)
            call(action='update', id=first, status='in_progress', notes='Implementation ready for review')
            call(action='close', id=first, notes='Coordinator verified parser cases')
            self.assertIn(second, [v['id'] for v in call(action='ready')])
            # Reopen the embedded DB in a new process for every request.
            output, error = beads_call(workspace, {'action': 'remember', 'key': 'quota-events', 'notes': 'Use structured quota events for retries'})
            self.assertFalse(error, output)
            output, error = beads_call(workspace, {'action': 'recall'})
            self.assertFalse(error)
            self.assertIn('structured quota events', output)
            output, error = beads_call(workspace, {'action': 'recall', 'key': 'quota-events'})
            self.assertFalse(error)
            self.assertIn('structured quota events', output)


class MCPAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_during_tool_and_cancellation_kills_helper(self):
        fixture = HelperTests()
        fixture.setUp()
        try:
            fixture.scenario(['sleep'])
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tinkertom', '-C', str(fixture.workspace), 'mcp',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=dict(os.environ))
            async def send(value):
                proc.stdin.write((json.dumps(value) + '\n').encode())
                await proc.stdin.drain()
            async def read():
                return json.loads(await asyncio.wait_for(proc.stdout.readline(), 8))
            try:
                await send({'id': 1, 'method': 'tools/call', 'params': {'name': 'code_worker', 'arguments': {'task': 'Implement fixture', 'files': ['feature.txt']}}})
                deadline = time.monotonic() + 8
                while not (fixture.workspace / 'calls.jsonl').exists():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(.05)
                pid = fixture.calls()[0]['pid']
                await send({'id': 2, 'method': 'ping'})
                self.assertEqual((await read())['id'], 2)
                await send({'method': 'notifications/cancelled', 'params': {'requestId': 1}})
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(.05)
                (fixture.workspace / 'source.py').write_text('def answer():\n    return 42\n')
                await send({'id': 3, 'method': 'tools/call', 'params': {'name': 'read', 'arguments': {'path': 'source.py'}}})
                result = await read()
                self.assertEqual(result['id'], 3)
                self.assertIn('return 42', result['result']['content'][0]['text'])
            finally:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 8)
        finally:
            fixture.doCleanups()
