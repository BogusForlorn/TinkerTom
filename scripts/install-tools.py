#!/usr/bin/env python3
"""Reproduce this suite's local installations from pinned upstream releases."""
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.prefix == sys.base_prefix:
        raise SystemExit("Run with .venv/bin/python scripts/install-tools.py")
    pins = json.loads((ROOT / "tools.lock.json").read_text())
    binary_dir = ROOT / ".tools/bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    for name in ("rtk", "headroom", "beads"):
        pin = pins[name]
        source = ROOT / ".tools/src" / name
        if not source.exists():
            subprocess.run(["git", "clone", "--depth", "1", "--branch", pin["tag"], pin["repository"], str(source)], check=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if commit != pin["commit"]:
            raise SystemExit(f"{source} is at a different revision; expected {pin['commit']}. Existing checkout left unchanged.")
    if platform.system() == "Linux" and platform.machine() == "x86_64":
        pin = pins["rtk"]
        binary = binary_dir / "rtk"
        if not binary.exists() or hashlib.sha256(binary.read_bytes()).hexdigest() != pin["binary_sha256"]:
            data = urllib.request.urlopen(pin["binary_url"], timeout=30).read()
            if hashlib.sha256(data).hexdigest() != pin["archive_sha256"]:
                raise SystemExit("RTK archive checksum mismatch")
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                member = next(m for m in archive.getmembers() if Path(m.name).name == "rtk" and m.isfile())
                binary.write_bytes(archive.extractfile(member).read())
                binary.chmod(0o755)
    else:
        subprocess.run(["cargo", "install", "--locked", "--path", str(ROOT / ".tools/src/rtk"), "--root", str(ROOT / ".tools")], check=True)
    if platform.system() == "Linux" and platform.machine() == "x86_64":
        pin = pins["beads"]
        binary = binary_dir / "bd"
        if not binary.exists() or hashlib.sha256(binary.read_bytes()).hexdigest() != pin["binary_sha256"]:
            data = urllib.request.urlopen(pin["binary_url"], timeout=30).read()
            if hashlib.sha256(data).hexdigest() != pin["archive_sha256"]:
                raise SystemExit("Beads archive checksum mismatch")
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                member = next(m for m in archive.getmembers() if Path(m.name).name == "bd" and m.isfile())
                contents = archive.extractfile(member).read()
                if hashlib.sha256(contents).hexdigest() != pin["binary_sha256"]:
                    raise SystemExit("Beads binary checksum mismatch")
                binary.write_bytes(contents)
                binary.chmod(0o755)
    elif not (binary_dir / "bd").exists():
        raise SystemExit("Install Beads for this platform using its official instructions; this lock pins the Linux x86_64 binary.")
    subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=headroom-ai", pins["headroom"]["distribution"], pins["chat"]["distribution"], pins["native_transport"]["distribution"]], check=True)
    for name in ("headroom", "tinkertom"):
        link = binary_dir / name
        if not link.exists():
            link.symlink_to(Path(sys.executable).parent / name)
    # Vocab caching is best-effort. Compression uses estimates if downloads are
    # unavailable; this is a public tokenizer vocabulary, not a model/API call.
    cache = ROOT / ".tools/tokenizer-cache"
    cache.mkdir(exist_ok=True)
    for name, expected in (("o200k_base", "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"),):
        url = f"https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken"
        target = cache / hashlib.sha1(url.encode()).hexdigest()
        if target.exists():
            continue
        try:
            data = urllib.request.urlopen(url, timeout=5).read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Tokenizer checksum mismatch")
            target.write_bytes(data)
        except (OSError, ValueError) as exc:
            print(f"Tokenizer cache unavailable ({exc}); Headroom can use estimates.")
    print("Suite installed. Run .venv/bin/tinkertom doctor")


if __name__ == "__main__":
    main()
