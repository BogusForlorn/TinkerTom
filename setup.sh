#!/bin/sh
# Install this checkout and enable its commands in the user's shell.
set -eu

tinkertom_setup_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
tinkertom_setup_python=python3
tinkertom_setup_system=false
tinkertom_setup_shell=auto

usage() {
    cat <<'USAGE'
Usage: ./setup.sh [--system] [--python PATH] [--no-shell]

Default: install using this checkout's .venv (created if missing).
--system      Use the selected non-venv Python and allow pip --break-system-packages.
--python PATH Select Python 3.11+ (default: python3).
--no-shell    Do not update ~/.zshrc or ~/.bashrc.

The setup detects zsh or bash from SHELL and adds a reusable shell integration.
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

if ! command -v "$tinkertom_setup_python" >/dev/null 2>&1; then
    printf 'Python not found: %s. Install Python 3.11+ or use --python PATH.\n' "$tinkertom_setup_python" >&2
    exit 1
fi
"$tinkertom_setup_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "TinkerTom requires Python 3.11+")'

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
    "$tinkertom_setup_python" "$tinkertom_setup_root/scripts/install-tools.py" --system --shell "$tinkertom_setup_shell"
else
    if [ ! -x "$tinkertom_setup_root/.venv/bin/python" ]; then
        "$tinkertom_setup_python" -m venv "$tinkertom_setup_root/.venv"
    fi
    "$tinkertom_setup_root/.venv/bin/python" "$tinkertom_setup_root/scripts/install-tools.py" --shell "$tinkertom_setup_shell"
fi

if [ "$tinkertom_setup_shell" != none ]; then
    printf 'Setup complete. Open a new terminal, or run: source ~/.%src\n' "$tinkertom_setup_shell"
    printf '%s\n' 'Then use tinkertom-codex or tinkertom-claude from your project directory.'
fi
