import asyncio
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tinkertom.cli import main
from tinkertom.codex_proxy import CodexBridge, exhausted_windows
from tinkertom.config import Config
from tinkertom.mcp import response
from tinkertom.native import claude_settings, codex_ui, codex_start_permissions
from tinkertom.native_hooks import handle
from tinkertom.native_state import NativeState, schedule
from tinkertom.optimization import suite_environment

ROOT = Path(__file__).resolve().parent.parent


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.state = NativeState(self.workspace / '.tinkertom/native/codex')
        self.config = Config(reset_buffer_seconds=0).validate()
        self.bridge = CodexBridge([], self.workspace, {}, self.state, self.config)
        self.bridge.incoming({'id': 1, 'method': 'thread/start'})
        self.bridge.outgoing({'id': 1, 'result': {'thread': {'id': 'exact-session'}}})

    def test_lowercase_provider_shortcuts_and_provider_flags(self):
        with patch('tinkertom.native.run_native', return_value=0) as run:
            for provider in ('codex', 'claude'):
                self.assertEqual(main(['-C', str(self.workspace), provider, '--model', 'example', 'resume', '--last']), 0)
                run.assert_called_with(self.workspace, provider, ['--model', 'example', 'resume', '--last'])
            self.assertEqual(main(['-C', str(self.workspace)]), 0)
            run.assert_called_with(self.workspace, 'codex', [])

    def test_new_thread_policy_keeps_user_overrides_and_resumed_permissions(self):
        config = Config(permissions='yolo').validate()
        policy = codex_start_permissions(config, ['--model', 'chosen'])
        self.assertEqual(policy, {'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})
        # Wrapper flags are removed before this function; the effective
        # config, not a forwarded CLI argument, expresses standard mode.
        self.assertIsNone(codex_start_permissions(Config().validate(), []))
        for args in (['--sandbox', 'read-only'], ['-sworkspace-write'], ['--ask-for-approval=on-request'],
                     ['--approve-for-me'], ['--profile', 'work'], ['-c', 'sandbox_mode="read-only"'],
                     ['--config=approval_policy="on-request"'], ['-cpermissions.default="readonly"']):
            self.assertIsNone(codex_start_permissions(config, args), args)
        bridge = CodexBridge([], self.workspace, {}, self.state, config, start_permissions=policy)
        start = {'id': 100, 'method': 'thread/start', 'params': {
            'approvalPolicy': 'on-request', 'sandbox': 'workspace-write', 'permissions': None, 'model': 'chosen'}}
        bridge.incoming(start)
        self.assertEqual(start['params']['sandbox'], 'danger-full-access')
        self.assertEqual(start['params']['approvalPolicy'], 'never')
        self.assertEqual(start['params']['model'], 'chosen')
        self.assertNotIn('permissions', start['params'])
        for method in ('thread/resume', 'thread/fork', 'turn/start'):
            message = {'id': 101, 'method': method, 'params': {
                'threadId': 'other', 'approvalPolicy': 'on-request', 'sandbox': 'read-only'}}
            bridge.incoming(message)
            self.assertEqual(message['params']['approvalPolicy'], 'on-request')
            self.assertEqual(message['params']['sandbox'], 'read-only')

    def test_structured_rate_failure_waits_and_queues_without_claiming(self):
        with patch('tinkertom.codex_proxy.time.time', return_value=1000):
            self.bridge.outgoing({'method': 'turn/completed', 'params': {'threadId': 'exact-session', 'turn': {
                'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded', 'message': 'Limit hit'}}}})
        self.assertEqual(self.state.read()['next_run_at'], 19000)
        self.assertIsNone(self.bridge.retry(18999))
        reply = self.bridge.incoming({'id': 2, 'method': 'turn/start', 'params': {
            'threadId': 'exact-session', 'input': [{'type': 'text', 'text': 'Also update docs'}]}})
        self.assertIn('queued', reply['error']['message'])
        resume = self.bridge.retry(19000)
        self.assertEqual(resume['method'], 'turn/start')
        self.assertEqual(resume['params']['threadId'], 'exact-session')
        self.assertEqual(resume['params']['input'][-1]['text'], 'Also update docs')
        self.assertIsNone(self.bridge.retry(19001))
        self.assertFalse(self.bridge.outgoing({'id': resume['id'], 'result': {}}))

    def test_tool_output_subagents_and_auth_errors_never_schedule(self):
        for method, params in [
            ('item/commandExecution/outputDelta', {'threadId': 'exact-session', 'delta': 'usageLimitExceeded'}),
            ('error', {'threadId': 'subagent', 'willRetry': False, 'error': {'codexErrorInfo': 'usageLimitExceeded'}}),
            ('error', {'threadId': 'exact-session', 'willRetry': False, 'error': {'codexErrorInfo': 'unauthorized'}}),
            ('error', {'threadId': 'exact-session', 'willRetry': True, 'error': {'codexErrorInfo': 'rateLimitExceeded'}}),
        ]:
            self.bridge.outgoing({'method': method, 'params': params})
        self.assertEqual(self.state.read()['status'], 'ready')

    def test_only_exhausted_windows_affect_wait_and_duplicates_do_not_extend(self):
        windows = {'primary': {'usedPercent': 100, 'resetsAt': 1500}, 'secondary': {'usedPercent': 60, 'resetsAt': 999999}}
        self.assertEqual(exhausted_windows(windows, 1000), [windows['primary']])
        data = {}
        schedule(data, 's', {'windows': exhausted_windows(windows, 1000)}, self.config, 1000)
        self.assertEqual(data['next_run_at'], 1500)
        schedule(data, 's', {}, self.config, 1100)
        self.assertEqual(data['next_run_at'], 1500)
        windows['secondary']['usedPercent'] = 100
        self.assertEqual(len(exhausted_windows(windows, 1000)), 2)

    def test_cancel_prevents_wake_and_resume_keeps_deadline(self):
        with self.state.edit() as data:
            schedule(data, 'exact-session', {}, self.config, 1000)
        new_bridge = CodexBridge([], self.workspace, {}, self.state, self.config)
        new_bridge.incoming({'id': 9, 'method': 'thread/resume', 'params': {'threadId': 'exact-session'}})
        new_bridge.outgoing({'id': 9, 'result': {'thread': {'id': 'exact-session'}}})
        self.assertIsNone(new_bridge.retry(18999))
        new_bridge.incoming({'id': 10, 'method': 'turn/interrupt', 'params': {'threadId': 'exact-session'}})
        self.assertIsNone(new_bridge.retry(20000))

    def test_switching_threads_cancels_before_the_new_thread_reply(self):
        with self.state.edit() as data:
            schedule(data, 'exact-session', {}, self.config, 1000)
        self.bridge.incoming({'id': 11, 'method': 'thread/start'})
        self.assertIsNone(self.bridge.retry(20000))
        self.assertEqual(self.state.read()['status'], 'waiting')

    def test_resume_restores_each_threads_wait_and_pending_input(self):
        with self.state.edit() as data:
            schedule(data, 'exact-session', {}, self.config, 1000)
            data['pending_input'] = [{'type': 'text', 'text': 'Keep this follow-up'}]
        self.bridge.incoming({'id': 19, 'method': 'thread/unsubscribe', 'params': {'threadId': 'exact-session'}})
        self.assertIsNone(self.bridge.retry(20000))
        self.assertEqual(self.state.read()['status'], 'waiting')
        self.bridge.incoming({'id': 20, 'method': 'thread/resume', 'params': {'threadId': 'other-session'}})
        self.bridge.outgoing({'id': 20, 'result': {'thread': {'id': 'other-session'}}})
        self.assertEqual(self.state.read()['status'], 'ready')
        self.assertIsNone(self.bridge.retry(20000))
        self.bridge.incoming({'id': 21, 'method': 'thread/resume', 'params': {'threadId': 'exact-session'}})
        self.bridge.outgoing({'id': 21, 'result': {'thread': {'id': 'exact-session'}}})
        retry = self.bridge.retry(19000)
        self.assertEqual(retry['params']['threadId'], 'exact-session')
        self.assertEqual(retry['params']['input'][-1]['text'], 'Keep this follow-up')

    def test_claude_switch_and_resume_restores_cooldown(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude')
        handle({'session_id': 'a', 'hook_event_name': 'SessionStart'}, state, self.config)
        handle({'session_id': 'a', 'hook_event_name': 'StopFailure', 'error': 'rate_limit'}, state, self.config)
        deadline = state.read()['next_run_at']
        handle({'session_id': 'b', 'hook_event_name': 'SessionStart'}, state, self.config)
        self.assertEqual(state.read()['status'], 'ready')
        handle({'session_id': 'a', 'hook_event_name': 'SessionStart'}, state, self.config)
        self.assertEqual(state.read()['next_run_at'], deadline)

    def test_failed_resume_keeps_current_threads_continuation(self):
        with self.state.edit() as data:
            schedule(data, 'exact-session', {}, self.config, 1000)
        self.bridge.incoming({'id': 25, 'method': 'thread/resume', 'params': {'threadId': 'missing'}})
        self.assertIsNone(self.bridge.retry(20000))
        self.bridge.outgoing({'id': 25, 'error': {'message': 'Session not found'}})
        self.assertEqual(self.bridge.retry(20000)['params']['threadId'], 'exact-session')

    def test_recovering_an_interrupted_retry_reuses_the_saved_thread(self):
        with self.state.edit() as data:
            schedule(data, 'exact-session', {}, self.config, 1000)
            data['status'] = 'resuming'
        self.bridge.incoming({'id': 12, 'method': 'thread/resume', 'params': {'threadId': 'exact-session'}})
        self.bridge.outgoing({'id': 12, 'result': {'thread': {'id': 'exact-session'}}})
        self.assertIsNone(self.bridge.retry(18999))
        self.assertEqual(self.bridge.retry(19000)['params']['threadId'], 'exact-session')

    def test_claude_hooks_keep_queue_and_ignore_nonrate_failures(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude')
        event = {'session_id': 'claude-exact'}
        start = handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        self.assertIn('Headroom', start['hookSpecificOutput']['additionalContext'])
        handle({**event, 'hook_event_name': 'StopFailure', 'error': 'billing_error'}, state, self.config)
        self.assertEqual(state.read()['status'], 'ready')
        handle({**event, 'hook_event_name': 'StopFailure', 'error': 'rate_limit'}, state, self.config)
        first = state.read()['next_run_at']
        reply = handle({**event, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'Keep compatibility'}, state, self.config)
        self.assertEqual(reply['decision'], 'block')
        self.assertEqual(state.read()['pending_prompts'], ['Keep compatibility'])
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        self.assertEqual(state.read()['next_run_at'], first)

    def test_claude_settings_preserve_explicit_hooks(self):
        settings = {'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'custom-hook'}]}]}, 'theme': 'dark'}
        flags, rest = claude_settings(self.state.directory, self.workspace, self.config, ['--model', 'custom', '--settings', json.dumps(settings)])
        written = json.loads(Path(flags[1]).read_text())
        self.assertEqual(written['theme'], 'dark')
        self.assertEqual(len(written['hooks']['PreToolUse']), 2)
        self.assertEqual(rest, ['--model', 'custom'])

    def test_mcp_handshake_navigation_and_failure_status(self):
        init = response({'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}, self.workspace)
        self.assertEqual(init['result']['serverInfo']['name'], 'tinkertom')
        self.assertIsNone(response({'method': 'notifications/initialized'}, self.workspace))
        source = self.workspace / 'example.py'
        source.write_text('def wanted():\n    return 42\n')
        result = response({'id': 2, 'method': 'tools/call', 'params': {'name': 'read', 'arguments': {'path': 'example.py', 'symbol': 'wanted'}}}, self.workspace)
        self.assertIn('return 42', result['result']['content'][0]['text'])
        result = response({'id': 3, 'method': 'tools/call', 'params': {'name': 'run', 'arguments': {'command': 'printf failure; exit 7'}}}, self.workspace)
        self.assertTrue(result['result']['isError'])
        self.assertIn('exit_code: 7', result['result']['content'][0]['text'])


class CodexLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_are_server_config_for_fresh_explicit_and_automatic_resume(self):
        for permissions in ('yolo', 'standard'):
            for mode in ('fresh', 'resume', 'saved_wait'):
                with self.subTest(permissions=permissions, mode=mode), tempfile.TemporaryDirectory() as d:
                    workspace = Path(d)
                    state = NativeState(workspace / '.tinkertom/native/codex')
                    config = Config(permissions=permissions, model='chosen-model').validate()
                    if mode == 'saved_wait':
                        with state.edit() as data:
                            schedule(data, 'saved-session', {}, config)
                    saved = state.read()
                    calls = []
                    async def server(*argv, **kwargs):
                        calls.append(argv)
                        # Only socket readiness is needed: this fixture exercises
                        # the real launcher without issuing any model requests.
                        Path(argv[argv.index('--listen') + 1].removeprefix('unix://')).touch()
                        return SimpleNamespace(returncode=0)
                    ui = SimpleNamespace(returncode=0, poll=lambda: 0)
                    extra = ['resume', 'saved-session'] if mode == 'resume' else []
                    with patch('tinkertom.native.asyncio.create_subprocess_exec', side_effect=server), \
                            patch('tinkertom.native.subprocess.Popen', return_value=ui) as launch:
                        result = await codex_ui('codex', extra, workspace, config, {}, state, 0)
                    self.assertEqual(result, 0)
                    args = launch.call_args.args[0]
                    self.assertIn('--remote', args)
                    self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
                    self.assertNotIn('--sandbox', args)
                    self.assertEqual(args[args.index('--model') + 1], 'chosen-model')
                    if mode != 'fresh':
                        self.assertEqual(args[-2:], ['resume', 'saved-session'])
                    server_args = calls[0]
                    self.assertTrue(any('mcp_servers.tinkertom.command=' in item for item in server_args))
                    self.assertEqual('approval_policy="never"' in server_args, permissions == 'yolo')
                    self.assertEqual('sandbox_mode="danger-full-access"' in server_args, permissions == 'yolo')
                    self.assertEqual(state.read(), saved)


class BridgeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_picker_connections_share_backend_and_quota_resume(self):
        from websockets.asyncio.client import unix_connect
        from websockets.asyncio.server import unix_serve
        with tempfile.TemporaryDirectory(prefix='tt-wire-') as directory:
            workspace = Path(directory)
            requests = []
            turns = 0
            async def backend(client):
                nonlocal turns
                self.assertNotIn('Sec-WebSocket-Extensions', client.request.headers)
                async for frame in client:
                    d = json.loads(frame)
                    requests.append(d)
                    method = d.get('method')
                    if method in {'thread/start', 'thread/resume'}:
                        await client.send(json.dumps({'id': d['id'], 'result': {'thread': {'id': 'wire-session'}}}))
                    elif method == 'turn/start':
                        turns += 1
                        await client.send(json.dumps({'id': d['id'], 'result': {}}))
                        await client.send(json.dumps({'method': 'turn/started', 'params': {'threadId': 'wire-session', 'turn': {'id': str(turns)}}}))
                        error = {'codexErrorInfo': 'usageLimitExceeded', 'message': 'retry in 1 second'} if turns == 1 else None
                        await client.send(json.dumps({'method': 'turn/completed', 'params': {'threadId': 'wire-session', 'turn': {'status': 'failed' if error else 'completed', 'error': error}}}))
                    elif 'id' in d:
                        await client.send(json.dumps({'id': d['id'], 'result': {}}))
            state = NativeState(workspace / '.tinkertom/native/codex')
            upstream, socket = str(workspace / 'upstream.sock'), str(workspace / 'proxy.sock')
            bridge = CodexBridge(upstream, workspace, suite_environment(), state, Config(reset_buffer_seconds=0).validate())
            async with unix_serve(backend, upstream), unix_serve(bridge.handle, socket):
                async with unix_connect(socket) as client:
                    await client.send(json.dumps({'id': 1, 'method': 'thread/start'}))
                    await client.recv()
                    owner = bridge.owner
                    # /resume's picker uses a separate connection with overlapping
                    # request IDs. Closing it must leave the main UI operational.
                    async with unix_connect(socket) as picker:
                        await picker.send(json.dumps({'id': 1, 'method': 'thread/list'}))
                        self.assertEqual(json.loads(await picker.recv())['id'], 1)
                        self.assertEqual(bridge.owner, owner)
                    await client.send(json.dumps({'id': 2, 'method': 'mcpServerStatus/list'}))
                    self.assertEqual(json.loads(await client.recv())['id'], 2)
                    await client.send(json.dumps({'id': 3, 'method': 'turn/start', 'params': {'threadId': 'wire-session', 'input': [{'type': 'text', 'text': 'task'}]}}))
                    for _ in range(3):
                        await client.recv()
                    start = time.time()
                    message = json.loads(await asyncio.wait_for(client.recv(), 4))
                    self.assertEqual(message['method'], 'turn/started')
                    self.assertGreaterEqual(time.time() - start, .8)
                    message = json.loads(await client.recv())
                    self.assertEqual(message['params']['turn']['status'], 'completed')
            self.assertEqual([x['method'] for x in requests], ['thread/start', 'thread/list', 'mcpServerStatus/list', 'turn/start', 'turn/start'])
            self.assertEqual(requests[-1]['params']['threadId'], 'wire-session')


class NativeTerminalTests(unittest.TestCase):
    def test_native_claude_ui_slash_commands_and_exact_resume_for_both_spellings(self):
        for spelling in ('tinkertom', 'TinkerTom'):
            with self.subTest(spelling=spelling), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                executable = workspace / 'fake-claude'
                executable.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fake_native_claude.py').read_text())
                executable.chmod(0o755)
                (workspace / 'tinkertom.toml').write_text('reset_buffer_seconds = 0\n')
                master, slave = pty.openpty()
                command = 'source "$1"; cd -- "$2"; ' + spelling + ' claude --tt-executable "$3"'
                proc = subprocess.Popen(['zsh', '-fc', command, 'test', str(ROOT / 'shell/tinkertom.zsh'), directory, str(executable)],
                                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                transcript = bytearray()
                def wait_text(wanted):
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        if wanted in transcript:
                            return
                        if select.select([master], [], [], .05)[0]:
                            try:
                                transcript.extend(os.read(master, 65536))
                            except OSError:
                                break
                    self.fail(f'Missing {wanted!r}: {bytes(transcript)!r}')
                try:
                    wait_text(b'NATIVE CLAUDE UI')
                    os.write(master, b'/help\n')
                    wait_text(b'NATIVE SLASH HELP')
                    os.write(master, b'Implement the task\n')
                    wait_text(b'NATIVE QUOTA FAILURE')
                    os.write(master, b'Also update docs\n')
                    wait_text(b'Follow-up queued locally')
                    wait_text(b'CONTINUED fake-native-session')
                    os.write(master, b'/exit\n')
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertNotIn(b'Task for claude:', transcript)
                    calls = [json.loads(line) for line in (workspace / 'native-calls.jsonl').read_text().splitlines()]
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0]['cwd'], directory)
                    self.assertGreaterEqual(calls[1]['at'] - calls[0]['at'], 1)
                    self.assertIn('Also update docs', calls[1]['argv'][-1])
                    self.assertNotIn('-p', calls[0]['argv'])
                    for call in calls:
                        self.assertIn('--dangerously-skip-permissions', call['argv'])
                        if os.geteuid() == 0:
                            self.assertEqual(call['is_sandbox'], '1')
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=7)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()
                    os.close(master)


if __name__ == '__main__':
    unittest.main()
