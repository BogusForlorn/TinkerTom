# Optional task chat, detach and follow-up messages

The default `tinkertom codex` / `tinkertom claude` opens the [native provider UI](native.md). This page covers the separate, optional task-chat interface. From your project directory:

```sh
TinkerTom chat --provider codex
# Enter the task when prompted, then keep typing follow-ups at “You ›”.

TinkerTom chat --provider claude "Implement SPEC.md" --verify 'npm test'
```

This is TinkerTom's terminal chat, not the native Codex/Claude full-screen interface. A separate worker runs the coding agent. The view prints turn summaries, starts, retries, cooldowns and blockers; it doesn't stream every internal tool token. The worker continues its unfinished objective even when you aren't typing.

Messages are queued immediately on disk and delivered at the **next turn boundary**. They do not interrupt an in-flight shell operation or model turn. For an urgent change, use `/stop`, wait until the task is paused, and send the new instruction. A follow-up sent to a stopped or completed task restarts it. Use `/resume` to continue a paused task without adding instructions.

When a provider is rate-limited, new messages remain queued until its saved reset deadline. No polling prompts, `/usage` calls, manual quota claims or paid extensions are used. The computer and worker must remain running, or a service must restart the worker later.

| Command | Behavior |
| --- | --- |
| `/detach`, `/quit`, `/exit`, Ctrl-C or Ctrl-D in the chat | Close the view, leave the worker running |
| `/stop` | Request a pause and terminate the active CLI process group |
| `/resume` | Restart the task after a pause/blocker has been resolved |
| `/status` | Show task status and saved reset timestamp |
| Other `/…` commands | Display help; never forward them to provider billing/usage controls |

You can also start without opening a view:

```sh
TinkerTom start --provider codex "Implement SPEC.md" --detach
TinkerTom attach TASK_ID
TinkerTom send TASK_ID "Also add a migration guide"
TinkerTom status TASK_ID
TinkerTom stop TASK_ID
```

`attach` connects to an existing task without creating a second agent. After completion, another message continues the saved session. You can attach from a new terminal, provided you're in the same project directory. Lowercase `tinkertom start` without `--detach` is a foreground worker: Ctrl-C there **stops** it, unlike Ctrl-C in the chat view.

Inbox messages are acknowledged only after a valid report from the agent. A crash before acknowledgement can cause a message to be replayed; delivery is durable and at least once, not exactly once. Messages arriving during completion checks prevent the task being marked complete until those messages have been handled. Old messages remain on disk as recovery evidence; only pending messages are injected into continuation prompts. A fresh native session recovers the checkpoint and last report and is instructed to read saved user-message history to recover requirements. Important requirements should also remain in checkpoints.
