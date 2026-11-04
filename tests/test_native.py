from dataclasses import replace
import asyncio
from datetime import datetime
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
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tinkertom.cli import main
from tinkertom.codex_proxy import COORDINATOR_EDIT_POLICY, COORDINATOR_POLICY, CodexBridge, exhausted_windows
from tinkertom.config import Config
from tinkertom.mcp import TOOLS, response
from tinkertom.native import claude_compaction_environment, claude_settings, claude_tool_search_mode, claude_ui, codex_settings, codex_ui, codex_start_permissions, coordinator_edit_args, mcp_config, native_profile_args, run_native
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
                self.assertEqual(main(['-C', str(self.workspace), provider, '--', 'pentest', '--model', 'example']), 0)
                run.assert_called_with(self.workspace, provider, ['--', 'pentest', '--model', 'example'])
            self.assertEqual(main(['-C', str(self.workspace)]), 0)
            run.assert_called_with(self.workspace, 'codex', [])
            self.assertEqual(main(['-C', str(self.workspace), provider, '--tt-coordinator-edits', '--model', 'example']), 0)
            run.assert_called_with(self.workspace, provider, ['--tt-coordinator-edits', '--model', 'example'])

    def test_coordinator_policy_defaults_strict_and_opt_in_is_consumed(self):
        self.assertFalse(self.config.coordinator_edits)
        self.assertIn('code_worker', COORDINATOR_POLICY)
        self.assertIn('Do not directly edit source', COORDINATOR_POLICY)
        self.assertIn('provider-native editing tools directly', COORDINATOR_EDIT_POLICY)
        self.assertEqual(coordinator_edit_args(['--tt-coordinator-edits', '--model', 'x']),
                         (True, ['--model', 'x']))
        self.assertEqual(coordinator_edit_args(['--model', 'x']), (False, ['--model', 'x']))
        self.assertEqual(coordinator_edit_args(['--', '--tt-coordinator-edits']),
                         (False, ['--', '--tt-coordinator-edits']))
        strict = handle({'session_id': 'strict', 'hook_event_name': 'SessionStart'},
                        NativeState(self.workspace / '.tinkertom/native/claude'), self.config)
        self.assertIn('Do not directly edit source', strict['hookSpecificOutput']['additionalContext'])
        flexible = replace(self.config, coordinator_edits=True)
        edit = handle({'session_id': 'edit', 'hook_event_name': 'SessionStart'},
                      NativeState(self.workspace / '.tinkertom/native/claude-edit'), flexible)
        context = edit['hookSpecificOutput']['additionalContext']
        self.assertIn('provider-native editing tools directly', context)
        self.assertNotIn('Do not directly edit source files', context)
        bridge = CodexBridge([], self.workspace, {}, self.state,
                             replace(self.config, coordinator_edits=True))
        turn = {'id': 1, 'method': 'turn/start', 'params': {'threadId': 's', 'input': []}}
        bridge.incoming(turn)
        self.assertIn('provider-native editing tools directly',
                      turn['params']['additionalContext']['tinkertom']['value'])

    def test_claude_edit_mode_allows_built_in_edit_tools_but_keeps_bash_guard(self):
        flags, _ = claude_settings(self.state.directory, self.workspace,
                                    replace(self.config, coordinator_edits=True), [])
        self.assertTrue(Path(flags[1]).exists())
        with patch('tinkertom.native.subprocess.Popen', return_value=SimpleNamespace(returncode=0, poll=lambda: 0)), \
                patch('tinkertom.native.termios.tcgetattr', return_value=[]), \
                patch('tinkertom.native.termios.tcsetattr'), \
                patch('tinkertom.native.sys.stdin.fileno', return_value=0):
            claude_ui('claude', [], self.workspace, replace(self.config, coordinator_edits=True), {},
                      NativeState(self.workspace / '.tinkertom/native/claude-edit-ui'), 0)
    def test_native_profile_modes_strip_only_exact_leading_pentest(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.assertEqual(native_profile_args(['pentest', '--model', 'example', 'resume', 'SESSION']),
                                 ('authorized_security', ['--model', 'example', 'resume', 'SESSION']))
                self.assertEqual(native_profile_args(['--', 'pentest', '--model', 'example']),
                                 ('general', ['--', 'pentest', '--model', 'example']))
                self.assertEqual(native_profile_args(['Pentest', '--model', 'example']),
                                 ('general', ['Pentest', '--model', 'example']))

    def test_native_launch_snapshots_profile_and_preserves_provider_args(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as d:
                workspace = Path(d)
                (workspace / 'tinkertom.toml').write_text('rubber_duck_profile = "authorized_security"\n')
                with patch('tinkertom.native.shutil.which', return_value='/bin/provider'), \
                     patch('tinkertom.native.sys.stdin.isatty', return_value=True), \
                     patch('tinkertom.native.sys.stdout.isatty', return_value=True), \
                     patch('tinkertom.native.termios.tcgetattr', return_value=[]), \
                     patch('tinkertom.native.termios.tcsetattr'), \
                     patch('tinkertom.native.codex_ui', new_callable=AsyncMock) as codex, \
                     patch('tinkertom.native.claude_ui', return_value=0) as claude:
                    run_native(workspace, provider, ['--model', 'example'])
                snapshot = json.loads((workspace / '.tinkertom/native' / provider / 'config.json').read_text())
                self.assertEqual(snapshot['rubber_duck_profile'], 'general')
                forwarded = codex.call_args.args[1] if provider == 'codex' else claude.call_args.args[1]
                self.assertEqual(forwarded, ['--model', 'example'])

                (workspace / 'tinkertom.toml').write_text('rubber_duck_profile = "general"\n')
                with patch('tinkertom.native.shutil.which', return_value='/bin/provider'), \
                     patch('tinkertom.native.sys.stdin.isatty', return_value=True), \
                     patch('tinkertom.native.sys.stdout.isatty', return_value=True), \
                     patch('tinkertom.native.termios.tcgetattr', return_value=[]), \
                     patch('tinkertom.native.termios.tcsetattr'), \
                     patch('tinkertom.native.codex_ui', new_callable=AsyncMock) as codex, \
                     patch('tinkertom.native.claude_ui', return_value=0) as claude:
                    run_native(workspace, provider, ['pentest', '--model', 'example', 'resume', 'SESSION'])
                snapshot = json.loads((workspace / '.tinkertom/native' / provider / 'config.json').read_text())
                self.assertEqual(snapshot['rubber_duck_profile'], 'authorized_security')
                forwarded = codex.call_args.args[1] if provider == 'codex' else claude.call_args.args[1]
                self.assertEqual(forwarded, ['--model', 'example', 'resume', 'SESSION'])

    def test_new_thread_policy_keeps_user_overrides_and_resumed_permissions(self):
        config = Config(permissions='yolo').validate()
        policy = codex_start_permissions(config, ['--model', 'chosen'])
        self.assertEqual(policy, {'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})
        # Wrapper flags are removed before this function; the effective
        # config, not a forwarded CLI argument, expresses standard mode.
        self.assertEqual(codex_start_permissions(Config().validate(), []),
                         {'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'model': 'gpt-daybreak-blue-latest'})
        for args in (['--sandbox', 'read-only'], ['-sworkspace-write'], ['--ask-for-approval=on-request'],
                     ['--approve-for-me'], ['-c', 'sandbox_mode="read-only"'],
                     ['--config=approval_policy="on-request"'], ['-cpermissions.default="readonly"']):
            self.assertEqual(codex_start_permissions(config, args), {'model': 'gpt-daybreak-blue-latest'}, args)
        for args in (['--model', 'chosen'], ['--model=chosen'], ['-m', 'chosen'], ['-mchosen'],
                     ['-c', 'model="chosen"']):
            self.assertEqual(codex_start_permissions(config, args),
                             {'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}, args)
        for args in (['--profile', 'work'], ['-pwork']):
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

    def test_claude_postcompact_recovers_stale_wait_without_restart(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude-postcompact')
        event = {'session_id': 'claude-exact'}
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        with state.edit() as data:
            data.update(status='waiting', detected_at=10, next_run_at=200,
                        pending_prompts=[])
        handle({**event, 'hook_event_name': 'PostCompact', 'trigger': 'manual'}, state, self.config)
        saved = state.read()
        self.assertEqual(saved, {'session_id': 'claude-exact', 'status': 'ready', 'pending_prompts': []})

    def test_claude_postcompact_queues_due_now_and_is_idempotent(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude-postcompact-queue')
        event = {'session_id': 'claude-exact'}
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        with state.edit() as data:
            data.update(status='waiting', detected_at=10, next_run_at=200,
                        pending_prompts=['follow up'])
        with patch('tinkertom.native_hooks.time.time', return_value=300):
            handle({**event, 'hook_event_name': 'PostCompact'}, state, self.config)
        first = state.read()
        self.assertEqual(first['next_run_at'], 300)
        self.assertEqual(first['pending_prompts'], ['follow up'])
        with patch('tinkertom.native_hooks.time.time', return_value=400):
            handle({**event, 'hook_event_name': 'PostCompact'}, state, self.config)
        self.assertEqual(state.read(), first)

    def test_claude_postcompact_and_stopfailure_ignore_invalid_scope(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude-recovery-guards')
        event = {'session_id': 'claude-exact'}
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        with state.edit() as data:
            data.update(status='waiting', detected_at=10, next_run_at=200,
                        pending_prompts=['keep'])
        for candidate in ({'hook_event_name': 'PostCompact', 'session_id': 'other'},
                          {'hook_event_name': 'PostCompact'},
                          {'hook_event_name': 'PostCompact', **event, 'agent_id': 'subagent'}):
            before = state.read()
            handle(candidate, state, self.config)
            self.assertEqual(state.read(), before)

        for status in ('ready', 'resuming'):
            with state.edit() as data:
                data.update(status=status, detected_at=10, next_run_at=200,
                            pending_prompts=['keep'])
            before = state.read()
            handle({**event, 'hook_event_name': 'PostCompact'}, state, self.config)
            self.assertEqual(state.read(), before)

        for agent_id in (None, ''):
            with state.edit() as data:
                data.update(status='waiting', detected_at=10, next_run_at=200,
                            pending_prompts=[])
            handle({**event, 'hook_event_name': 'PostCompact', 'agent_id': agent_id}, state, self.config)
            self.assertEqual(state.read(), {'session_id': event['session_id'], 'status': 'ready', 'pending_prompts': []})

        with state.edit() as data:
            data.update(status='waiting', pending_prompts=[])
        handle({**event, 'hook_event_name': 'StopFailure', 'error': 'invalid_request'}, state, self.config)
        self.assertEqual(state.read(), {'session_id': event['session_id'], 'status': 'ready', 'pending_prompts': []})
        with patch('tinkertom.native_state.time.time', return_value=100):
            handle({**event, 'hook_event_name': 'StopFailure', 'error': 'rate_limit'}, state, self.config)
        self.assertEqual(state.read()['status'], 'waiting')
        self.assertGreater(state.read()['next_run_at'], 100)

    def test_claude_nonrate_stopfailure_never_drops_queued_prompts(self):
        state = NativeState(self.workspace / '.tinkertom/native/claude-recovery-queue')
        event = {'session_id': 'claude-exact'}
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        with state.edit() as data:
            data.update(status='resuming', detected_at=10, next_run_at=200,
                        pending_prompts=['keep'])
        handle({**event, 'hook_event_name': 'StopFailure', 'error': 'invalid_request'}, state, self.config)
        self.assertEqual(state.read()['status'], 'resuming')
        self.assertEqual(state.read()['pending_prompts'], ['keep'])

    def test_claude_postcompact_preserves_custom_hooks_and_is_registered(self):
        settings = {
            'env': {'ENABLE_TOOL_SEARCH': 'false'},
            'hooks': {'PostCompact': [{'hooks': [{'type': 'command', 'command': 'custom-hook'}]}]},
        }
        flags, _ = claude_settings(self.state.directory, self.workspace, self.config,
                                    ['--settings', json.dumps(settings)], tool_search='false')
        written = json.loads(Path(flags[1]).read_text())
        standard_events = {'SessionStart', 'UserPromptSubmit', 'StopFailure', 'Stop', 'PostCompact', 'PreToolUse'}
        self.assertTrue(standard_events <= written['hooks'].keys())
        self.assertEqual(len(written['hooks']['PostCompact']), 2)
        self.assertEqual(written['env']['ENABLE_TOOL_SEARCH'], 'false')

    def test_claude_hook_uses_only_displayed_reset_error_fields(self):
        zone = ZoneInfo('Asia/Singapore')
        now = datetime(2026, 10, 6, 13, 50, tzinfo=zone).timestamp()
        expected = datetime(2026, 10, 6, 14, 50, tzinfo=zone).timestamp()
        for index, field in enumerate(('error_details', 'last_assistant_message')):
            state = NativeState(self.workspace / f'.tinkertom/native/claude-{index}')
            event = {'session_id': f'claude-{index}'}
            handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
            failure = {**event, 'hook_event_name': 'StopFailure', 'error': 'rate_limit',
                       field: "You've hit your session limit \u00b7 resets 2:50pm (Asia/Singapore)"}
            with patch('tinkertom.native_state.time.time', return_value=now):
                handle(failure, state, self.config)
            self.assertEqual(state.read()['next_run_at'], expected)

        state = NativeState(self.workspace / '.tinkertom/native/claude-unrelated')
        event = {'session_id': 'claude-unrelated'}
        handle({**event, 'hook_event_name': 'SessionStart'}, state, self.config)
        with patch('tinkertom.native_state.time.time', return_value=now):
            handle({**event, 'hook_event_name': 'StopFailure', 'error': 'rate_limit',
                    'cwd': 'resets 2:50pm (Asia/Singapore)'}, state, self.config)
        self.assertEqual(state.read()['next_run_at'], now + self.config.cooldown_seconds)

    def test_claude_settings_preserve_explicit_hooks(self):
        settings = {'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'custom-hook'}]}]}, 'theme': 'dark'}
        flags, rest = claude_settings(self.state.directory, self.workspace, self.config, ['--model', 'custom', '--settings', json.dumps(settings)])
        written = json.loads(Path(flags[1]).read_text())
        self.assertEqual(written['theme'], 'dark')
        self.assertEqual(len(written['hooks']['PreToolUse']), 2)
        self.assertEqual(rest, ['--model', 'custom'])

    def test_claude_tool_search_mode_and_mcp_env_precedence(self):
        for value in ('true', 'false', 'auto'):
            self.assertEqual(claude_tool_search_mode([], {'ENABLE_TOOL_SEARCH': value}), value)
        self.assertEqual(claude_tool_search_mode([], {}), 'auto')
        settings = json.dumps({'env': {'ENABLE_TOOL_SEARCH': 'false'}})
        self.assertEqual(claude_tool_search_mode(['--settings', settings], {'ENABLE_TOOL_SEARCH': 'true'}), 'false')
        self.assertEqual(claude_tool_search_mode(['--settings', json.dumps({'env': {'ENABLE_TOOL_SEARCH': ''}})],
                                                  {'ENABLE_TOOL_SEARCH': 'true'}), 'true')
        flags, _ = claude_settings(self.state.directory, self.workspace, self.config,
                                    ['--settings', settings], provider='claude')
        mcp_path = Path(flags[flags.index('--mcp-config') + 1])
        generated = json.loads(mcp_path.read_text())['mcpServers']['tinkertom']
        self.assertEqual(generated['env']['ENABLE_TOOL_SEARCH'], 'false')

    def test_claude_tool_search_settings_errors_and_relative_files(self):
        with self.assertRaisesRegex(ValueError, r'--settings requires'):
            claude_tool_search_mode(['--settings', '--model'], {})
        with self.assertRaisesRegex(ValueError, r'--settings requires'):
            claude_settings(self.state.directory, self.workspace, self.config,
                            ['--settings', '--model'])
        settings_path = self.workspace / 'claude-settings.json'
        settings_path.write_text(json.dumps({'env': {'ENABLE_TOOL_SEARCH': 'true'}}))
        self.assertEqual(claude_tool_search_mode(['--settings', settings_path.name], {}, self.workspace), 'true')

    def test_claude_session_context_is_dynamic_and_codex_instructions_are_not(self):
        start = handle({'session_id': 'claude', 'hook_event_name': 'SessionStart'},
                       NativeState(self.workspace / '.tinkertom/native/claude'), self.config)
        context = start['hookSpecificOutput']['additionalContext']
        self.assertIn('ToolSearch', context)
        policy_tools = {'read', 'symbols', 'run', 'code_worker', 'rubber_duck', 'beads', 'handover'}
        registered_tools = {tool['name'] for tool in TOOLS}
        self.assertTrue(policy_tools <= registered_tools)
        for name in policy_tools:
            self.assertIn(f'mcp__tinkertom__{name}', context)
        from tinkertom.codex_proxy import INSTRUCTIONS
        self.assertNotIn('ToolSearch', INSTRUCTIONS)

    def test_provider_handover_context_is_injected_only_for_targets(self):
        marker = self.workspace / '.tinkertom'
        marker.mkdir(exist_ok=True)
        (marker / 'handover.json').write_text(json.dumps({
            'provider': 'codex', 'source': 'codex', 'target': 'claude',
            'state': 'completed', 'expires_at': time.time() + 1000,
        }))
        (marker / 'checkpoint.md').write_text('target checkpoint')
        claude = handle({'session_id': 'target', 'hook_event_name': 'SessionStart'},
                        NativeState(self.workspace / '.tinkertom/native/claude'), self.config)
        self.assertIn('target checkpoint', claude['hookSpecificOutput']['additionalContext'])
        codex = self.bridge
        turn = {'id': 'handover-turn', 'method': 'turn/start', 'params': {'threadId': 'exact-session', 'input': []}}
        codex.incoming(turn)
        self.assertNotIn('target checkpoint', turn['params']['additionalContext']['tinkertom']['value'])
        (marker / 'handover.json').write_text(json.dumps({
            'provider': 'claude', 'source': 'claude', 'target': 'codex',
            'state': 'completed', 'expires_at': time.time() + 1000,
        }))
        with self.state.edit() as data:
            data.update(status='waiting', session_id='exact-session', next_run_at=0, pending_input=[])
        retry = codex.retry(1)
        self.assertIn('target checkpoint', retry['params']['additionalContext']['tinkertom']['value'])

    def test_compaction_values_and_provider_scopes_are_exact(self):
        self.assertEqual(claude_compaction_environment(110000), {
            'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '75',
            'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '146667',
        })
        settings, _ = claude_settings(self.state.directory, self.workspace,
                                       Config(auto_compact_tokens=130000).validate(),
                                       ['--settings', json.dumps({'env': {'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '1'}})])
        env = json.loads(Path(settings[1]).read_text())['env']
        self.assertEqual(env['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'], '75')
        self.assertEqual(env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '173334')
        codex = codex_settings(self.workspace, auto_compact_tokens=150000)
        self.assertIn('model_auto_compact_token_limit=150000', codex)
        self.assertIn('model_auto_compact_token_limit_scope="total"', codex)
        self.assertNotIn('CLAUDE_CODE_AUTO_COMPACT_WINDOW', json.dumps(codex))

    def test_auto_compact_token_validation_is_strict_and_bounded(self):
        for value in (110000, 130000, 150000):
            with self.subTest(value=value):
                self.assertEqual(Config(auto_compact_tokens=value).validate().auto_compact_tokens, value)
        for value in (True, False, 130000.0, '130000', 109999, 150001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Config(auto_compact_tokens=value).validate()

    def test_claude_coordinator_argv_model_precedence_and_policy(self):
        cases = [
            (Config(rubber_duck_profile='authorized_security').validate(), [], 'claude-opus-4-6'),
            (Config().validate(), [], None),
            (Config(model='project', rubber_duck_profile='authorized_security').validate(), [], 'project'),
            (Config(model='project', rubber_duck_profile='authorized_security').validate(), ['--model=explicit'], 'explicit'),
        ]
        for config, extra, expected in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as d:
                workspace = Path(d)
                state = NativeState(workspace / '.tinkertom/native/claude')
                proc = SimpleNamespace(returncode=0, poll=lambda: 0)
                with patch('tinkertom.native.subprocess.Popen', return_value=proc) as launch, \
                        patch('tinkertom.native.termios.tcgetattr', return_value=[]), \
                        patch('tinkertom.native.termios.tcsetattr'):
                    self.assertEqual(claude_ui('claude', extra, workspace, config, {}, state, 0), 0)
                command = launch.call_args.args[0]
                self.assertEqual(command.count('--append-system-prompt'), 1)
                self.assertEqual(command[command.index('--system-prompt-snapshot') + 1], 'off')
                self.assertEqual(command[command.index('--disallowedTools') + 1], 'Edit,Write,NotebookEdit,Bash')
                if expected:
                    self.assertEqual(sum(arg == '--model' or arg.startswith('--model=') for arg in command), 1)
                    self.assertTrue(any(arg == expected or arg == '--model' and command[command.index(arg) + 1] == expected or arg == '--model=' + expected for arg in command))
                else:
                    self.assertNotIn('--model', command)

    def test_generated_mcp_bootstrap_is_explicit_for_native_and_helpers(self):
        config_path = self.state.directory / 'config.json'
        native = mcp_config(self.workspace, config_path, 'codex',
                            'authorized_security', managed=True)
        self.assertEqual(native['args'][-7:], [
            '--__tinkertom-config', str(config_path),
            '--__tinkertom-provider', 'codex',
            '--__tinkertom-profile', 'authorized_security',
            '--__tinkertom-managed'])
        helper = mcp_config(self.workspace, config_path, 'claude', 'general',
                            helper=True, managed=True)
        self.assertEqual(helper['args'][-2:],
                         ['--__tinkertom-helper', '--__tinkertom-managed'])

        flags, _ = claude_settings(
            self.state.directory, self.workspace, self.config, [], config_path,
            'claude', 'authorized_security', managed=True)
        mcp_path = Path(flags[flags.index('--mcp-config') + 1])
        args = json.loads(mcp_path.read_text())['mcpServers']['tinkertom']['args']
        self.assertIn('authorized_security', args)
        self.assertIn('--__tinkertom-managed', args)

        relative = mcp_config(Path('.'), Path('.tinkertom/native/codex/config.json'),
                              'codex', 'authorized_security', managed=True)
        self.assertEqual(Path(relative['args'][relative['args'].index('-C') + 1]), Path.cwd())
        config_arg = relative['args'][relative['args'].index('--__tinkertom-config') + 1]
        self.assertTrue(Path(config_arg).is_absolute())

    def test_codex_pretooluse_hook_is_valid_nested_toml_and_preserves_user_hooks(self):
        import tomllib
        home = self.workspace / 'codex-home'
        home.mkdir()
        (home / 'config.toml').write_text(
            '[[hooks.PreToolUse]]\nmatcher = "^Edit$"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "user-hook"\n')
        with patch.dict(os.environ, {'CODEX_HOME': str(home)}):
            args = codex_settings(self.workspace)
        value = next(a for a in args if a.startswith('hooks.PreToolUse='))
        groups = tomllib.loads(value)['hooks']['PreToolUse']
        self.assertEqual(groups[0]['hooks'][0]['command'], 'user-hook')
        group = groups[-1]
        self.assertEqual(group['matcher'], '^Bash$')
        hook = group['hooks'][0]
        self.assertEqual((hook['type'], hook['timeout']), ('command', 5))
        self.assertIn('tinkertom.hooks', hook['command'])
        self.assertTrue(hook['statusMessage'])
        self.assertFalse(any('trust' in a.lower() for a in args))

    def test_command_compression_flag_is_explicit_and_safety_hook_stays(self):
        for enabled in (True, False):
            config = replace(self.config, claude_hook=enabled)
            flags, _ = claude_settings(self.state.directory, self.workspace, config, [], tool_search='auto')
            written = json.loads(Path(flags[1]).read_text())
            self.assertEqual(written['env']['TINKERTOM_COMMAND_COMPRESSION'], '1' if enabled else '0')
            self.assertEqual(written['hooks']['PreToolUse'][-1]['matcher'], 'Bash')

    def test_codex_mcp_auto_approval_is_opt_in_and_exact(self):
        default = codex_settings(self.workspace)
        self.assertNotIn('mcp_servers.tinkertom.default_tools_approval_mode="approve"', default)
        enabled = codex_settings(self.workspace, auto_approve=True)
        self.assertEqual(enabled[-2:], ['-c', 'mcp_servers.tinkertom.default_tools_approval_mode="approve"'])

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
                    joined = ' '.join(server_args)
                    self.assertIn('--__tinkertom-provider', joined)
                    self.assertIn('--__tinkertom-profile', joined)
                    self.assertIn('--__tinkertom-managed', joined)
                    self.assertIn('mcp_servers.tinkertom.default_tools_approval_mode="approve"', server_args)
                    self.assertEqual('approval_policy="never"' in server_args, True)
                    self.assertEqual('sandbox_mode="danger-full-access"' in server_args, permissions == 'yolo')
                    self.assertEqual('sandbox_mode="workspace-write"' in server_args, permissions == 'standard')
                    self.assertEqual(state.read(), saved)

    async def test_explicit_short_and_long_models_win_without_duplicate_default(self):
        for extra in (['-m', 'explicit-short'], ['--model=explicit-long']):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as d:
                workspace = Path(d)
                state = NativeState(workspace / '.tinkertom/native/codex')
                calls = []
                async def server(*argv, **kwargs):
                    calls.append(argv)
                    Path(argv[argv.index('--listen') + 1].removeprefix('unix://')).touch()
                    return SimpleNamespace(returncode=0)
                ui = SimpleNamespace(returncode=0, poll=lambda: 0)
                with patch('tinkertom.native.asyncio.create_subprocess_exec', side_effect=server), \
                        patch('tinkertom.native.subprocess.Popen', return_value=ui) as launch:
                    self.assertEqual(await codex_ui('codex', extra, workspace, Config().validate(), {}, state, 0), 0)
                command = launch.call_args.args[0]
                self.assertNotIn('gpt-daybreak-blue-latest', command)
                self.assertEqual(command[-len(extra):], extra)
                self.assertIn('approval_policy="never"', calls[0])
                self.assertIn('sandbox_mode="workspace-write"', calls[0])

    async def test_explicit_permission_flags_win_while_default_model_remains(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            state = NativeState(workspace / '.tinkertom/native/codex')
            calls = []
            async def server(*argv, **kwargs):
                calls.append(argv)
                Path(argv[argv.index('--listen') + 1].removeprefix('unix://')).touch()
                return SimpleNamespace(returncode=0)
            ui = SimpleNamespace(returncode=0, poll=lambda: 0)
            extra = ['-a', 'on-request', '-s', 'read-only']
            with patch('tinkertom.native.asyncio.create_subprocess_exec', side_effect=server), \
                    patch('tinkertom.native.subprocess.Popen', return_value=ui) as launch:
                self.assertEqual(await codex_ui('codex', extra, workspace, Config().validate(), {}, state, 0), 0)
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index('--model') + 1], 'gpt-daybreak-blue-latest')
            self.assertEqual(command[-len(extra):], extra)
            self.assertNotIn('approval_policy="never"', calls[0])
            self.assertFalse(any('sandbox_mode=' in arg for arg in calls[0]))
            self.assertNotIn('mcp_servers.tinkertom.default_tools_approval_mode="approve"', calls[0])


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
                    retry = calls[1]['argv']
                    self.assertIn('--resume', retry)
                    self.assertIn('Also update docs', retry[retry.index('--resume') + 2])
                    self.assertLess(retry.index('--resume'), retry.index('--mcp-config'))
                    self.assertNotIn('-p', calls[0]['argv'])
                    for call in calls:
                        self.assertIn('--dangerously-skip-permissions', call['argv'])
                        self.assertEqual(call['tool_search'], calls[0]['tool_search'])
                        self.assertEqual(call['argv'][call['argv'].index('--system-prompt-snapshot') + 1], 'off')
                        self.assertEqual(call['argv'][call['argv'].index('--disallowedTools') + 1],
                                         'Edit,Write,NotebookEdit,Bash')
                        if os.geteuid() == 0:
                            self.assertEqual(call['is_sandbox'], '1')
                    self.assertEqual([call['tool_search'] for call in calls], ['auto', 'auto'])
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
