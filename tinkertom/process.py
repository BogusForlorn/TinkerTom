"""Stream CLI output to disk with bounded memory, timeouts and tree cleanup."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

from .providers import Events, Outcome


def terminate_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_process(argv: list[str], *, cwd: Path, prompt: str, log_dir: Path,
                timeout: float, stopped, provider: str | None = None,
                on_session=None, lease_fd: int | None = None, env: dict | None = None,
                timeout_paused=None) -> Outcome:
    log_dir.mkdir(parents=True, exist_ok=True)
    events = Events(provider or "codex", time.time)
    out = events.outcome
    buffers = {"stdout": b"", "stderr": b""}
    discarded = {"stdout": False, "stderr": False}
    error_tail = b""
    last_session = None
    started = time.monotonic()
    checked, elapsed = started, 0.0
    def timed_out():
        nonlocal checked, elapsed
        now = time.monotonic()
        if not timeout_paused or not timeout_paused():
            elapsed += now - checked
        checked = now
        return elapsed >= timeout
    with tempfile.TemporaryFile() as stdin, (log_dir / "stdout.log").open("wb") as stdout_log, (log_dir / "stderr.log").open("wb") as stderr_log:
        stdin.write(prompt.encode())
        stdin.seek(0)
        proc = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                pass_fds=(lease_fd,) if lease_fd is not None else (), env=env)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, ("stdout", stdout_log))
                selector.register(proc.stderr, selectors.EVENT_READ, ("stderr", stderr_log))
                while selector.get_map():
                    if stopped():
                        out.interrupted = True
                        terminate_group(proc)
                        break
                    if timed_out():
                        out.failure("CLI process timed out", time.time())
                        terminate_group(proc)
                        break
                    for key, _ in selector.select(timeout=0.25):
                        name, log = key.data
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            if buffers[name] and provider and name == "stdout" and not discarded[name]:
                                events.feed(buffers[name].decode(errors="replace"))
                            selector.unregister(key.fileobj)
                            continue
                        log.write(chunk)
                        log.flush()
                        if name == "stderr":
                            error_tail = (error_tail + chunk)[-64000:]
                        if not provider or name != "stdout":
                            continue
                        buffers[name] += chunk
                        while b"\n" in buffers[name]:
                            line, buffers[name] = buffers[name].split(b"\n", 1)
                            if not discarded[name]:
                                events.feed(line.decode(errors="replace"))
                            discarded[name] = False
                            if out.session_id and out.session_id != last_session:
                                last_session = out.session_id
                                if on_session:
                                    on_session(last_session)
                        # Preserve raw output on disk; never let a tool's enormous
                        # JSON event exhaust the supervisor's memory.
                        if len(buffers[name]) > 2 * 1024 * 1024:
                            buffers[name] = b""
                            discarded[name] = True
                # A child may close its pipes but keep running. Continue enforcing
                # stop/timeout instead of blocking indefinitely in wait().
                while proc.poll() is None:
                    if stopped() or timed_out():
                        out.interrupted = stopped()
                        if not out.interrupted:
                            out.failure("CLI process timed out", time.time())
                        terminate_group(proc)
                        break
                    time.sleep(0.1)
                out.returncode = proc.wait()
        finally:
            terminate_group(proc)
            proc.stdout.close()
            proc.stderr.close()
    if out.session_id and out.session_id != last_session and on_session:
        on_session(out.session_id)
    if provider and not out.interrupted and out.returncode != 0:
        out.success = False
        if not out.error_kind:
            out.failure(error_tail.decode(errors="replace") or f"CLI exited {out.returncode}", time.time())
    if provider and not out.success and not out.error_kind and not out.interrupted:
        out.failure(error_tail.decode(errors="replace") or "CLI ended without a successful result event", time.time())
    return out
