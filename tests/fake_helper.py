"""Offline fixture for CLI helper routing, exact resume and cancellation."""
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ['TT_FIXTURE_ROOT'])
provider = Path(sys.argv[0]).name
scenario = json.loads((root / 'scenario.json').read_text())
calls = root / 'calls.jsonl'

# Capability/auth probes are deliberately not review calls.
if '--version' in sys.argv[1:]:
    sys.exit(0 if scenario.get('remote_supported', True) else 2)
if sys.argv[1:] == ['auth', 'status']:
    marker = os.environ.get('TT_AUTH_PROBE_MARKER')
    if marker and marker == scenario.get('auth_probe_marker'):
        (root / 'auth_probe_marker_seen').write_text('seen\n')
    outcome = scenario.get('auth_status', 'invalid')
    if outcome == 'timeout':
        time.sleep(60)
    if outcome == 'true':
        print(json.dumps({'loggedIn': True}), flush=True)
    elif outcome == 'false':
        print(json.dumps({'loggedIn': False}), flush=True)
    elif outcome == 'secret':
        print(json.dumps({'loggedIn': True, 'email': 'sentinel@example.test',
                          'org': 'sentinel-org', 'path': '/sentinel/token'}), flush=True)
        print('stderr-sentinel@example.test stderr-sentinel-org /stderr-sentinel/token', file=sys.stderr,
              flush=True)
    elif outcome == 'oversized':
        print('x' * 17000, flush=True)
        time.sleep(.2)
    else:
        print('not-json', flush=True)
    sys.exit(0)
count = len(calls.read_text().splitlines()) if calls.exists() else 0
prompt = sys.stdin.read()
with calls.open('a') as out:
    out.write(json.dumps({'argv': sys.argv[1:], 'provider': provider, 'prompt': prompt,
        'cwd': os.getcwd(), 'at': time.time(), 'pid': os.getpid(),
        'helper': os.environ.get('TINKERTOM_HELPER'), 'native': os.environ.get('TINKERTOM_NATIVE_DIR'),
        'task_dir': os.environ.get('TINKERTOM_TASK_DIR'), 'attempt': os.environ.get('TINKERTOM_ATTEMPT'),
        'enable_tool_search': os.environ.get('ENABLE_TOOL_SEARCH'),
        'auth_env': {'copilot': bool(os.environ.get('COPILOT_GITHUB_TOKEN')),
                     'gh': bool(os.environ.get('GH_TOKEN')),
                     'github': bool(os.environ.get('GITHUB_TOKEN'))},
        'parent_identity': {key: os.environ.get(key) for key in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_PID', 'CODEX_THREAD_ID')},
        'is_sandbox': os.environ.get('IS_SANDBOX'), 'mcp_timeout': os.environ.get('MCP_TOOL_TIMEOUT')}) + '\n')

def emit(value):
    print(json.dumps(value), flush=True)

if provider == 'codex':
    emit({'type': 'thread.started', 'thread_id': 'helper-exact'})
elif provider == 'claude':
    emit({'type': 'system', 'subtype': 'init', 'session_id': 'helper-exact'})
step = scenario['steps'][min(count, len(scenario['steps']) - 1)]
if step == 'rate':
    reset = time.time() + scenario.get('wait', .5)
    if provider == 'codex':
        emit({'type': 'turn.failed', 'error': {'message': 'Usage limit reached', 'resets_at': reset}})
    else:
        emit({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'resetsAt': reset}})
        emit({'type': 'result', 'is_error': True, 'result': 'Usage limit reached'})
    sys.exit(1)
if step == 'sleep':
    time.sleep(60)
if step == 'auth':
    print('401 authentication failed', file=sys.stderr)
    sys.exit(1)
if step == 'oauth':
    print('Failed to authenticate: OAuth session expired and could not be refreshed', file=sys.stderr)
    sys.exit(1)
if step == 'http503':
    print('HTTP 503 service unavailable', file=sys.stderr)
    sys.exit(1)
if step == 'refusal':
    report = 'I cannot help with that request.'
elif step == 'review':
    report = 'Verdict: needs verification. Check the zero-length input boundary.'
else:
    (root / 'feature.txt').write_text('implemented\n')
    report = json.dumps({'attempt': os.environ['TINKERTOM_ATTEMPT'], 'status': 'complete',
        'summary': 'Implemented fixture change; coordinator must verify.', 'next_steps': []})
if provider == 'copilot':
    print(report, flush=True)
elif provider == 'codex':
    emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': report}})
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 5}})
else:
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'helper-exact',
        'result': report, 'usage': {'input_tokens': 12, 'output_tokens': 5}})
