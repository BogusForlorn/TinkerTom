# Coding workers, rubber duck and Beads

Start the native UI as usual from your project:

```sh
tinkertom codex
# or
tinkertom claude
```

The per-invocation TinkerTom MCP server exposes `code_worker`, `rubber_duck` and `beads`, alongside RTK/Headroom `run`, focused `read`, `symbols` and `wait_status`. No separate provider API client, API proxy or Copilot CLI is used. Sign into both installed CLIs to use opposite-provider reviews. Existing CLI authentication and account billing settings apply.

## Model roles

| Main UI | Coordinator/reviewer | Implementation worker | Rubber duck |
| --- | --- | --- | --- |
| Codex | Your selected native model, such as GPT-6 Astra | Codex CLI `gpt-5.6-luna`, medium reasoning | Configured Claude model, high effort |
| Claude Code | Your selected native model, such as Opus | Claude Code `sonnet`, medium effort | Configured Codex model, high reasoning |

The coordinator receives instructions to delegate substantial implementation with explicit file scope, requirements and acceptance checks. It should inspect the worker's diff, run relevant checks independently, and own final acceptance. This is an instructed workflow, not an automatic interception of every edit; trivial edits can remain with the main model. Selecting a smaller main model with `/model` also changes the coordinator: TinkerTom does not override that choice.

Writable helpers run one at a time per workspace, under a local lock. The main agent must avoid editing their files concurrently. Workers inherit the project's configured permissions and have access to compression/navigation and Beads MCP tools. Their TinkerTom tool list excludes delegation/review tools, and recursive helper calls are rejected. File scope is a model instruction, not an OS sandbox; YOLO workers retain broad filesystem access. Workers return a structured completion/blocker report, and the coordinator must check the actual changes.

Rubber duck receives a bounded proposal, concise rationale, optional context and selected file excerpts. It challenges assumptions, correctness, edge cases and verification. It runs from a temporary directory: Codex uses a read-only sandbox and ignores user-config tools; Claude runs with native tools disabled and an empty strict MCP configuration. It cannot implement the proposal. Supplied file excerpts are limited to 8,000 characters per file and 24,000 total, so the coordinator must supply relevant evidence. Model agreement does not establish correctness.

Identical review requests and supplied evidence reuse a saved critique. Changed excerpts/context produce a new cache key; `force: true` requests a fresh critique. The cache does not infer changes elsewhere in the repository. Implementation calls always run again, because the working tree may have changed.

You can prompt the coordinator naturally, for example:

> Track this feature in Beads. Delegate the parser implementation to a code worker, use rubber duck to challenge the error-handling plan, and review the changes and tests before closing the task.

These are MCP tools used by the model, not new provider slash commands. Native `/resume`, `/model` and `/mcp` remain provider commands. In Codex, `/mcp` should show `tinkertom: connected (7 tools)` when all integrations are enabled.

## Persistent tasks and memory

[Beads](https://github.com/gastownhall/beads) **v1.2.2** is cloned under `.tools/src/beads`; its executable is `.tools/bin/bd`. Source commit and per-platform download hashes are pinned in `tools.lock.json`. `setup.sh` selects checksum-verified releases for Linux ARM64/x86_64 and macOS ARM64/Intel. The macOS binaries target macOS 26; older macOS builds the same pinned source with CGO and `gms_pure_go` enabled, then caches a receipt containing the source commit, platform, macOS version and built binary hash. This preserves the embedded Dolt database; no separate server is started. See the [installation instructions](../README.md#installation) for prerequisites.

The first `beads` tool call initializes `.beads` in the current workspace with `--stealth`, keeping Beads files locally excluded from Git and preventing its bootstrap commit. It skips provider instructions and Git/provider hook installation, and wrapper calls use `--sandbox` to disable automatic remote pushes. Calls are serialized because the embedded store permits one writer at a time. Direct `bd` commands bypass the wrapper's lock; avoid running them concurrently with an agent's Beads call. This local task store is not automatically backed up or shared with a Git remote.

| Action | Arguments/purpose |
| --- | --- |
| `init` | Initialize local storage explicitly |
| `create` | `title`, optional `description` including acceptance checks |
| `ready`, `list` | Bounded task listings; optional `limit` (1–100) |
| `show`, `claim` | Explicit issue `id` |
| `update` | `id`, `status`, optional progress `notes` |
| `depend` | `id` is blocked by `depends_on` |
| `close` | `id`, verification evidence in `notes`; coordinator owns acceptance |
| `remember` | `notes`, optional stable `key` for updating a memory |
| `recall` | A `key` retrieves a memory; omit it to list memories |

The coordinator is instructed to check ready tasks and memory after resuming or compaction. Beads stores task facts and decisions; provider session history still supplies conversational context. Neither replaces acceptance checks.

## Waiting, cancellation and recovery

Each helper invokes `codex exec` or `claude -p`, saves its exact native session ID immediately, and parses structured CLI events. On a quota failure it keeps the MCP call pending, saves the reset deadline and waits locally, without model requests. At the explicit deadline plus buffer, or the five-hour fallback plus buffer, it resumes that same CLI/model/session. It never calls `/usage`, claims a reset, enables extra usage or switches routes to bypass a quota. An exhausted weekly allowance can require a longer normal wait.

Helpers receive independent session environments: the launcher clears inherited Claude nesting/session markers and the parent Codex thread identifier. TinkerTom's helper-role guard remains active to prevent recursive delegation through its MCP tools.

MCP tool timeouts are configured to seven days. The server remains responsive to ping, other tools and cancellation while helpers wait. The headless supervisor excludes its live helper's quota cooldown from the parent CLI timeout. Helper CLI invocations still have a 900-second default timeout; quota cooldown itself is outside that timeout. Authentication/model access failures return a blocker instead of silently substituting a model.

Cancelling the tool stops its child process group and saves a paused record. Closing the native launcher is not detaching; keep it alive, or use tmux. A pending MCP call is not restored across closing/reopening the client. Helper request, state, usage fields, critique/report and attempt logs remain under `.tinkertom/agents/`; the coordinator must inspect those and the working tree before reassigning cancelled work. Native main-session reset recovery is described in [native.md](native.md).

## Controls and cost

Defaults are active in every workspace, with optional per-project overrides in `tinkertom.toml`:

```toml
beads = true
delegate_coding = true
rubber_duck = true
rubber_duck_profile = "general"       # or "authorized_security"
codex_worker_model = "gpt-5.6-luna"
claude_worker_model = "sonnet"
codex_duck_model = "gpt-5.6-sol"
claude_duck_model = "fable"
codex_security_duck_model = "gpt-5.6-sol"
claude_security_duck_model = "claude-opus-4-6"
agent_timeout_seconds = 900
worker_max_turns = 8
```

`worker_max_turns` bounds successful helper CLI turns, not the number of internal tool calls or quota retries. A helper that exhausts the budget returns a blocker for coordinator inspection. Native sessions read configuration at launch; existing headless tasks retain their saved configuration.

Smaller workers, bounded evidence, cached identical critiques, focused reads and RTK/Headroom reduce avoidable context. Delegation and independent reviews also consume tokens and can cost more for small tasks. No universal savings percentage is claimed. Helper usage fields are saved when the CLI supplies them; `tinkertom savings` measures command-output reduction, not total account billing.

Model references: [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [Claude model configuration](https://code.claude.com/docs/en/model-config). Availability depends on the signed-in CLI account.
