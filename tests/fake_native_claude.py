"""Terminal/lifecycle fixture. Never contacts a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
session = args[args.index('--resume') + 1] if '--resume' in args else 'fake-native-session'


def hook(kind, **fields):
    result = subprocess.run([sys.executable, '-m', 'tinkertom.native_hooks'],
                            input=json.dumps({'hook_event_name': kind, 'session_id': session, **fields}),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


with Path('native-calls.jsonl').open('a') as log:
    log.write(json.dumps({'argv': args, 'cwd': os.getcwd(), 'at': time.time(), 'is_sandbox': os.environ.get('IS_SANDBOX')}) + '\n')
hook('SessionStart')
print('NATIVE CLAUDE UI', flush=True)
if '--resume' in args and args[-1] != session:
    print('CONTINUED ' + session, flush=True)
    hook('Stop')
for line in sys.stdin:
    text = line.strip()
    if text == '/help':
        print('NATIVE SLASH HELP', flush=True)
    elif text == '/exit':
        break
    elif text:
        reply = hook('UserPromptSubmit', prompt=text)
        if reply.get('decision') == 'block':
            print(reply['reason'], flush=True)
        else:
            hook('StopFailure', error='rate_limit', error_details='retry in 1 second')
            print('NATIVE QUOTA FAILURE', flush=True)
