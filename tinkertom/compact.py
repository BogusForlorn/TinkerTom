"""Bounded command output with full logs and exact exit-status preservation."""

import os
import json
from pathlib import Path
import shlex
import shutil
import re
import signal
import subprocess
import sys
import uuid

from .process import run_process
from .optimization import deduplicate, rtk_available, rtk_path, suite_environment
from .storage import atomic_write


def preview(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size <= max_bytes:
            return stream.read().decode(errors="replace")
        head = stream.read(max_bytes // 2)
        stream.seek(-(max_bytes // 2), 2)
        tail = stream.read()
    critical = b""
    if size <= 1024 * 1024:
        data = path.read_bytes()
        middle = data[len(head):size - len(tail)]
        for line in middle.splitlines(keepends=True):
            if re.search(rb"\b(error|failed|failure|fatal|panic|exception|traceback)\b", line, re.I):
                critical += line[:max_bytes // 3 - len(critical)]
                if len(critical) >= max_bytes // 3:
                    break
        if critical:
            allowance = (max_bytes - len(critical)) // 2
            head, tail = data[:allowance], data[-allowance:]
    details = "\n[error context from omitted region]\n" + critical.decode(errors="replace") if critical else ""
    return head.decode(errors="replace") + f"\n[... {size - len(head) - len(tail) - len(critical)} bytes omitted; full log: {path} ...]\n" + details + tail.decode(errors="replace")


def compress_file(path: Path, directory: Path, headroom: bool) -> tuple[Path, dict]:
    metrics = {"raw_bytes": path.stat().st_size}
    # Bound compressor memory and time; very large streams go straight to preview.
    if path.stat().st_size > 1024 * 1024:
        return path, {**metrics, "skipped": "over 1 MiB; bounded preview only"}
    text = path.read_text(errors="replace")
    candidate = deduplicate(text)
    output = directory / (path.stem + ".compressed")
    atomic_write(output, candidate)
    metrics["after_dedup_bytes"] = len(candidate.encode())
    if headroom and len(candidate) >= 2000:
        try:
            proc = subprocess.run([sys.executable, "-m", "tinkertom.compress_worker", str(output)],
                                  env=suite_environment(), capture_output=True, timeout=15)
            if proc.returncode == 0:
                metrics["headroom"] = json.loads(proc.stdout)
            else:
                metrics["headroom"] = {"error": proc.stderr.decode(errors="replace")[-1000:]}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            metrics["headroom"] = {"error": str(exc)}
    metrics["compressed_bytes"] = output.stat().st_size
    return output, metrics


def compact(argv: list[str], workspace: Path, max_bytes: int = 8000, timeout: float = 900,
            *, use_rtk: bool = False, headroom: bool = True, raw: bool = False, shell: str | None = None) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv and shell is None:
        raise ValueError("Supply a command after --")
    if max_bytes < 256 or timeout <= 0:
        raise ValueError("max-bytes must be >= 256 and timeout must be positive")
    root = Path(os.environ.get("TINKERTOM_TASK_DIR", str(workspace / ".tinkertom")))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / "command-logs" / uuid.uuid4().hex[:12]
    original = shell if shell is not None else shlex.join(argv)
    rewritten = None
    if use_rtk and not raw and rtk_available():
        result = subprocess.run([rtk_path(), "rewrite", original], capture_output=True, text=True,
                                timeout=5, env=suite_environment(), cwd=workspace)
        # RTK 0.48: 3 is a usable rewrite with an ask rule. The enclosing
        # coding CLI still owns authorization; our hook never auto-allows tools.
        if result.returncode == 2:
            raise ValueError("RTK matched a deny rule; command was not executed")
        if result.returncode in (0, 3) and result.stdout.strip():
            rewritten = result.stdout.strip()
    if shell is not None or rewritten:
        argv = ["/bin/bash", "-c", rewritten or original]
    metadata = {"command": original, "rtk_command": rewritten, "headroom_enabled": headroom, "raw": raw}
    stopped = [False]
    old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in old:
        signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
    try:
        result = run_process(argv, cwd=workspace, prompt="", log_dir=directory,
                             timeout=timeout, stopped=lambda: stopped[0], env=suite_environment())
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        path = directory / f"{name}.log"
        if raw:
            if hasattr(stream, "buffer"):
                stream.flush()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, stream.buffer)
            else:
                with path.open(errors="replace") as source:
                    shutil.copyfileobj(source, stream)
        else:
            compressed, metrics = compress_file(path, directory, headroom)
            rendered = preview(compressed, max_bytes)
            print(rendered, end="", file=stream)
            metrics["displayed_bytes"] = len(rendered.encode())
            metadata[name] = metrics
    atomic_write(directory / "metrics.json", json.dumps(metadata, indent=2) + "\n")
    if not raw:
        print(f"\n[full logs: {directory}]", file=sys.stderr)
    if result.interrupted:
        return 130
    if result.error_kind:
        return 124
    return result.returncode if result.returncode >= 0 else 128 - result.returncode
