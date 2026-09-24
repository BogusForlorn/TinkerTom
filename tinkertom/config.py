from dataclasses import asdict, dataclass, fields
from pathlib import Path
import math
import tomllib


@dataclass
class Config:
    provider: str = "codex"
    permissions: str = "standard"
    model: str = ""
    executable: str = ""
    cooldown_seconds: float = 18000
    reset_buffer_seconds: float = 30
    retry_seconds: float = 30
    max_failures: int = 5
    max_stalls: int = 5
    max_turns: int = 0
    turn_timeout_seconds: float = 3600
    verify_timeout_seconds: float = 900
    max_context_turns: int = 0
    checkpoint_chars: int = 12000
    rtk: bool = True
    headroom: bool = True
    claude_hook: bool = True
    beads: bool = True
    delegate_coding: bool = True
    rubber_duck: bool = True
    rubber_duck_profile: str = "general"
    codex_worker_model: str = "gpt-5.6-luna"
    claude_worker_model: str = "sonnet"
    codex_duck_model: str = "gpt-5.6-sol"
    claude_duck_model: str = "fable"
    codex_security_duck_model: str = "gpt-5.6-sol"
    claude_security_duck_model: str = "claude-opus-4-6"
    agent_timeout_seconds: float = 900
    worker_max_turns: int = 8
    verify: list[str] | None = None

    def validate(self):
        if self.provider not in {"codex", "claude"}:
            raise ValueError("provider must be codex or claude")
        if self.permissions not in {"standard", "yolo"}:
            raise ValueError("permissions must be standard or yolo")
        if self.rubber_duck_profile not in {"general", "authorized_security"}:
            raise ValueError("rubber_duck_profile must be general or authorized_security")
        for name in ("cooldown_seconds", "retry_seconds", "turn_timeout_seconds", "verify_timeout_seconds", "agent_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if type(self.reset_buffer_seconds) not in (int, float) or not math.isfinite(self.reset_buffer_seconds) or self.reset_buffer_seconds < 0:
            raise ValueError("reset_buffer_seconds must be finite and nonnegative")
        for name in ("max_failures", "max_stalls", "max_turns", "max_context_turns", "checkpoint_chars"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.max_failures < 1 or self.max_stalls < 1 or self.checkpoint_chars < 100:
            raise ValueError("max_failures/max_stalls must be positive; checkpoint_chars must be >= 100")
        if not isinstance(self.model, str) or not isinstance(self.executable, str):
            raise ValueError("model/executable must be strings")
        if any(type(getattr(self, name)) is not bool for name in ("rtk", "headroom", "claude_hook", "beads", "delegate_coding", "rubber_duck")):
            raise ValueError("Optimizer and orchestration switches must be booleans")
        for name in ("codex_worker_model", "claude_worker_model", "codex_duck_model", "claude_duck_model",
                     "codex_security_duck_model", "claude_security_duck_model"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a model name")
        if type(self.worker_max_turns) is not int or not 1 <= self.worker_max_turns <= 100:
            raise ValueError("worker_max_turns must be 1..100")
        if self.verify is None:
            self.verify = []
        if not isinstance(self.verify, list) or not all(isinstance(x, str) and x.strip() for x in self.verify):
            raise ValueError("verify must be a list of nonempty shell commands")
        return self

    def to_dict(self) -> dict:
        return asdict(self.validate())


def load_config(workspace: Path, overrides: dict | None = None) -> Config:
    path = workspace / "tinkertom.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    valid = {f.name for f in fields(Config)}
    unknown = data.keys() - valid
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    data.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return Config(**data).validate()
