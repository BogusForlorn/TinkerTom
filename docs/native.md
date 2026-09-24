# Native provider UI

```sh
cd /your/project
tinkertom codex
TinkerTom claude
```

Both spellings reach the same Python entry point. With no provider, Codex is selected. Plain native launches use the `general` rubber-duck profile; use `tinkertom codex pentest` or `tinkertom claude pentest` for the `authorized_security` profile. The exact leading mode token is consumed, while every later provider argument is preserved. To pass a literal leading provider argument or prompt named `pentest`, use the provider separator: `tinkertom codex -- pentest`. Enter prompts directly in the actual provider interface. Native slash commands, menus, history, model selection, MCP connections and repository instructions remain provider features. The wrapper doesn't implement a replacement chat or intercept keyboard input.

## Local tools

Each invocation adds an MCP server named `tinkertom`:

| Tool | Purpose |
| --- | --- |
| `run` | Execute a command once; RTK, repeated-line reduction, local Headroom, then bounded output. Preserve exit status and save logs. |
| `read` | Read a focused line range or Python definition. |
| `symbols` | Find Python definitions before reading their bodies. |
| `wait_status` | Read the local wait deadline and queue count without a provider request. |
| `rubber_duck` | Opposite-provider CLI critique using the configured model selected by the project review profile. |
| `code_worker` | Bounded implementation using GPT-5.6 Luna or Sonnet; coordinator reviews the changes. |
| `beads` | Persistent local tasks, dependencies and memory. |

See [model roles, Beads and helper controls](agents.md). These tools are enabled by default and can be disabled in project configuration.

Claude also gets a `PreToolUse` hook that wraps supported simple Bash commands. Compound shells and built-in file tools remain untouched. Codex receives stable application context recommending these MCP tools. It can still choose its native tools, so compression isn't guaranteed for every command. This is command-output compression, not full-transcript compression or an API proxy. Exact output is available through `raw`; raw output can be large.

The MCP server can also be configured manually in clients that support stdio:

```json
{
  "command": "/root/TinkerTom/.venv/bin/python",
  "args": ["-m", "tinkertom", "-C", "/absolute/project", "mcp"]
}
```

An MCP connection alone provides the tools. **Automatic waking requires the TinkerTom launcher to keep running**; a quota-limited model cannot reliably call a skill or tool to wake itself.

## What happens at a quota limit

**Codex:** the native TUI connects over a private local Unix socket to TinkerTom's bridge, which runs one shared installed Codex app-server. The bridge supports additional client connections used by `/resume` and isolates their request IDs. It forwards provider messages and watches structured failure notifications for the active thread. It saves the normal reset deadline. At that deadline, after the failed turn has ended, it submits one continuation to that same thread. No terminal key is injected, so the wrapper cannot accidentally press a quota-credit button. Ordinary provider slash-command requests pass through.

**Claude:** per-invocation lifecycle hooks record the exact session and a `StopFailure` event with `error: rate_limit`. The launcher keeps the native UI open while waiting. At the deadline it terminates the failed UI process, restores the terminal, and starts `claude --resume EXACT_SESSION_ID` with a short continuation and queued follow-ups. This reopens the UI; it does not preserve an unsent editor draft. Submit a follow-up to queue it before the reset if you need to keep it. The adapter requires a Claude version supporting `StopFailure` hooks.

For both adapters:

- Use an unambiguous reset timestamp or retry delay with the configured buffer (30 seconds by default). Otherwise wait five hours **from detecting the failure**, which may be later than the provider's actual reset.
- Respect a later exhausted weekly window when the provider supplies it. Do not delay until a weekly reset merely because a non-exhausted weekly window exists.
- Make no model requests during the cooldown. Submitted follow-ups are saved locally for the retry. The native UI may show a local “queued” error/blocked-prompt notice until the retry.
- Do not issue `/usage`, claim a manual reset, buy credits, enable extra usage, or switch accounts, providers or billing routes. Ordinary usage can still count toward the provider's shared weekly allowance; existing account billing settings remain in force.
- Keep working in the existing native conversation after the retry. Native task completion is the provider's normal agent behavior. It does **not** run the headless supervisor's JSON completion-report and acceptance-command loop; use `start --verify ...` when you need that separate contract.

## Closing, restarting and detaching

Normal native exit stops this launcher; closing the terminal is not detaching. Explicitly interrupting a waiting Codex thread pauses its continuation. Switching to another thread stops waking the old one while it is inactive; `/resume` restores its saved deadline and queued input. Opening and cancelling the session picker leaves the active thread unchanged. Pending deadlines are stored in `.tinkertom/native/PROVIDER/state.json`, with per-session archives under `sessions/`; `tinkertom native-status` displays the active state. A launcher restart with no extra provider arguments reopens its saved waiting session. A machine crash still requires restarting the launcher, and an interrupted retry can be delivered again. Check files before repeating side effects.

For a terminal you can detach and return to, use tmux:

```sh
tmux new -s tinkertom
tinkertom codex
# Ctrl-B, then D
tmux attach -t tinkertom
```

TinkerTom's `--detach` is reserved for `start`, which runs a headless task. That interface has its own task IDs, `attach`, `send`, acceptance checks and service recovery. Those commands don't attach to native UI sessions.

If an ongoing native Claude session was started outside tmux, there is currently no built-in way to detach that running terminal. From another terminal in the same project, run `tinkertom native-status` and note the Claude `session_id`. Finish or interrupt the current operation and exit the old Claude UI, then resume it inside tmux:

```sh
cd /your/project
tmux new -s tinkertom-claude
tinkertom claude --resume SESSION_ID
# Detach: Ctrl-B, then D
# Return later: tmux attach -t tinkertom-claude
```

The saved conversation and main-session cooldown survive this move; an in-flight helper MCP call does not. If the session was idle rather than waiting on quota, submit a continuation prompt after resuming to restart work.

## Configuration and verification

Provider arguments pass through, for example `tinkertom codex --model MODEL`, `tinkertom codex resume SESSION_ID`, or `tinkertom claude --resume SESSION_ID`. TinkerTom reserves `--tt-standard` for normal provider permissions and `--tt-executable PATH` for selecting a CLI binary. The selected native profile is written to the session config snapshot used by MCP rubber-duck calls; headless `start`/`chat` tasks continue to use their project TOML profile. It uses the current directory; put `-C DIR` before the provider to override it. Custom remote Codex endpoints are not supported because the bridge needs its local backend.

The requested default is YOLO unless the project's TOML explicitly sets permissions. For Claude running as root in YOLO mode, TinkerTom sets `IS_SANDBOX=1` in the child environment and passes `--dangerously-skip-permissions`. Automatic retries preserve both settings. The same launch environment is used by headless Claude tasks. The variable tells Claude to treat the environment as sandboxed; it does not create filesystem or network isolation, and isn't exported globally or added to Codex launches. Native standard mode retains the provider's normal permission UI; MCP `run` executes as the local OS user rather than inside the provider's shell sandbox.

For native Codex, TinkerTom applies its YOLO defaults to the private app-server (`approval_policy="never"`, `sandbox_mode="danger-full-access"`) and new-thread creation requests. Explicit CLI permission/profile selections take precedence over the new-thread default; resume requests and subsequent permission changes pass through unchanged. It does not inject a permission override flag into the remote TUI. Codex 0.154.0 rejects such UI overrides when resuming a remote task, including through `/resume`. Here “remote” refers to the UI's connection to the local TinkerTom bridge; it doesn't require a second machine. Use the project `permissions` setting or `--tt-standard` rather than adding client permission flags to a resume command. Provider policy checks still apply.

Global Codex/Claude configuration files aren't edited. Explicit Claude `--settings` are merged with TinkerTom's hook configuration. Disabling customizations, hooks, or MCP tools in the native client disables the associated integration.

Validated locally against **Codex CLI 0.153.4**, including selecting a previous conversation with native `/resume`, the `/model` menu and `/mcp` showing all seven tools connected after resuming, without a model prompt. Its app-server transport is experimental; CLI changes can require adapter updates. **Claude Code 2.1.266** is installed; helper CLI arguments were checked locally. Its reset/retry lifecycle is tested with a terminal fixture. Both adapters have simulated reset/retry and per-session restoration tests. Live helper model calls and a real five-hour authenticated quota cycle haven't been tested.

An isolated **Codex CLI 0.154.0** installation reproduced the remote-resume permission error with the old UI flag. With the fix, fresh sessions, an exact saved-session resume and startup recovery of a saved cooldown all retained YOLO mode and all seven MCP tools. The cooldown deadline remained unchanged. The in-UI `/resume` picker opened, but automated selection of the synthetic fixture did not complete, so that interaction still needs validation on 0.154.0. These checks created a fixture conversation without calling a model, then archived it; they did not update the system Codex installation or validate a real five-hour provider reset.

References: [Codex app-server](https://developers.openai.com/codex/app-server/), [Claude lifecycle hooks](https://code.claude.com/docs/en/hooks), [Claude CLI options](https://code.claude.com/docs/en/cli-reference), [MCP stdio](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports).
