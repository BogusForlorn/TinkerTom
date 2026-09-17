# Source this file from ~/.bashrc. The target workspace is the caller's cwd.
TINKERTOM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PATH="$TINKERTOM_ROOT/.tools/bin:$PATH"

function TinkerTom() {
    local tinkertom_bin="$TINKERTOM_ROOT/.tools/bin/tinkertom"
    if [[ ! -x "$tinkertom_bin" ]]; then
        printf 'TinkerTom is not installed at %s. See %s/README.md.\n' "$tinkertom_bin" "$TINKERTOM_ROOT" >&2
        return 127
    fi
    command "$tinkertom_bin" -C "$PWD" "$@"
}

alias tinkertom-claude='tinkertom claude'
alias tinkertom-codex='tinkertom codex'
