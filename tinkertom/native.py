"""Launch the providers' actual terminal interfaces with local integrations."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tomllib

from .codex_proxy import CodexBridge
from .codex_proxy import CLAUDE_POLICY, coordinator_context, coordinator_policy
from .config import load_config
from .native_state import CONTINUE, NativeState
from .optimization import provider_environment, SUITE_ROOT
from .storage import Store, atomic_write


def mcp_config(workspace, config_path=None, provider=None, profile=None, helper=False, managed=False,
               tool_search=None):
    workspace = Path(workspace).resolve()
    args = ["-m", "tinkertom", "-C", str(workspace), "mcp"]
    if config_path is not None:
        args += ["--__tinkertom-config", str(Path(config_path).resolve())]
    if provider is not None:
        args += ["--__tinkertom-provider", provider]
    if profile is not None:
        args += ["--__tinkertom-profile", profile]
    if helper:
        args.append("--__tinkertom-helper")
    if managed:
        args.append("--__tinkertom-managed")
    result = {"command": sys.executable, "args": args}
    if provider == "claude":
        result["env"] = {"ENABLE_TOOL_SEARCH": tool_search or "auto"}
    return result


def claude_tool_search_mode(extra, environment=None, workspace=None):
    """Resolve Claude's dynamic tool-search mode without changing process env."""
    environment = os.environ if environment is None else environment
    mode = environment.get("ENABLE_TOOL_SEARCH")
    for index, arg in enumerate(extra):
        if arg == "--settings" or arg.startswith("--settings="):
            if arg == "--settings":
                if index + 1 >= len(extra) or extra[index + 1].startswith("-"):
                    raise ValueError("Claude --settings requires JSON or a file path")
                value = extra[index + 1]
            else:
                value = arg.split("=", 1)[1]
                if not value:
                    raise ValueError("Claude --settings requires JSON or a file path")
            if value.lstrip().startswith("{"):
                supplied = json.loads(value)
            else:
                path = Path(value)
                if workspace is not None and not path.is_absolute():
                    path = Path(workspace) / path
                supplied = json.loads(path.read_text())
            setting = supplied.get("env") if isinstance(supplied, dict) else None
            if isinstance(setting, dict) and setting.get("ENABLE_TOOL_SEARCH"):
                mode = setting["ENABLE_TOOL_SEARCH"]
    return mode or "auto"


def claude_compaction_environment(auto_compact_tokens):
    """Return Claude's official compaction environment values for a token limit."""
    return {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str((auto_compact_tokens * 100 + 74) // 75),
    }


def toml_value(value):
    """Serialize JSON-like data as an inline TOML value for ``codex -c``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def user_codex_hooks():
    """The user's configured PreToolUse groups; ``-c`` replaces the key, so keep them."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        groups = tomllib.loads(path.read_text()).get("hooks", {}).get("PreToolUse", [])
    except (OSError, ValueError):
        return []
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


def codex_settings(workspace, config_path=None, provider=None, profile=None, helper=False, managed=False,
                   auto_approve=False, auto_compact_tokens=None):
    server = mcp_config(workspace, config_path, provider, profile, helper, managed)
    settings = ["-c", "mcp_servers.tinkertom.command=" + json.dumps(server["command"]),
            "-c", "mcp_servers.tinkertom.args=" + json.dumps(server["args"]),
            "-c", "mcp_servers.tinkertom.tool_timeout_sec=604800"]
    hook = shlex.join([sys.executable, "-m", "tinkertom.hooks"])
    group = {"matcher": "^Bash$", "hooks": [{"type": "command", "command": hook, "timeout": 5,
                                              "statusMessage": "Checking TinkerTom command policy"}]}
    settings += ["-c", "hooks.PreToolUse=" + toml_value(user_codex_hooks() + [group])]
    if auto_approve:
        settings += ["-c", 'mcp_servers.tinkertom.default_tools_approval_mode="approve"']
    if auto_compact_tokens is not None:
        settings += ["-c", f"model_auto_compact_token_limit={auto_compact_tokens}",
                     "-c", 'model_auto_compact_token_limit_scope="total"']
    return settings


def claude_settings(directory, workspace, config, extra, config_path=None, provider=None, profile=None,
                    helper=False, managed=False, tool_search=None):
    """Combine explicit --settings hooks without editing provider files."""
    settings, forwarded = {}, []
    tool_search = tool_search or claude_tool_search_mode(extra, workspace=workspace)
    iterator = iter(extra)
    for arg in iterator:
        if arg == "--settings" or arg.startswith("--settings="):
            value = next(iterator, None) if arg == "--settings" else arg.split("=", 1)[1]
            if value is None:
                raise ValueError("Claude --settings requires JSON or a file path")
            if not value.lstrip().startswith("{"):
                path = Path(value)
                value = (path if path.is_absolute() else workspace / path).read_text()
            supplied = json.loads(value)
            if not isinstance(supplied, dict):
                raise ValueError("Claude --settings must contain an object")
            for kind, groups in supplied.pop("hooks", {}).items():
                settings.setdefault("hooks", {}).setdefault(kind, []).extend(groups)
            settings.update(supplied)
        else:
            forwarded.append(arg)
    if not isinstance(settings.get("env"), dict):
        settings["env"] = {}
    settings["env"]["ENABLE_TOOL_SEARCH"] = tool_search
    settings["env"]["TINKERTOM_COMMAND_COMPRESSION"] = "1" if config.claude_hook else "0"
    settings["env"].update(claude_compaction_environment(config.auto_compact_tokens))
    invocation = shlex.join([sys.executable, "-m", "tinkertom.native_hooks"])
    events = ["SessionStart", "UserPromptSubmit", "StopFailure", "Stop", "PostCompact"]
    events.append("PreToolUse")
    for kind in events:
        group = {"hooks": [{"type": "command", "command": invocation, "timeout": 5}]}
        if kind == "PreToolUse":
            group["matcher"] = "Bash"
        settings.setdefault("hooks", {}).setdefault(kind, []).append(group)
    path = directory / "claude-hooks.json"
    atomic_write(path, json.dumps(settings))
    mcp = directory / "mcp.json"
    atomic_write(mcp, json.dumps({"mcpServers": {"tinkertom": {"type": "stdio", **mcp_config(
        workspace, config_path, provider, profile, helper, managed, tool_search)}}}))
    return ["--settings", str(path), "--mcp-config", str(mcp)], forwarded


def stop_child(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def native_profile_args(extra):
    """Select the native review profile from an optional leading mode token."""
    extra = list(extra)
    if extra and extra[0] == "pentest":
        return "authorized_security", extra[1:]
    return "general", extra


def coordinator_edit_args(extra):
    """Consume TinkerTom's native opt-in without forwarding it to a provider."""
    extra = list(extra)
    separator = extra.index("--") if "--" in extra else len(extra)
    enabled = "--tt-coordinator-edits" in extra[:separator]
    extra = [arg for index, arg in enumerate(extra)
             if arg != "--tt-coordinator-edits" or index >= separator]
    return enabled, extra


def claude_model_override(extra):
    """Whether provider arguments explicitly select Claude's model."""
    for arg in extra:
        if arg == "--":
            break
        if arg == "--model" or arg.startswith("--model="):
            return True
        if arg == "-m" or (arg.startswith("-m") and not arg.startswith("--")):
            return True
    return False


def codex_start_permissions(config, extra):
    """Return only defaults not explicitly selected by provider arguments."""
    policy_override = False
    model_override = False
    for index, arg in enumerate(extra):
        if arg == "--":
            break
        option = arg.split("=", 1)[0]
        if option in {"--sandbox", "--ask-for-approval", "--approve-for-me", "--full-auto",
                      "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--permissions"}:
            policy_override = True
        if option in {"--model"} or (arg.startswith("-m") and not arg.startswith("--")):
            model_override = True
        if option == "--profile" or (arg.startswith("-p") and not arg.startswith("--")):
            # Profiles can select both model and permission settings.
            policy_override = model_override = True
        if arg.startswith(("-s", "-a")) and not arg.startswith("--"):
            policy_override = True
        value = None
        if arg in {"-c", "--config"} and index + 1 < len(extra):
            value = extra[index + 1]
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        if value:
            key = value.split("=", 1)[0].strip().strip('"')
            if key in {"approval_policy", "sandbox_mode", "permissions"} or key.startswith(("permissions.", "sandbox_workspace_write.")):
                policy_override = True
            if key in {"model", "model_provider"} or key.startswith("model."):
                model_override = True
    policy = {}
    if not policy_override:
        policy.update(approvalPolicy="never",
                      sandbox="danger-full-access" if config.permissions == "yolo" else "workspace-write")
    if not model_override:
        policy["model"] = config.model or config.codex_native_model
    return policy or None


async def codex_ui(executable, extra, workspace, config, env, state, lease_fd):
    from websockets.asyncio.server import unix_serve
    policy = codex_start_permissions(config, extra)
    settings = codex_settings(workspace, state.directory / "config.json", "codex",
                               config.rubber_duck_profile, managed=True,
                               auto_approve=bool(policy and policy.get("approvalPolicy") == "never"),
                               auto_compact_tokens=config.auto_compact_tokens)
    if policy and "approvalPolicy" in policy:
        settings += ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="' + policy["sandbox"] + '"']
    saved = state.read()
    if not extra and saved.get("status") in {"waiting", "resuming"} and saved.get("session_id"):
        extra = ["resume", saved["session_id"]]
    flags = []
    if policy and "model" in policy:
        flags += ["--model", policy["model"]]
    # Short private socket path avoids AF_UNIX limits in deeply nested projects.
    with tempfile.TemporaryDirectory(prefix="tt-") as runtime:
        socket_path = str(Path(runtime) / "codex.sock")
        upstream = str(Path(runtime) / "backend.sock")
        bridge = CodexBridge(upstream, workspace, env, state, config, lease_fd,
                             start_permissions=policy)
        with (state.directory / "app-server.log").open("ab") as log:
            backend = await asyncio.create_subprocess_exec(executable, *settings, "app-server", "--listen", "unix://" + upstream,
                cwd=workspace, env=env, stdout=log, stderr=log, start_new_session=True, pass_fds=(lease_fd,))
            try:
                deadline = time.monotonic() + 15
                while not Path(upstream).exists():
                    if backend.returncode is not None or time.monotonic() >= deadline:
                        raise ValueError(f"Codex app-server did not start. See {state.directory / 'app-server.log'}")
                    await asyncio.sleep(0.1)
                async with unix_serve(bridge.handle, socket_path, origins=[None], max_size=64 * 1024 * 1024, close_timeout=2):
                    command = [executable, "--remote", "unix://" + socket_path, *flags, *extra]
                    proc = subprocess.Popen(command, cwd=workspace, env=env, pass_fds=(lease_fd,))
                    try:
                        while proc.poll() is None:
                            await asyncio.sleep(0.1)
                        return proc.returncode
                    finally:
                        stop_child(proc)
            finally:
                if backend.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(backend.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(backend.wait(), 5)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(backend.pid, signal.SIGKILL)
                        await backend.wait()


def claude_ui(executable, extra, workspace, config, env, state, lease_fd):
    tool_search = claude_tool_search_mode(extra, env, workspace)
    env = dict(env)
    env["ENABLE_TOOL_SEARCH"] = tool_search
    settings, forwarded = claude_settings(state.directory, workspace, config, extra,
                                          state.directory / "config.json", "claude",
                                          config.rubber_duck_profile, managed=True,
                                          tool_search=tool_search)
    flags = ["--dangerously-skip-permissions"] if config.permissions == "yolo" else []
    explicit_model = claude_model_override(forwarded)
    selected_model = config.model
    if not explicit_model and not selected_model and config.rubber_duck_profile == "authorized_security":
        selected_model = config.claude_pentest_model
    if not explicit_model and selected_model:
        flags += ["--model", selected_model]
    if config.rubber_duck_profile in {"general", "authorized_security"}:
        policy = coordinator_policy(config.coordinator_edits)
        flags += ["--append-system-prompt", policy + CLAUDE_POLICY,
                  "--system-prompt-snapshot", "off",
                  "--disallowedTools", "Bash" if config.coordinator_edits else "Edit,Write,NotebookEdit,Bash"]
    base = [executable, *flags]
    saved = state.read()
    if not forwarded and saved.get("status") in {"waiting", "resuming"} and saved.get("session_id"):
        forwarded = ["--resume", saved["session_id"]]
    # Claude's --mcp-config is variadic. Keep an optional positional user prompt
    # before our trailing MCP flag so it isn't consumed as another config file.
    command = [executable, *flags, *forwarded, *settings]
    while True:
        terminal = termios.tcgetattr(sys.stdin.fileno())
        proc = subprocess.Popen(command, cwd=workspace, env=env, pass_fds=(lease_fd,))
        try:
            while proc.poll() is None:
                data = state.read()
                if data.get("status") == "waiting" and data["next_run_at"] <= time.time():
                    # The failed native turn has ended (StopFailure). Restart via
                    # an exact session ID; never send Enter into a quota dialog.
                    stop_child(proc)
                    with state.edit() as current:
                        if current.get("status") != "waiting":
                            return 0
                        prompt = CONTINUE + "\n\n" + "\n\n".join(current.get("pending_prompts", []))
                        current.update(status="resuming")
                        # Keep the positional continuation before Claude's
                        # variadic --mcp-config, just like the initial launch.
                        command = [*base, "--resume", current["session_id"], prompt, *settings]
                    break
                time.sleep(0.25)
            else:
                return proc.returncode
        finally:
            stop_child(proc)
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal)


def run_native(workspace: Path, provider: str, extra: list[str]) -> int:
    if os.environ.get("TINKERTOM_MANAGED"):
        raise ValueError("A managed agent cannot recursively open a native supervisor")
    profile, extra = native_profile_args(extra)
    changes = {"provider": provider, "rubber_duck_profile": profile}
    coordinator_edits, extra = coordinator_edit_args(extra)
    if coordinator_edits:
        changes["coordinator_edits"] = True
    path = workspace / "tinkertom.toml"
    # Keep Claude's historical config-less YOLO default. Codex now has its own
    # never/workspace-write default and must not change Claude launch behavior.
    if provider == "claude" and (not path.exists() or "permissions" not in tomllib.loads(path.read_text())):
        changes["permissions"] = "yolo"
    if "--tt-standard" in extra:
        extra.remove("--tt-standard")
        changes["permissions"] = "standard"
    if "--tt-executable" in extra:
        index = extra.index("--tt-executable")
        if index + 1 == len(extra):
            raise ValueError("--tt-executable requires a binary path")
        changes["executable"] = extra[index + 1]
        del extra[index:index + 2]
    config = load_config(workspace, changes)
    env = provider_environment(provider, config.permissions)
    executable = shutil.which(config.executable or provider, path=env["PATH"])
    if not executable:
        raise ValueError(f"{provider} CLI is not installed/on PATH. Install and sign into {provider}, then run tinkertom {provider} again.")
    if any(flag in extra for flag in ("--help", "-h", "--version", "-v" if provider == "claude" else "-V")):
        return subprocess.call([executable, *extra], cwd=workspace, env=env)
    if "--detach" in extra or "--enqueue" in extra:
        raise ValueError("Native UI mode stays attached to its terminal. Use tmux to detach it, or tinkertom start --provider " + provider + " 'task' --detach for the headless supervisor.")
    if any(arg == "--remote" or arg.startswith("--remote=") for arg in extra):
        raise ValueError("TinkerTom uses a private local --remote connection for reset supervision; external remote endpoints aren't supported.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Native UI needs a terminal. Use tinkertom start for headless tasks.")
    store = Store(workspace)
    with store.lease() as lease_fd:
        state = NativeState(store.root / "native" / provider)
        with state.edit() as saved:
            if saved.get("status") == "resuming":
                # A prior launcher died after sending its retry. The short
                # recovery prompt inspects native history before doing more work.
                saved["status"] = "waiting"
        atomic_write(state.directory / "config.json", json.dumps(config.to_dict()))
        env.update(TINKERTOM_MANAGED="1", TINKERTOM_NATIVE_DIR=str(state.directory), TINKERTOM_PROVIDER=provider,
                   TINKERTOM_RTK=str(int(config.rtk)), TINKERTOM_HEADROOM=str(int(config.headroom)))
        if provider == "claude":
            env["MCP_TOOL_TIMEOUT"] = "604800000"
            env["ENABLE_TOOL_SEARCH"] = claude_tool_search_mode(extra, env, workspace)
            env.update(claude_compaction_environment(config.auto_compact_tokens))
        else:
            for key in ("ENABLE_TOOL_SEARCH", *claude_compaction_environment(config.auto_compact_tokens)):
                env.pop(key, None)
        env["PYTHONPATH"] = str(SUITE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        terminal = termios.tcgetattr(sys.stdin.fileno())
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        signal.signal(signal.SIGINT, lambda *_: None)  # Native CLI handles Ctrl-C.
        def terminate(*_):
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, terminate)
        try:
            if provider == "codex":
                return asyncio.run(codex_ui(executable, extra, workspace, config, env, state, lease_fd))
            return claude_ui(executable, extra, workspace, config, env, state, lease_fd)
        except KeyboardInterrupt:
            return 130
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal)
            for sig, handler in old.items():
                signal.signal(sig, handler)
