"""Native Codex TUI bridge: multiple clients <-> shared local app-server.

Only structured provider control events schedule retries. No terminal parsing,
keystroke injection, usage polling, quota-credit consumption or billing calls.
"""
import asyncio
import json
import time
import uuid

from .native_state import CONTINUE, schedule, wait_message
from .providers import reset_time


INSTRUCTIONS = """TinkerTom local suite is available through the tinkertom MCP server.
Prefer its run tool for verbose commands: RTK -> deduplication -> local Headroom -> bounded preview, with saved logs and exit status. Use focused read/symbols tools. Inspect raw logs when exact output matters. Avoid repeating full-file reads and unchanged context; retain native compaction and session history.
Work autonomously on the user's current coding task until complete or a concrete blocker needs input. Keep brief milestone/recovery notes in .tinkertom/checkpoint.md for long tasks; inspect working files before retrying interrupted actions.
Act as the coordinating and reviewing model. Use the code_worker MCP tool for substantial implementation: specify a bounded task, file scope and acceptance checks. It selects GPT-5.6 Luna or Sonnet; you retain planning and final review. Don't edit the same files concurrently with a worker. Inspect the resulting diff and independently run relevant acceptance checks before claiming completion. Small direct edits are reasonable when delegation would cost more than it saves.
Use rubber_duck to challenge significant plans, uncertain assumptions or consequential changes before finalizing. Supply a concise proposal and evidence; evaluate the critique rather than treating another model as ground truth. It uses the opposite provider's CLI. Avoid repeatedly asking for identical critiques; results are cached.
Use beads to track multi-step work, dependencies and durable discoveries. Check ready tasks/memory after compaction or resuming; create and claim bounded tasks, record progress/blockers, and close them only after review and verification. Don't create new beads for trivial chat. Long helper calls can wait through quota resets without polling; cancel the tool if the user cancels the work. Never recursively delegate from a helper.
Quota recovery is handled outside the model. Do not query /usage, /usage-credits or /extra-usage, claim/reset quota, purchase credits, enable overage, or switch billing/accounts/models/providers to bypass limits. Follow direct user instructions about their task and stop when they cancel it."""


def rate_error(error):
    return isinstance(error, dict) and error.get("codexErrorInfo") in ("usageLimitExceeded", "rateLimitExceeded")


def exhausted_windows(value, now):
    """Exclude a weekly reset unless that window is actually exhausted."""
    result = []
    if isinstance(value, dict):
        used = value.get("usedPercent")
        if isinstance(used, (int, float)) and used >= 100 and reset_time(value, now):
            result.append(value)
        else:
            for child in value.values():
                result.extend(exhausted_windows(child, now))
    return result


class CodexBridge:
    def __init__(self, upstream, workspace, environment, state, config, lease_fd=None):
        self.upstream, self.workspace, self.environment = upstream, workspace, environment
        self.state, self.config, self.lease_fd = state, config, lease_fd
        self.active = None
        self.requests = {}
        self.switches = {}
        self.internal = set()
        self.rate_limits = {}
        self.busy = False
        self.owner = None
        self.clients = {}

    def incoming(self, message, connection=0):
        """Returns an immediate response only when a turn is queued locally."""
        method, params = message.get("method"), message.get("params") or {}
        if method in {"thread/start", "thread/resume", "thread/fork", "account/rateLimits/read"}:
            self.requests[(connection, message.get("id"))] = method
        if method in {"thread/start", "thread/fork"} or (method == "thread/resume" and params.get("threadId") != self.active):
            self.switches[(connection, message.get("id"))] = (self.active, self.owner, self.busy)
            if self.active:
                # Keep this thread's deadline on disk. A picker can switch away
                # and resume it later without discarding its cooldown.
                self.active = None
        if method in {"thread/unsubscribe", "turn/interrupt", "thread/archive", "thread/delete"}:
            if params.get("threadId") == self.active and connection == self.owner:
                with self.state.edit() as data:
                    if data.get("status") == "waiting" and method != "thread/unsubscribe":
                        data.update(status="paused")
                if method != "turn/interrupt":
                    self.active = None
        if method in {"turn/start", "turn/steer"}:
            with self.state.edit() as data:
                if data.get("status") == "waiting" and data.get("session_id") == params.get("threadId"):
                    pending = data.setdefault("pending_input", [])
                    # Bound persisted queues; refuse explicitly rather than drop input.
                    if len(json.dumps(pending)) + len(json.dumps(params.get("input", []))) > 128000:
                        detail = "TinkerTom's pending queue is full. Save further instructions in a file."
                    else:
                        pending.extend(params.get("input", []))
                        detail = "Follow-up queued locally. " + wait_message(data)
                    return {"id": message.get("id"), "error": {"code": -32000, "message": detail}}
            if method == "turn/start":
                context = params.setdefault("additionalContext", {})
                if context is None:
                    context = params["additionalContext"] = {}
                context["tinkertom"] = {"kind": "application", "value": INSTRUCTIONS}
                message["params"] = params
        return None

    def outgoing(self, message, connection=0):
        """Observe provider replies; return False for our own request replies."""
        request_id = message.get("id")
        if (connection, request_id) in self.internal:
            self.internal.remove((connection, request_id))
            if message.get("error"):
                with self.state.edit() as data:
                    data.update(status="blocked", reason="Automatic turn/start was rejected: " + str(message["error"]))
            return False
        request = self.requests.pop((connection, request_id), None)
        origin = self.switches.pop((connection, request_id), None)
        if origin and "error" in message and self.active is None:
            # A missing or invalid saved session must not disable the current
            # thread's cooldown when the UI returns to it after the error.
            self.active, self.owner, self.busy = origin
        if request == "account/rateLimits/read" and "result" in message:
            self.rate_limits.update({k: v for k, v in (message["result"].get("rateLimits") or {}).items() if v is not None})
        if request in {"thread/start", "thread/resume", "thread/fork"} and "result" in message:
            thread = message["result"].get("thread") or {}
            self.active = thread.get("id")
            self.owner = connection
            self.busy = False
            if self.active:
                self.state.activate(self.active)
                with self.state.edit() as data:
                    if data.get("status") == "resuming":
                        # Recovery after a launcher crash: inspect the exact
                        # session before retrying work that may already have run.
                        data.update(status="waiting")
        method, params = message.get("method"), message.get("params") or {}
        if method == "account/rateLimits/updated":
            # Sparse snapshots are merged rather than replacing absent windows.
            self.rate_limits.update({k: v for k, v in (params.get("rateLimits") or {}).items() if v is not None})
            with self.state.edit() as data:
                if data.get("status") == "waiting":
                    reset = reset_time(exhausted_windows(self.rate_limits, time.time()), time.time())
                    if reset:
                        data["next_run_at"] = max(data["next_run_at"], reset + self.config.reset_buffer_seconds)
        if params.get("threadId") != self.active or not self.active or connection != self.owner:
            return True
        error = None
        if method == "turn/started":
            self.busy = True
        elif method == "error" and not params.get("willRetry", True) and rate_error(params.get("error")):
            error = params["error"]
        elif method == "turn/completed":
            self.busy = False
            turn = params.get("turn") or {}
            if turn.get("status") == "failed" and rate_error(turn.get("error")):
                error = turn["error"]
            elif turn.get("status") in {"completed", "interrupted"}:
                with self.state.edit() as data:
                    data.update(status="ready" if turn["status"] == "completed" else "paused", pending_input=[])
            elif turn.get("status") == "failed":
                with self.state.edit() as data:
                    data.update(status="blocked", reason=str(turn.get("error")))
        if error:
            with self.state.edit() as data:
                schedule(data, self.active, {"error": error, "windows": exhausted_windows(self.rate_limits, time.time())}, self.config)
                # Show the deadline in the native error display. Don't fake a
                # success or hide the provider's original failure classification.
                error["message"] = error.get("message", "Usage limit") + "\n" + wait_message(data)
        return True

    def retry(self, now=None):
        now = time.time() if now is None else now
        if self.busy or not self.active:
            return None
        with self.state.edit() as data:
            if data.get("status") != "waiting" or data.get("session_id") != self.active or data["next_run_at"] > now:
                return None
            identifier = "tinkertom-" + uuid.uuid4().hex
            self.internal.add((self.owner, identifier))
            # Keep inflight input on disk until a successful turn, so a second
            # quota failure doesn't lose follow-ups or require user resubmission.
            inputs = [{"type": "text", "text": CONTINUE}] + data.get("pending_input", [])
            data.update(status="resuming")
            self.busy = True
            return {"id": identifier, "method": "turn/start", "params": {
                "threadId": self.active, "input": inputs,
                "additionalContext": {"tinkertom": {"kind": "application", "value": INSTRUCTIONS}}}}

    async def handle(self, websocket):
        from websockets.asyncio.client import unix_connect
        connection = uuid.uuid4().hex
        tasks = []
        try:
            # Codex rejects Sec-WebSocket-Extensions on the control socket.
            async with unix_connect(self.upstream, compression=None, max_size=64 * 1024 * 1024, close_timeout=2) as upstream:
                self.clients[connection] = upstream

                async def client():
                    async for frame in websocket:
                        value = json.loads(frame)
                        reply = self.incoming(value, connection)
                        if reply is not None:
                            await websocket.send(json.dumps(reply))
                        else:
                            await upstream.send(json.dumps(value))

                async def server():
                    async for frame in upstream:
                        value = json.loads(frame)
                        if self.outgoing(value, connection):
                            await websocket.send(json.dumps(value))

                async def timer():
                    while True:
                        await asyncio.sleep(0.25)
                        if self.owner == connection:
                            value = self.retry()
                            if value:
                                await upstream.send(json.dumps(value))

                tasks = [asyncio.create_task(fn()) for fn in (client, server, timer)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.clients.pop(connection, None)
            self.requests = {key: value for key, value in self.requests.items() if key[0] != connection}
            self.switches = {key: value for key, value in self.switches.items() if key[0] != connection}
            self.internal = {key for key in self.internal if key[0] != connection}
            if self.owner == connection:
                self.owner = None
                self.active = None
