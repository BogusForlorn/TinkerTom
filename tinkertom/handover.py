"""Constrained, provider-scoped coordinator handovers."""
import json
import math
import os
from contextlib import contextmanager
import fcntl
from pathlib import Path
import subprocess
import tempfile
import time


MARKER = "handover.json"
DEFAULT_TTL = 3600
MAX_FACTS = 12000
MAX_CONTEXT = 12000
TRUNCATION_NOTICE = "\n[handover checkpoint truncated at 12000 characters; read .tinkertom/checkpoint.md for the remainder]\n"


def _directory(workspace):
    root = workspace / ".tinkertom"
    if root.is_symlink():
        raise ValueError(".tinkertom symlink escape is not allowed")
    root.mkdir(exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ValueError(".tinkertom is not a directory")
    return root


def _provider():
    value = os.environ.get("TINKERTOM_PROVIDER", "")
    if value not in {"codex", "claude"}:
        raise ValueError("A valid current provider is required")
    return value


def _marker(workspace):
    path = _directory(workspace) / MARKER
    if path.is_symlink():
        raise ValueError("handover marker symlink escape is not allowed")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("handover marker is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("handover marker is invalid")
    return value


def active_marker(workspace, provider=None):
    marker = _marker(workspace)
    if not marker or not isinstance(marker.get("expires_at"), (int, float)):
        return None
    if marker["expires_at"] <= time.time():
        return None
    if provider is None:
        provider = os.environ.get("TINKERTOM_PROVIDER")
    return marker if marker.get("provider") == provider else None


def handover_context(workspace, provider):
    """Return a completed target handover checkpoint, or no context.

    This is deliberately read-only and never consumes the marker: a target
    launch can fail and be retried while the handover remains unexpired.
    Invalid or symlinked paths fail closed because this runs during provider
    startup and must not follow an unexpected path.
    """
    try:
        workspace = Path(workspace)
        root = workspace / ".tinkertom"
        marker_path = root / MARKER
        checkpoint_path = root / "checkpoint.md"
        if root.is_symlink() or not root.is_dir() or marker_path.is_symlink():
            return ""
        if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
            return ""
        marker = json.loads(marker_path.read_text())
        if (not isinstance(marker, dict) or marker.get("state") != "completed"
                or marker.get("target") != provider
                or marker.get("provider") not in {"codex", "claude"}
                or marker.get("source") != marker.get("provider")
                or marker.get("source") == marker.get("target")
                or not isinstance(marker.get("expires_at"), (int, float))
                or isinstance(marker.get("expires_at"), bool)
                or not math.isfinite(marker["expires_at"])
                or marker["expires_at"] <= time.time()):
            return ""
        with checkpoint_path.open("r", encoding="utf-8") as stream:
            text = stream.read(MAX_CONTEXT + 1)
        if len(text) > MAX_CONTEXT:
            text = text[:MAX_CONTEXT - len(TRUNCATION_NOTICE)] + TRUNCATION_NOTICE
        return ("\n\nTinkerTom handover checkpoint for this target provider:\n" + text)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return ""


def _save_json(path, value):
    directory = path.parent
    fd, name = tempfile.mkstemp(prefix=".handover-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _save_text(path, value):
    """Atomically write plain text, without JSON string encoding."""
    if path.is_symlink():
        raise ValueError(f"{path.name} symlink escape is not allowed")
    directory = path.parent
    fd, name = tempfile.mkstemp(prefix=".handover-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _handover_lock(workspace):
    directory = _directory(workspace)
    path = directory / "handover.lock"
    if path.is_symlink():
        raise ValueError("handover lock symlink escape is not allowed")
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _git_facts(workspace, source, target):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    facts = {"provider": _provider(), "source": source, "target": target,
             "branch": "(non-git)", "HEAD": "(non-git)", "dirty": False,
             "status": "(not a git worktree)"}

    def git(*args):
        return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=workspace,
                              env=env, text=True, capture_output=True, timeout=20)
    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return facts
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
        head = git("rev-parse", "--verify", "HEAD")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
        facts["branch"] = branch.stdout.strip() or "(detached)"
        facts["HEAD"] = head.stdout.strip() or "(unborn)"
        text = (status.stdout or status.stderr).strip()
        facts["status"] = text[:MAX_FACTS]
        facts["dirty"] = bool(text)
    except (OSError, subprocess.SubprocessError):
        facts["status"] = "(git inspection failed)"
    return facts


def _bead_items(value):
    if isinstance(value, dict):
        if value.get("status") == "in_progress" and value.get("id"):
            yield value
        for child in value.values():
            yield from _bead_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _bead_items(child)


def _is_helper_bead(bead):
    if bead.get("helper") is True or bead.get("is_helper") is True:
        return True
    kind = str(bead.get("type", "")).lower()
    bead_id = str(bead.get("id", "")).lower()
    title = str(bead.get("title", bead.get("name", ""))).lower()
    return kind == "helper" or bead_id.startswith("helper-") or "tinkertom helper" in title


def _bead_candidates(workspace, enabled):
    if not enabled:
        return {"available": False, "items": [], "reason": "Beads disabled by configuration"}
    from .beads import beads_call
    try:
        raw = beads_call(workspace, {"action": "list", "limit": 20})[0]
        value = json.loads(raw)
        items = []
        seen = set()
        for bead in _bead_items(value):
            if _is_helper_bead(bead) or bead["id"] in seen:
                continue
            seen.add(bead["id"])
            items.append(bead)
        return {"available": True, "items": items[:20]}
    except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"available": False, "items": [], "reason": str(exc)[:500] or "Beads unavailable"}


def begin(workspace, args):
    with _handover_lock(workspace):
        provider = _provider()
        target = "claude" if provider == "codex" else "codex"
        ttl = args.get("ttl_seconds", DEFAULT_TTL)
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ValueError("ttl_seconds must be 1..86400")
        now = time.time()
        marker = {"provider": provider, "state": "active", "created_at": now,
                  "expires_at": now + ttl, "source": provider, "target": target}
        path = _directory(workspace) / MARKER
        if path.is_symlink():
            raise ValueError("handover marker symlink escape is not allowed")
        _save_json(path, marker)
        facts = _git_facts(workspace, provider, target)
        from .agents import active_config
        candidates = _bead_candidates(workspace, active_config(workspace).beads)
        result = {**facts, "marker": {"state": "active", "expires_at": marker["expires_at"]},
                  "beads_candidates": candidates}
        return json.dumps(result)


def _bead_status(workspace, bead_id):
    from .beads import beads_call
    raw = beads_call(workspace, {"action": "show", "id": bead_id})[0]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Selected Bead did not return JSON") from exc
    if isinstance(value, dict) and value.get("status"):
        return value
    found = []
    def walk(item):
        if isinstance(item, dict):
            if item.get("id") == bead_id and item.get("status"):
                found.append(item)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value)
    if not found:
        raise ValueError("Selected Bead was not found")
    return found[0]


def finalize(workspace, args):
    with _handover_lock(workspace):
        provider = _provider()
        marker = _marker(workspace)
        if (not marker or marker.get("state") != "active"
                or not isinstance(marker.get("expires_at"), (int, float))
                or marker["expires_at"] <= time.time()
                or marker.get("source") != provider
                or marker.get("provider") != provider):
            raise ValueError("No unexpired active handover belongs to the current provider")
        from .agents import active_config
        config = active_config(workspace)
        checkpoint = args.get("checkpoint")
        limit = config.checkpoint_chars
        if not isinstance(checkpoint, str) or not checkpoint.strip():
            raise ValueError("checkpoint must be nonempty")
        bead_id = args.get("bead_id")
        notes = args.get("notes", "")
        if bead_id is not None:
            if not isinstance(bead_id, str) or not bead_id.strip():
                raise ValueError("bead_id must be nonempty")
            if not isinstance(notes, str) or not notes.strip() or len(notes) > 16000:
                raise ValueError("notes must be nonempty and bounded when bead_id is supplied")
            if not config.beads:
                raise ValueError("Beads are disabled; bead_id cannot be supplied")
        facts = _git_facts(workspace, marker["source"], marker["target"])
        verified = ("# Handover checkpoint\n\n## Verified facts\n"
                    f"- Source: {facts['source']}\n- Target: {facts['target']}\n"
                    f"- Branch: {facts['branch']}\n- HEAD: {facts['HEAD']}\n"
                    f"- Dirty: {'yes' if facts['dirty'] else 'no'}\n"
                    f"- Status:\n\n```text\n{facts['status']}\n```\n\n"
                    "## Coordinator narrative\n\n" + checkpoint.strip() + "\n")
        if len(verified) > limit:
            raise ValueError(f"complete checkpoint must be at most {limit} characters")
        if bead_id is not None:
            bead = _bead_status(workspace, bead_id)
            if bead.get("status") != "in_progress":
                raise ValueError("Selected Bead must be in_progress")
            from .beads import append_bead_notes
            try:
                append_bead_notes(workspace, bead_id, notes)
            except (OSError, ValueError) as exc:
                raise ValueError("Partial failure: Beads notes were not appended; checkpoint was not installed.") from exc
        path = _directory(workspace) / "checkpoint.md"
        try:
            _save_text(path, verified)
            marker.update(state="completed", completed_at=time.time())
            _save_json(_directory(workspace) / MARKER, marker)
        except (OSError, ValueError) as exc:
            raise ValueError("Partial failure: checkpoint or handover marker installation failed; "
                             f"cross-store state may be incomplete ({exc}).") from exc
        return json.dumps({"provider": provider, "checkpoint": str(path), "state": "completed",
                           "cross_store_atomic": False,
                           "message": "Checkpoint installed; any Beads note and checkpoint are not cross-store atomic."})


def abort(workspace):
    with _handover_lock(workspace):
        provider = _provider()
        marker = active_marker(workspace, provider)
        if not marker:
            raise ValueError("No unexpired handover belongs to the current provider")
        path = _directory(workspace) / MARKER
        path.unlink()
        return json.dumps({"provider": provider, "state": "aborted"})


def handover_call(workspace, args):
    action = args.get("action")
    if action == "begin":
        return begin(workspace, args), False
    if action == "finalize":
        return finalize(workspace, args), False
    if action == "abort":
        return abort(workspace), False
    raise ValueError("action must be begin, finalize or abort")
