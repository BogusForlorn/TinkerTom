from functools import lru_cache
from importlib.util import find_spec
import os
from pathlib import Path
import re
import shutil
import subprocess

SUITE_ROOT = Path(__file__).resolve().parent.parent


def suite_environment() -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(SUITE_ROOT / ".tools/bin"), str(SUITE_ROOT / ".venv/bin"), env.get("PATH", "")])
    env.update(RTK_TELEMETRY_DISABLED="1", HEADROOM_TELEMETRY_DISABLED="1", DO_NOT_TRACK="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
    env["TIKTOKEN_CACHE_DIR"] = str(SUITE_ROOT / ".tools/tokenizer-cache")
    env["HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS"] = "1"
    return env


def provider_environment(provider: str, permissions: str) -> dict:
    """Provider-only launch settings; never export them into the user's shell."""
    env = suite_environment()
    if provider == "claude" and permissions == "yolo" and os.geteuid() == 0:
        # Explicitly requested root-mode Claude launch. This is a Claude runtime
        # hint, not an OS sandbox, and must survive automatic session restarts.
        env["IS_SANDBOX"] = "1"
    return env


def rtk_path() -> str | None:
    local = SUITE_ROOT / ".tools/bin/rtk"
    return str(local) if local.is_file() else shutil.which("rtk")


@lru_cache(maxsize=1)
def rtk_available() -> bool:
    path = rtk_path()
    if not path:
        return False
    try:
        # A different crates.io project also uses the name rtk.
        result = subprocess.run([path, "gain"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=suite_environment())
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def headroom_available() -> bool:
    return find_spec("headroom") is not None


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def deduplicate(text: str) -> str:
    """Run-length encode identical adjacent lines, preserving exact counts."""
    lines = ANSI.sub("", text).splitlines(keepends=True)
    output = []
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and lines[end] == lines[index]:
            end += 1
        output.append(lines[index])
        if end - index > 1:
            output.append(f"[previous line repeated {end - index} times total]\n")
        index = end
    candidate = "".join(output)
    return candidate if len(candidate) < len(text) else text


def headroom_compress(text: str) -> tuple[str, dict]:
    if len(text) < 2000 or not headroom_available():
        return text, {"applied": False}
    # Headroom is a local library here. No proxy, model request, remote compressor,
    # conversation rewriting, automatic model download or credential changes.
    for key in ("HEADROOM_TELEMETRY_DISABLED", "DO_NOT_TRACK", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LITELLM_LOCAL_MODEL_COST_MAP", "TIKTOKEN_CACHE_DIR", "HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS"):
        os.environ[key] = suite_environment()[key]
    from headroom import compress
    result = compress([
        {"role": "user", "content": "Preserve errors, failures, warnings and anomalies in this command output."},
        {"role": "tool", "tool_call_id": "output", "name": "shell", "content": text},
    ], model="gpt-4o", protect_recent=0, kompress_model="disabled", compress_system_messages=False)
    candidate = result.messages[-1].get("content")
    metrics = {"applied": False, "tokens_before": result.tokens_before, "tokens_after": result.tokens_after,
               "transforms": result.transforms_applied, "tokenizer": "Headroom gpt-4o estimate; not provider billing"}
    if isinstance(candidate, str) and len(candidate) < len(text):
        metrics["applied"] = True
        return candidate, metrics
    return text, metrics
