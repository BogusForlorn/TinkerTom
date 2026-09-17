"""CLI adapters and conservative parsing of provider control events.

Tool output is deliberately never inspected for rate-limit/completion signals.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import re

from .config import Config


def command(config: Config, session_id: str | None) -> list[str]:
    executable = config.executable or config.provider
    if config.provider == "codex":
        argv = [executable, "exec"]
        if session_id:
            argv += ["resume"]
        argv += ["--json", "--skip-git-repo-check"]
        if config.permissions == "yolo":
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            # Config flags work on both exec and resume; --sandbox isn't accepted
            # directly by some versions of the resume subcommand.
            argv += ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="workspace-write"']
        if config.model:
            argv += ["--model", config.model]
        if session_id:
            argv += [session_id]
        return argv + ["-"]
    argv = [executable, "-p", "--output-format", "stream-json", "--verbose"]
    if config.permissions == "yolo":
        argv += ["--dangerously-skip-permissions"]
    else:
        argv += ["--permission-mode", "dontAsk", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash"]
    if config.model:
        argv += ["--model", config.model]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def reset_time(data, now: float) -> float | None:
    """Use explicit epochs, timezone-bearing ISO dates, or relative retry delays.

    Ambiguous local clock strings intentionally fall back to configured cooldown.
    Multiple windows are combined using the latest reset, including weekly limits.
    """
    candidates = []

    def epoch(value):
        try:
            timestamp = float(value)
            if timestamp > 1e12:
                timestamp /= 1000
            if math.isfinite(timestamp) and timestamp > now:
                candidates.append(timestamp)
                return
        except (ValueError, TypeError):
            pass
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo and dt.timestamp() > now:
                    candidates.append(dt.timestamp())
            except ValueError:
                pass

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = key.lower().replace("_", "").replace("-", "")
                if normalized in {"resetsat", "resetat", "resettime", "resetstimestamp"}:
                    epoch(item)
                elif normalized in {"retryafter", "retryafterseconds", "resetsinseconds"}:
                    try:
                        delay = float(item)
                        if math.isfinite(delay) and delay > 0:
                            candidates.append(now + delay)
                    except (ValueError, TypeError):
                        pass
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            for stamp in re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", value):
                epoch(stamp)
            for duration in re.findall(r"(?:try again|retry|resets?)\s+(?:after\s+|in\s+)((?:\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\s*)+)", value, re.I):
                delay = 0
                for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", duration, re.I):
                    delay += float(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit[0].lower()]
                if delay > 0:
                    candidates.append(now + delay)

    walk(data)
    return max(candidates) if candidates else None


RATE_PATTERN = re.compile(r"rate[ _-]?limit|usage[ _-]?limit|usageLimitReached|too many requests|\b429\b|(?:hit|reached|exceeded).{0,30}(?:limit|quota)|limit.{0,20}(?:reached|exceeded)", re.I)
AUTH_PATTERN = re.compile(r"unauthorized|unauthenticated|authentication|invalid.{0,10}(?:api.?key|token)|not logged in|please (?:log|sign) in|\b401\b|insufficient_quota|credit balance|billing|payment required|dangerously-skip-permissions.{0,60}root|permission denied|unknown (?:option|argument)|unexpected argument|model.{0,40}(?:not found|does not exist|not supported)", re.I)
SESSION_PATTERN = re.compile(r"(?:session|thread|conversation).{0,60}(?:not found|does not exist|expired)|no (?:conversation|session).{0,30}(?:found|with)", re.I)
CONTEXT_PATTERN = re.compile(r"context.{0,30}(?:exceeded|full|limit)|prompt is too long|maximum context length", re.I)


@dataclass
class Outcome:
    session_id: str | None = None
    final_text: str = ""
    success: bool = False
    error: str = ""
    error_kind: str = ""
    reset_at: float | None = None
    usage: dict = field(default_factory=dict)
    returncode: int = 0
    interrupted: bool = False

    def failure(self, data, now: float):
        message = data if isinstance(data, str) else json.dumps(data)
        self.error = message[-12000:]
        self.success = False
        if self.error_kind == "fatal" or AUTH_PATTERN.search(message):
            self.error_kind = "fatal"
        elif RATE_PATTERN.search(message):
            self.error_kind = "rate_limit"
            reset = reset_time(data, now)
            if reset:
                self.reset_at = max(self.reset_at or 0, reset)
        elif SESSION_PATTERN.search(message):
            self.error_kind = "session_missing"
        elif CONTEXT_PATTERN.search(message):
            self.error_kind = "context_full"
        elif self.error_kind not in {"rate_limit", "session_missing", "context_full"}:
            self.error_kind = "transient"


class Events:
    def __init__(self, provider: str, now):
        self.provider = provider
        self.now = now
        self.outcome = Outcome()

    def feed(self, line: str):
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        out = self.outcome
        kind = data.get("type")
        if self.provider == "codex":
            if kind == "thread.started":
                out.session_id = data.get("thread_id")
            elif kind == "item.completed":
                item = data.get("item") or {}
                if item.get("type") == "agent_message":
                    out.final_text = str(item.get("text", ""))[-64000:]
            elif kind in {"error", "turn.failed"}:
                out.failure(data, self.now())
            elif kind == "turn.completed":
                out.success = True
                out.error = out.error_kind = ""
                out.reset_at = None
                out.usage = data.get("usage") or {}
        else:
            if data.get("parent_tool_use_id"):
                return
            if kind in {"system", "result"} and data.get("session_id"):
                out.session_id = data["session_id"]
            if kind == "rate_limit_event":
                info = data.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    out.failure({"type": "rate_limit", **info}, self.now())
            elif kind == "assistant" and data.get("error"):
                out.failure(data, self.now())
            elif kind == "result":
                out.final_text = str(data.get("result", ""))[-64000:]
                out.usage = data.get("usage") or {}
                if "total_cost_usd" in data:
                    out.usage["reported_cost_usd"] = data["total_cost_usd"]
                if data.get("is_error") or data.get("subtype", "success") != "success":
                    # Turn caps should continue the existing session, not consume
                    # the network failure budget.
                    if data.get("subtype") == "error_max_turns":
                        out.error_kind = "turn_cap"
                    else:
                        out.failure(data, self.now())
                else:
                    out.success = True
                    out.error = out.error_kind = ""
                    out.reset_at = None


def parse_report(text: str, attempt: str) -> dict | None:
    value = text.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("attempt") != attempt:
        return None
    if data.get("status") not in {"continue", "complete", "blocked"}:
        return None
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return None
    if not isinstance(data.get("next_steps"), list) or not all(isinstance(x, str) for x in data["next_steps"]):
        return None
    if data["status"] == "complete" and data["next_steps"]:
        return None
    return data
