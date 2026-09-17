# Source this file from ~/.zshrc. The target workspace is always the caller's cwd.
typeset -g TINKERTOM_ROOT="${${(%):-%x}:A:h:h}"
typeset -U path
path=("$TINKERTOM_ROOT/.tools/bin" $path)

function TinkerTom() {
    local tinkertom_bin="$TINKERTOM_ROOT/.tools/bin/tinkertom"

    if [[ ! -x "$tinkertom_bin" ]]; then
        print -u2 -- "TinkerTom is not installed at $tinkertom_bin. See $TINKERTOM_ROOT/README.md."
        return 127
    fi

    command "$tinkertom_bin" -C "$PWD" "$@"
}

alias tinkertom-claude='tinkertom claude'
alias tinkertom-codex='tinkertom codex'
