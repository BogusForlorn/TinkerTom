# Source this file from ~/.zshrc. The target workspace is always the caller's cwd.
typeset -U path
path=(/root/TinkerTom/.tools/bin $path)

function TinkerTom() {
    local tinkertom_bin='/root/TinkerTom/.venv/bin/tinkertom'

    if [[ ! -x "$tinkertom_bin" ]]; then
        print -u2 -- "TinkerTom is not installed at $tinkertom_bin. See /root/TinkerTom/README.md."
        return 127
    fi

    command "$tinkertom_bin" -C "$PWD" "$@"
}
