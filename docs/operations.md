# Running across resets and reboots

The task directory is the recovery record:

```text
.tinkertom/
  runner.lock
  tasks/<id>/
    state.json            # objective, config, session, status, next_run_at, usage
    checkpoint.md         # agent-maintained milestone handoff
    last-report.json      # validated final report from the most recent turn
    events.jsonl          # supervisor transitions
    runner.log            # detached worker output
    stop                  # persistent stop request, if present
    inbox/*.json          # durable user messages; acknowledgements are in state
    claude-hooks.json     # per-invocation optimization hook settings
    attempts/000001/
      prompt.txt
      stdout.log
      stderr.log
      result.json
      verify-1/           # raw acceptance command output
```

State updates use a temporary file, file fsync, atomic rename and directory fsync. Native session history remains in the provider CLI's own storage; back that up separately if moving machines. Do not copy only `state.json` and expect native resume to work elsewhere.

New prompts can be sent from `TinkerTom attach TASK_ID` or `TinkerTom send TASK_ID "message"`. Delivery respects the saved cooldown; it never extends quotas. See [chat and messaging](chat.md). The wait-only policy prohibits automatic `/usage`, quota claims, buying credits and enabling overage. Existing account billing settings remain outside the wrapper's control.

`next_run_at` is a Unix timestamp. If the worker is killed while waiting, launching it again honors the same deadline. Local wall-clock corrections affect deadlines; the CLI timeout uses a monotonic clock. Numeric reset timestamps, timezone-bearing ISO dates and explicit relative retry durations are supported. Clock-only strings such as “resets 3pm” use the configured fallback instead of guessing a date/timezone.

## Linux user service

1. Create a pending task with `tinkertom -C /path/to/project start "Your objective" --provider codex --verify 'your-test-command' --enqueue`. This saves the task without making a model call. Configure the chosen CLI's login under the same OS account as the service.
2. Copy `examples/tinkertom.service` to `~/.config/systemd/user/tinkertom.service`. Replace the project path, virtualenv path, CLI binary PATH and `TASK_ID`.
3. Correct any saved authentication/configuration blocker before starting the service. Avoid running both a service and a detached worker at once; the workspace lease rejects the second worker.
4. Load and start the unit:

```sh
systemctl --user daemon-reload
systemctl --user enable --now tinkertom.service
journalctl --user -u tinkertom.service -f
```

For user services to start without an interactive login, your system needs user lingering enabled, usually with `loginctl enable-linger YOUR_USER`. This is host configuration, not something TinkerTom changes automatically. The service account must have access to the repository, native CLI session storage and authenticated credentials.

Stop using `systemctl --user stop tinkertom.service`. Completion exits 0; blocked tasks exit 2; deliberate pauses exit 3. The unit restarts unexpected failures and does not repeatedly restart completed, blocked or paused tasks. After correcting a blocker or deciding to resume a pause, explicitly run `systemctl --user restart tinkertom.service`; its `resume` command clears the stop marker. For simpler manual operation, keep using `resume --detach`; state persistence is identical.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `blocked` with authentication error | Authenticate the selected CLI interactively, then `resume TASK_ID` |
| “A runner already owns this workspace” | Check its status/logs and stop it, or use a separate worktree |
| Wait seems longer than five hours | Check `next_run_at`; a longer provider window may apply, or fallback began at detection |
| `completed` but no independent checks | Add `--verify` commands when creating the task; state records `completion_basis` |
| CLI repeatedly returns plain text | Inspect raw logs/version; the adapter requires Codex JSONL or Claude stream-json |
| Fresh session misses information | Inspect checkpoint quality and last report; disable optional context rotation |
| CLI cannot find RTK/commands under systemd | Set the service PATH to the installed binary directories |
| Claude refuses permission bypass under this account | Use an appropriate unprivileged account or a supported permission mode |

The stall detector compares the bounded checkpoint, final summary and Git changes. It is a heuristic: changing prose can look like progress, and valid investigation can leave files unchanged. Set a total `max_turns` limit if you need a deterministic bound. There is no hard cross-provider dollar budget; CLI token counters alone cannot accurately enforce one mid-request.
