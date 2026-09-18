#!/usr/bin/env python3
"""Reproduce this suite's local installations from pinned upstream releases."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def platform_key(system=None, machine=None):
    """Normalize uname/Python platform names to a pinned release target."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    if system not in {"linux", "darwin"} or machine not in {"arm64", "x86_64"}:
        raise SystemExit(f"Unsupported platform: {system}/{machine}. Supported: macOS/Linux ARM64 and x86_64; use WSL on Windows.")
    return f"{system}-{machine}"


def installation_mode(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", action="store_true",
                        help="Install into the current non-venv Python; explicitly allow pip --break-system-packages")
    parser.add_argument("--shell", choices=("zsh", "bash", "none"), default="none",
                        help="Add or update TinkerTom's block in the selected user's shell startup file")
    parser.add_argument("--platform", help="uname-derived release target supplied by setup.sh")
    args = parser.parse_args(argv)
    in_venv = sys.prefix != sys.base_prefix
    if args.system and in_venv:
        parser.error("--system requires a Python outside a venv. Deactivate it or choose your system Python explicitly.")
    if not args.system and not in_venv:
        parser.error("Use .venv/bin/python scripts/install-tools.py, or explicitly select --system for a non-venv install.")
    if sys.version_info < (3, 11):
        parser.error("TinkerTom requires Python 3.11 or newer.")
    if args.platform:
        system, separator, machine = args.platform.partition("-")
        if not separator:
            parser.error("--platform must have the form darwin-arm64 or linux-x86_64")
        args.platform = platform_key(system, machine)
    else:
        args.platform = platform_key()
    return args


def install_binary(binary_dir, name, pin):
    """Verify both hashes before atomically replacing a release executable."""
    binary = binary_dir / name
    if binary.is_file() and not binary.is_symlink() and hashlib.sha256(binary.read_bytes()).hexdigest() == pin["binary_sha256"]:
        binary.chmod(0o755)
        return
    with urllib.request.urlopen(pin["binary_url"], timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != pin["archive_sha256"]:
        raise SystemExit(f"{name} archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = [m for m in archive.getmembers() if Path(m.name).name == name and m.isfile()]
        if len(members) != 1:
            raise SystemExit(f"{name} archive must contain exactly one executable")
        # Read only the selected file; never extract archive paths or links.
        with archive.extractfile(members[0]) as stream:
            contents = stream.read()
    if hashlib.sha256(contents).hexdigest() != pin["binary_sha256"]:
        raise SystemExit(f"{name} binary checksum mismatch")
    with tempfile.NamedTemporaryFile(dir=binary_dir, prefix=f".{name}-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            temporary.chmod(0o755)
            temporary.replace(binary)
        finally:
            temporary.unlink(missing_ok=True)


def needs_macos_build(target, pin, version=None):
    """Upstream Beads binaries may target a newer macOS than the host."""
    if not target.startswith("darwin-") or "minimum_macos" not in pin:
        return False
    version = version or platform.mac_ver()[0]
    if not version:
        raise SystemExit("Cannot determine macOS version for this release.")
    def major_minor(value):
        parts = [int(part) for part in value.split(".")[:2]]
        return tuple((parts + [0, 0])[:2])
    return major_minor(version) < major_minor(pin["minimum_macos"])


def build_beads(binary_dir, source, pin, target):
    """Build the pinned embedded-Dolt CLI on macOS older than its release binary."""
    version = platform.mac_ver()[0]
    identity = {"commit": pin["commit"], "platform": target, "macos": version}
    binary, receipt = binary_dir / "bd", binary_dir / "bd.build.json"
    if binary.is_file() and not binary.is_symlink() and receipt.is_file():
        try:
            previous = json.loads(receipt.read_text())
            if all(previous.get(k) == v for k, v in identity.items()) and previous.get("sha256") == hashlib.sha256(binary.read_bytes()).hexdigest():
                binary.chmod(0o755)
                return
        except (ValueError, OSError):
            pass
    print(f"Building pinned Beads for macOS {version}; the upstream binary requires macOS 26. This first build can take several minutes.", flush=True)
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise SystemExit(f"Cannot build pinned Beads from modified sources: {source}")
    # Git on a development Mac usually means CLT is already installed, but
    # check the compiler before downloading a Go toolchain.
    try:
        subprocess.run(["xcrun", "--find", "clang"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Beads on this macOS needs Apple Command Line Tools. Run xcode-select --install, then rerun setup.sh.") from None
    go = shutil.which("go")
    if not go:
        brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(p).is_file()), None)
        if not brew:
            raise SystemExit("Beads on macOS below 26 needs Go 1.26.2+ (or Homebrew to install Go). Install Go and rerun setup.sh.")
        subprocess.run([brew, "install", "go"], check=True)
        prefix = subprocess.check_output([brew, "--prefix", "go"], text=True).strip()
        go = str(Path(prefix) / "bin/go")
    arch = "arm64" if target == "darwin-arm64" else "amd64"
    cc_arch = "arm64" if arch == "arm64" else "x86_64"
    environment = dict(os.environ, CGO_ENABLED="1", GOOS="darwin", GOARCH=arch, GOTOOLCHAIN="auto",
                       CC=f"clang -arch {cc_arch}", CXX=f"clang++ -arch {cc_arch}",
                       MACOSX_DEPLOYMENT_TARGET=".".join(version.split(".")[:2]))
    with tempfile.TemporaryDirectory(dir=binary_dir, prefix=".bd-build-") as directory:
        built = Path(directory) / "bd"
        flags = f"-s -w -X main.Version={pin['tag'].removeprefix('v')} -X main.Commit={pin['commit']} -X main.Build={pin['commit'][:7]}"
        subprocess.run([go, "build", "-mod=readonly", "-tags", "gms_pure_go netgo", "-ldflags", flags,
                        "-o", str(built), "./cmd/bd"], cwd=source, env=environment, check=True)
        subprocess.run(["codesign", "-s", "-", "-f", str(built)], check=True)
        subprocess.run([str(built), "--version"], check=True, timeout=30)
        identity["sha256"] = hashlib.sha256(built.read_bytes()).hexdigest()
        built.replace(binary)
    receipt.write_text(json.dumps(identity, indent=2) + "\n")


def write_launcher(binary_dir, name, distribution):
    # pip's script directory can differ from the interpreter's directory (e.g.
    # /usr/bin/python3 with scripts under /usr/local/bin or ~/.local/bin).
    # Bind the suite launchers to this interpreter instead of guessing a path.
    code = ("import sys; from importlib.metadata import distribution; "
            f"sys.argv[0] = {name!r}; "
            f"sys.exit(next(e for e in distribution({distribution!r}).entry_points "
            f"if e.group == 'console_scripts' and e.name == {name!r}).load()())")
    script = "#!/bin/sh\n# Generated by TinkerTom's installer.\nexec " + shlex.join([sys.executable, "-c", code]) + ' "$@"\n'
    target = binary_dir / name
    if target.is_symlink():
        target.unlink()  # Never write through an old launcher symlink.
    target.write_text(script)
    target.chmod(0o755)


def install_python_packages(pins, system):
    command = [sys.executable, "-m", "pip", "install"]
    if system:
        command.append("--break-system-packages")
    command += ["--only-binary=headroom-ai", "-e", str(ROOT), pins["headroom"]["distribution"],
                pins["chat"]["distribution"], pins["native_transport"]["distribution"]]
    subprocess.run(command, check=True)


def configure_shell(shell):
    if shell == "none":
        return
    profile = Path.home() / (".zshrc" if shell == "zsh" else ".bashrc")
    start, end = "# >>> TinkerTom setup >>>", "# <<< TinkerTom setup <<<"
    block = start + "\nsource " + shlex.quote(str(ROOT / "shell" / ("tinkertom." + shell))) + "\n" + end
    contents = profile.read_text() if profile.exists() else ""
    pattern = re.compile(r"(?ms)^" + re.escape(start) + r"\n.*?^" + re.escape(end) + r"$")
    if start in contents or end in contents:
        if contents.count(start) != 1 or contents.count(end) != 1 or not pattern.search(contents):
            raise SystemExit(f"Incomplete/duplicate TinkerTom setup markers in {profile}; file left unchanged.")
        updated = pattern.sub(lambda _: block, contents, count=1)
    else:
        updated = contents + ("\n" if contents and not contents.endswith("\n") else "") + "\n" + block + "\n"
    if updated != contents:
        profile.write_text(updated)
    print("Shell integration configured in " + str(profile))


def main(argv=None):
    args = installation_mode(argv)
    pins = json.loads((ROOT / "tools.lock.json").read_text())
    releases = {}
    for tool in ("rtk", "beads"):
        try:
            releases[tool] = pins[tool]["binaries"][args.platform]
        except KeyError:
            raise SystemExit(f"No pinned {tool} release for {args.platform}; update this checkout before installing.") from None
    print(f"Installing suite for {args.platform}", flush=True)
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
    install_binary(binary_dir, "rtk", releases["rtk"])
    if needs_macos_build(args.platform, releases["beads"]):
        build_beads(binary_dir, ROOT / ".tools/src/beads", pins["beads"], args.platform)
    else:
        install_binary(binary_dir, "bd", releases["beads"])
    install_python_packages(pins, args.system)
    for name, distribution in (("headroom", "headroom-ai"), ("tinkertom", "tinkertom")):
        write_launcher(binary_dir, name, distribution)
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
    configure_shell(args.shell)
    print("Suite installed with " + sys.executable)
    print("Run " + shlex.join([str(binary_dir / "tinkertom"), "doctor"]))
    if args.shell == "none":
        print("For zsh, add this line to ~/.zshrc: source " + shlex.quote(str(ROOT / "shell/tinkertom.zsh")))


if __name__ == "__main__":
    main()
