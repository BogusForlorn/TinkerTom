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

from tinkertom.agents import agent_call, helpers_waiting
from tinkertom.beads import beads_call
from tinkertom.mcp import available_tools, response
from tinkertom.process import run_process

ROOT = Path(__file__).resolve().parent.parent


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.bin = self.workspace / 'bin'
        self.bin.mkdir()
        for provider in ('claude', 'codex'):
            path = self.bin / provider
            path.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fake_helper.py').read_text())
            path.chmod(0o755)
        (self.workspace / 'tinkertom.toml').write_text('permissions="yolo"\nreset_buffer_seconds=0\nretry_seconds=0.01\n')
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   TT_FIXTURE_ROOT=str(self.workspace), TINKERTOM_PROVIDER='codex')
        for key in ('TINKERTOM_HELPER', 'TINKERTOM_NATIVE_DIR', 'TINKERTOM_TASK_DIR'):
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
        for parent, other, model in [('codex', 'claude', 'claude-opus-4-6'), ('claude', 'codex', 'gpt-5.6-sol')]:
            with self.subTest(parent=parent), patch.dict(os.environ, TINKERTOM_PROVIDER=parent):
                self.scenario(['review'])
                source = self.workspace / 'source.py'
                source.write_text('initial evidence')
                args = {'proposal': 'Check boundaries', 'files': ['source.py']}
                value, error = agent_call('rubber_duck', args, self.workspace)
                result = json.loads(value)
                self.assertFalse(error)
                self.assertEqual((result['provider'], result['model']), (other, model))
                self.assertEqual(result['usage']['input_tokens'], 12)
                call = self.calls()[0]
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

    def test_smaller_workers_wait_then_resume_same_cli_and_session(self):
        for provider, model in [('codex', 'gpt-5.6-luna'), ('claude', 'sonnet')]:
            with self.subTest(provider=provider), patch.dict(os.environ, TINKERTOM_PROVIDER=provider,
                    CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='parent-session', CLAUDE_CODE_CHILD_SESSION='1', CLAUDE_PID='123', CODEX_THREAD_ID='parent-thread'):
                self.scenario(['rate', 'complete'])
                value, error = agent_call('code_worker', {'task': 'Implement feature and check it', 'files': ['feature.txt']}, self.workspace)
                self.assertFalse(error)
                self.assertEqual(json.loads(value)['status'], 'complete')
                first, second = self.calls()
                self.assertGreaterEqual(second['at'] - first['at'], .5)
                self.assertIn('helper-exact', second['argv'])
                self.assertIn(model, second['argv'])
                self.assertEqual(second['cwd'], str(self.workspace))
                self.assertIn('tinkertom', ' '.join(second['argv']))
                self.assertTrue(all(value is None for value in second['parent_identity'].values()))
                if provider == 'claude' and os.geteuid() == 0:
                    self.assertEqual(second['is_sandbox'], '1')
                    self.assertEqual(second['mcp_timeout'], '604800000')
                self.assertNotIn('/usage', second['argv'])

    def test_auth_failure_never_falls_back_or_retries(self):
        self.scenario(['auth'])
        _, error = agent_call('rubber_duck', {'proposal': 'Review failure'}, self.workspace)
        self.assertTrue(error)
        self.assertEqual(len(self.calls()), 1)

    def test_role_scope_and_configuration_checks(self):
        with patch.dict(os.environ, TINKERTOM_HELPER='1'):
            self.assertNotIn('code_worker', {t['name'] for t in available_tools(self.workspace)})
            self.assertTrue(response({'id': 1, 'method': 'tools/call', 'params': {'name': 'rubber_duck', 'arguments': {'proposal': 'x'}}}, self.workspace)['result']['isError'])
        for args in ({'task': 'x', 'files': []}, {'task': 'x', 'files': ['../escape']}):
            with self.assertRaises(ValueError):
                agent_call('code_worker', args, self.workspace)
        (self.workspace / 'tinkertom.toml').write_text('beads=false\ndelegate_coding=false\nrubber_duck=false\n')
        self.assertEqual(len(available_tools(self.workspace)), 4)

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
