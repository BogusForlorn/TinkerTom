# Installed token reduction suite

| Layer | Integration | What it reduces |
| --- | --- | --- |
| RTK 0.48.0 | Native command rewrite through `compact --rtk` | Supported Git, test, listing and other command output |
| Headroom 0.37.0 | Local `compress()` library, isolated process | Structured/redundant command output; no API proxy |
| Repeated-line reduction | Built into `compact` | Identical adjacent lines with an explicit repeat count |
| ANSI removal and output bounds | Built into `compact` | Formatting noise and oversized outputs, with error-line context where available |
| Targeted file reads | `read --start/--end`, all text languages | Avoids reading entire files |
| AST navigation | `symbols` and `read --symbol`, Python | Retrieves one definition, including decorators |
| Short continuation prompts | Native session resume | Avoids repeating the objective/instructions each turn |
| Checkpoints and circuit breakers | Supervisor | Limits repeated exploration, malformed loops and stale context |

The checked-out sources are `.tools/src/rtk` and `.tools/src/headroom`, at pinned release commits recorded in `tools.lock.json`. RTK is installed from a checksum-verified upstream binary, and Headroom from the matching released Python wheel. This avoids installing a Rust compiler solely to build binaries already supplied upstream. The Python package includes its native Rust extension.

The headroom and rtk commands are on the suite PATH loaded by zsh. Reproduce the installation with:

```sh
.venv/bin/python scripts/install-tools.py
TinkerTom doctor
```

## Automatic coverage

For Claude, the runner passes its own `--settings` file containing a PreToolUse hook. Simple eligible Bash commands go through the suite. The hook does not automatically grant permission. Compound commands, redirects, environment assignments and shell-state changes are excluded to retain shell behavior. Already wrapped RTK/TinkerTom calls aren't wrapped again. Global Claude settings are not edited.

For Codex, the runner supplies concise instructions and the suite PATH. Compression is **agent-guided**, not a mandatory interception of every native tool. For guaranteed use on a particular command, ask it to execute `tinkertom compact --rtk -- COMMAND`. Claude's built-in Read/Grep/Glob and any other bypassed native tools do not receive shell-output compression.

## Headroom mode

The integration calls Headroom locally on new tool output before that output returns to the model. Neural Kompress is disabled, and no LLM API or compression proxy is called. Telemetry is disabled for suite-managed processes. Public tokenizer vocabularies are cached by the installer; metrics fall back to estimates if unavailable. The model name passed to the tokenizer is an accounting reference, not a model invocation.

This mode does not rewrite complete conversations, route subscription credentials through a proxy, add Serena globally or install Headroom's full optional ML/proxy stack. It preserves native provider prompt-cache prefixes. Smaller output doesn't imply the same percentage reduction in total input tokens, subscription usage or bills.

## Inspect results and recover detail

```sh
TinkerTom compact --rtk -- git status
TinkerTom compact --max-bytes 12000 -- npm test
TinkerTom compact --raw -- git diff
TinkerTom savings
rtk gain
```

Every command has its own `command-logs/<id>/` directory under its active task, or under the project's `.tinkertom/` when invoked separately. `metrics.json` records the RTK rewrite, captured bytes, deduplication, Headroom transforms and displayed bytes. `stdout.log` and `stderr.log` retain captured output; when RTK ran first, those logs are already RTK-filtered. `*.compressed` files retain the local compression result. `--raw` disables all filtering and the footer, preserving command output and exit status.

Compression can omit needed information. Read the retained log when diagnosing missing details; for pre-RTK output, run a suitable command with `--raw` if repeating that command is appropriate. Commands are never automatically re-executed to obtain an unfiltered baseline. Headroom processes are limited to 15 seconds and streams over 1 MiB use only bounded previews. A compression failure leaves the captured output available.

`savings` measures captured-versus-displayed bytes for the helper. RTK maintains separate estimates. These metrics don't double-count each other's reductions and are not a provider billing ledger. No model requests are used to calculate them.
