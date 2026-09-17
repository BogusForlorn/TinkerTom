"""Local MCP stdio server; optional agent calls use installed provider CLIs."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys

from .compact import compact
from .navigation import read_file, symbols


TOOLS = [
    {"name": "run", "description": "Run a shell command once with RTK, local Headroom, deduplication and bounded output. Saves logs and preserves exit status. Compression is lossy; raw=true returns exact output (potentially large).",
     "inputSchema": {"type": "object", "properties": {
         "command": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 256, "maximum": 64000, "default": 8000},
         "timeout": {"type": "number", "minimum": 1, "maximum": 900, "default": 900}, "raw": {"type": "boolean", "default": False}},
         "required": ["command"], "additionalProperties": False}},
    {"name": "read", "description": "Read a selected file range (default 100 lines) or one Python symbol. Prefer narrow reads to whole files.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer", "minimum": 1},
         "end": {"type": "integer", "minimum": 1}, "symbol": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"name": "symbols", "description": "List Python definitions and line spans before selecting a focused read.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"name": "wait_status", "description": "Read the local wrapper's saved cooldown. Does not contact providers, query /usage, claim resets or change billing.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "rubber_duck", "description": "Challenge a proposal with the opposite provider's CLI: Claude sessions use Codex GPT-5.6 Sol; Codex sessions use Claude Opus 4.6. Supply a concise rationale and relevant evidence, not hidden chain-of-thought. Read-only critique; identical supplied evidence is cached. The call waits through normal quota resets and is cancellable.",
     "inputSchema": {"type": "object", "properties": {"proposal": {"type": "string", "maxLength": 32000},
         "context": {"type": "string", "maxLength": 24000}, "files": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
         "force": {"type": "boolean", "default": False}}, "required": ["proposal"], "additionalProperties": False}},
    {"name": "code_worker", "description": "Delegate a bounded implementation to the current provider's smaller CLI model: GPT-5.6 Luna or Sonnet. Edits the working tree; writable workers are serialized. Give explicit files, requirements and tests. The coordinating model must review changes and verify acceptance. Waits through quota resets; does not recursively delegate.",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "maxLength": 32000},
         "context": {"type": "string", "maxLength": 24000}, "files": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}},
         "required": ["task", "files"], "additionalProperties": False}},
    {"name": "beads", "description": "Persistent project tasks, dependencies and memory using the local bd CLI. Initializes embedded storage on first use, without installing provider/git hooks. Create/claim tasks, link blockers, record progress, and close only after coordinator verification. No automatic remote push.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["init", "ready", "list", "create", "show", "claim", "update", "close", "depend", "remember", "recall"]},
         "id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
         "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "deferred"]},
         "notes": {"type": "string"}, "depends_on": {"type": "string"},
         "key": {"type": "string", "description": "Memory key: remember stores/updates it; recall fetches it. Recall without a key lists memories."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
         "required": ["action"], "additionalProperties": False}},
]


def call(name: str, args: dict, workspace: Path) -> tuple[str, bool]:
    if name not in {tool["name"] for tool in available_tools(workspace)}:
        raise ValueError(f"Tool is unavailable in this role/configuration: {name}")
    if name in {"rubber_duck", "code_worker"}:
        from .agents import agent_call
        return agent_call(name, args, workspace)
    if name == "beads":
        from .beads import beads_call
        return beads_call(workspace, args)
    if name == "run":
        limit, timeout = args.get("max_bytes", 8000), args.get("timeout", 900)
        if type(limit) is not int or not 256 <= limit <= 64000 or type(timeout) not in (int, float) or not 1 <= timeout <= 900:
            raise ValueError("max_bytes must be 256..64000; timeout must be 1..900 seconds")
        if not isinstance(args.get("command"), str) or not args["command"].strip():
            raise ValueError("command must be nonempty")
        if type(args.get("raw", False)) is not bool:
            raise ValueError("raw must be a boolean")
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = compact([], workspace, limit, timeout, shell=args["command"], raw=args.get("raw", False),
                           use_rtk=os.environ.get("TINKERTOM_RTK", "1") == "1",
                           headroom=os.environ.get("TINKERTOM_HEADROOM", "1") == "1")
        return f"exit_code: {code}\n{out.getvalue()}{err.getvalue()}", code != 0
    if name in {"read", "symbols"}:
        path = Path(args["path"])
        path = path if path.is_absolute() else workspace / path
        if name == "symbols":
            return symbols(path), False
        start = args.get("start", 1)
        end = args.get("end", start + 99)
        if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start > 999:
            raise ValueError("Use a range of 1..1000 lines")
        return read_file(path, start, end, args.get("symbol")), False
    if name == "wait_status":
        records = []
        for path in sorted((workspace / ".tinkertom/native").glob("*/state.json")):
            data = json.loads(path.read_text())
            records.append({"provider": path.parent.name, **{key: data.get(key) for key in ("session_id", "status", "next_run_at", "reason")},
                            "queued_items": len(data.get("pending_input", [])) + len(data.get("pending_prompts", []))})
        for path in sorted((workspace / ".tinkertom/agents").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            data = json.loads(path.read_text())
            records.append({"helper": path.parent.name, **{key: data.get(key) for key in ("provider", "model", "status", "next_run_at")}})
        return json.dumps(records), False
    raise ValueError(f"Unknown tool: {name}")


def available_tools(workspace):
    from .agents import active_config
    config = active_config(workspace)
    disabled = set()
    if not config.beads:
        disabled.add("beads")
    if not config.rubber_duck or os.environ.get("TINKERTOM_HELPER"):
        disabled.add("rubber_duck")
    if not config.delegate_coding or os.environ.get("TINKERTOM_HELPER"):
        disabled.add("code_worker")
    return [tool for tool in TOOLS if tool["name"] not in disabled]


def response(request: dict, workspace: Path) -> dict | None:
    if "id" not in request:
        return None
    result = {"jsonrpc": "2.0", "id": request["id"]}
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion")
        result["result"] = {"protocolVersion": version if version in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-03-26",
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "tinkertom", "version": "0.1.0"}}
    elif method == "ping":
        result["result"] = {}
    elif method == "tools/list":
        result["result"] = {"tools": available_tools(workspace)}
    elif method == "tools/call":
        try:
            text, error = call(params["name"], params.get("arguments") or {}, workspace)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            text, error = str(exc), True
        result["result"] = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        result["error"] = {"code": -32601, "message": f"Unknown method: {method}"}
    return result


async def serve_async(workspace):
    """Keep stdin responsive to MCP cancellation while long CLI tools run."""
    reader = asyncio.StreamReader(limit=1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    tasks = {}
    def emit(value):
        print(json.dumps(value), flush=True)
    async def tool(request):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "tinkertom.mcp_worker", str(workspace),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await proc.communicate(json.dumps(request).encode())
            if stdout:
                emit(json.loads(stdout))
            else:
                emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": f"Tool worker exited {proc.returncode}; inspect .tinkertom logs"}})
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
    try:
        while line := await reader.readline():
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Expected an object")
                if request.get("method") == "notifications/cancelled":
                    task = tasks.get((request.get("params") or {}).get("requestId"))
                    if task:
                        task.cancel()
                    continue
                if request.get("method") == "tools/call" and "id" in request:
                    identifier = request["id"]
                    if identifier in tasks:
                        emit({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32600, "message": "Duplicate active request ID"}})
                        continue
                    task = asyncio.create_task(tool(request))
                    tasks[identifier] = task
                    def finished(task, identifier=identifier):
                        tasks.pop(identifier, None)
                        if not task.cancelled() and task.exception():
                            emit({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32603, "message": str(task.exception())}})
                    task.add_done_callback(finished)
                    continue
                result = response(request, workspace)
                if result is not None:
                    emit(result)
            except (ValueError, TypeError):
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC input"}})
    finally:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        transport.close()


def serve(workspace: Path) -> int:
    asyncio.run(serve_async(workspace))
    return 0
