#!/bin/sh
# Install this checkout and enable its commands in the user's shell.
set -eu

tinkertom_setup_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
tinkertom_setup_python=
tinkertom_setup_system=false
tinkertom_setup_shell=auto

usage() {
    cat <<'USAGE'
Usage: ./setup.sh [--system] [--python PATH] [--no-shell]

Default: install using this checkout's .venv (created if missing).
--system      Use the selected non-venv Python and allow pip --break-system-packages.
--python PATH Select Python 3.11+ (default: detect a compatible installed Python).
--no-shell    Do not update ~/.zshrc or ~/.bashrc.

The setup detects zsh or bash from SHELL and adds a reusable shell integration.
macOS/Linux ARM64 and x86_64 are selected using uname.
macOS below 26 builds Beads from pinned source (Apple Command Line Tools and Go).
On macOS, missing Python can be installed through an existing Homebrew installation.
Provider CLIs and their logins must be installed separately.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --system) tinkertom_setup_system=true; shift ;;
        --python)
            if [ "$#" -lt 2 ]; then
                printf '%s\n' '--python requires an interpreter path' >&2
                exit 2
            fi
            tinkertom_setup_python=$2
            shift 2
            ;;
        --no-shell) tinkertom_setup_shell=none; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

tinkertom_setup_os=$(uname -s)
tinkertom_setup_arch=$(uname -m)
case "$tinkertom_setup_os" in
    Darwin) tinkertom_setup_os=darwin ;;
    Linux) tinkertom_setup_os=linux ;;
    *) printf 'Unsupported operating system: %s. Use macOS, Linux or WSL.\n' "$tinkertom_setup_os" >&2; exit 1 ;;
esac
case "$tinkertom_setup_arch" in
    arm64|aarch64) tinkertom_setup_arch=arm64 ;;
    x86_64|amd64) tinkertom_setup_arch=x86_64 ;;
    *) printf 'Unsupported architecture: %s. Use ARM64 or x86_64.\n' "$tinkertom_setup_arch" >&2; exit 1 ;;
esac
tinkertom_setup_platform="$tinkertom_setup_os-$tinkertom_setup_arch"
printf 'Detected platform: %s\n' "$tinkertom_setup_platform"

compatible_python() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -z "$tinkertom_setup_python" ]; then
    for tinkertom_setup_candidate in python3 python3.13 python3.12 python3.11; do
        if compatible_python "$tinkertom_setup_candidate"; then
            tinkertom_setup_python=$tinkertom_setup_candidate
            break
        fi
    done
    if [ -z "$tinkertom_setup_python" ] && [ "$tinkertom_setup_os" = darwin ]; then
        tinkertom_setup_brew=
        for tinkertom_setup_candidate in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
            if command -v "$tinkertom_setup_candidate" >/dev/null 2>&1; then
                tinkertom_setup_brew=$tinkertom_setup_candidate
                break
            fi
        done
        if [ -n "$tinkertom_setup_brew" ]; then
            tinkertom_setup_python="$("$tinkertom_setup_brew" --prefix python@3.13)/bin/python3.13"
            if ! compatible_python "$tinkertom_setup_python"; then
                printf '%s\n' 'Installing Python 3.13 through Homebrew...'
                "$tinkertom_setup_brew" install python@3.13
            fi
        fi
    fi
fi
if [ -z "$tinkertom_setup_python" ] || ! compatible_python "$tinkertom_setup_python"; then
    printf '%s\n' 'Python 3.11+ is required. Install it or use --python PATH. On macOS: brew install python@3.13 (requires Homebrew).' >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    printf '%s\n' 'Git is required. On macOS, install Command Line Tools with: xcode-select --install' >&2
    exit 1
fi

if [ "$tinkertom_setup_shell" = auto ]; then
    case "${SHELL:-}" in
        */zsh|zsh) tinkertom_setup_shell=zsh ;;
        */bash|bash) tinkertom_setup_shell=bash ;;
        *)
            tinkertom_setup_shell=none
            printf '%s\n' 'Shell not recognized as zsh/bash; leaving shell startup files unchanged.'
            ;;
    esac
fi

if [ "$tinkertom_setup_system" = true ]; then
    "$tinkertom_setup_python" "$tinkertom_setup_root/scripts/install-tools.py" --platform "$tinkertom_setup_platform" --system --shell "$tinkertom_setup_shell"
else
    if [ ! -x "$tinkertom_setup_root/.venv/bin/python" ]; then
        "$tinkertom_setup_python" -m venv "$tinkertom_setup_root/.venv"
    fi
    "$tinkertom_setup_root/.venv/bin/python" "$tinkertom_setup_root/scripts/install-tools.py" --platform "$tinkertom_setup_platform" --shell "$tinkertom_setup_shell"
fi

if [ "$tinkertom_setup_shell" != none ]; then
    printf 'Setup complete. Open a new terminal, or run: source ~/.%src\n' "$tinkertom_setup_shell"
    printf '%s\n' 'Then use tinkertom-codex or tinkertom-claude from your project directory.'
fi
