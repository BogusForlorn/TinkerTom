"""CLI adapters and conservative parsing of provider control events.

Tool output is deliberately never inspected for rate-limit/completion signals.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def copilot_command(executable: str, model: str, remote_flags=()) -> list[str]:
    """One-shot, text-only, tool-less Copilot review invocation."""
    # With -p present Copilot ignores piped stdin, so prompt mode is selected
    # by the non-TTY stdin itself instead of passing a literal "-" prompt.
    return [executable, "--silent", "--model", model, "--reasoning-effort", "high",
            "--available-tools", "", "--disable-builtin-mcps", "--no-custom-instructions",
            "--no-ask-user", "--no-auto-update", *remote_flags,
            "--output-format", "text"]


def reset_time(data, now: float) -> float | None:
    """Use structured reset values, timezone-bearing ISO dates, or retry delays.

    As a fallback only, StopFailure ``error_details`` and
    ``last_assistant_message`` may contain an explicit timezone-qualified clock
    such as ``resets 2:50pm (Asia/Singapore)``. Bare/local clocks are ignored.
    Multiple structured windows are combined using the latest reset.
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

    clock_pattern = re.compile(
        r"\bresets\s+(\d{1,2})(?::([0-5]\d))?\s*([ap]m)?\s*\(([^()]+)\)", re.I
    )

    def clock_candidates(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in {"error_details", "last_assistant_message"}:
                    clock_candidates(item)
                elif isinstance(item, (dict, list)):
                    # Find the relevant StopFailure key without inspecting
                    # arbitrary scalar fields such as cwd.
                    clock_candidates(item)
        elif isinstance(value, list):
            for item in value:
                clock_candidates(item)
        elif isinstance(value, str):
            for match in clock_pattern.finditer(value):
                hour = int(match.group(1))
                minute = int(match.group(2) or 0)
                meridiem = (match.group(3) or "").lower()
                if meridiem:
                    if hour < 1 or hour > 12:
                        continue
                    hour = hour % 12 + (12 if meridiem == "pm" else 0)
                elif match.group(2) is None or hour > 23:
                    continue
                try:
                    zone = ZoneInfo(match.group(4).strip())
                except (ValueError, ZoneInfoNotFoundError):
                    continue
                zone_now = datetime.fromtimestamp(now, zone)
                naive = zone_now.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
                # Prefer the later occurrence for an ambiguous fall-back time.
                occurrence = naive.replace(tzinfo=zone, fold=1)
                roundtrip = occurrence.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
                if roundtrip != naive:
                    continue  # nonexistent spring-forward local time
                timestamp = occurrence.timestamp()
                if timestamp <= now:
                    if now - timestamp <= 5 * 60:
                        timestamp = now
                    else:
                        next_day = naive + timedelta(days=1)
                        occurrence = next_day.replace(tzinfo=zone, fold=1)
                        roundtrip = occurrence.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
                        if roundtrip != next_day:
                            continue
                        timestamp = occurrence.timestamp()
                candidates.append(timestamp)

    walk(data)
    if not candidates:
        clock_candidates(data)
    return max(candidates) if candidates else None


RATE_PATTERN = re.compile(r"rate[ _-]?limit|usage[ _-]?limit|usageLimitReached|too many requests|\b429\b|(?:hit|reached|exceeded).{0,30}(?:limit|quota)|limit.{0,20}(?:reached|exceeded)", re.I)
AUTH_PATTERN = re.compile(r"unauthorized|unauthenticated|authenticat(?:e|ion)|oauth|invalid.{0,10}(?:api.?key|token)|not logged in|please (?:log|sign) in|\b401\b|insufficient_quota|credit balance|billing|payment required|dangerously-skip-permissions.{0,60}root|permission denied", re.I)
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
        if self.provider == "copilot":
            separator = "\n" if self.outcome.final_text else ""
            self.outcome.final_text = (self.outcome.final_text + separator + line)[-64000:]
            self.outcome.success = True
            return
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
