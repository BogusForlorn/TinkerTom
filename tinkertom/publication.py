"""Good-faith boundary for agent-issued publication and Git mutations.

This is a best-effort command-position classifier, not a sandbox: it can be
bypassed (for example by scripts or other tools) and hook failures may fail open.
"""
from __future__ import annotations

import os
import re

REASON = ("Publication or Git mutation commands are not available to agents. "
          "Stop and give the user the exact terminal commands to run locally; "
          "do not seek a workaround or use another tool.")

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
# wrapper -> options that consume a following argument
_WRAPPERS = {"env": {"-u", "-C", "--unset", "--chdir"}, "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
             "doas": {"-u", "-C"}, "command": set(), "builtin": set(), "exec": {"-a"}, "nohup": set(),
             "nice": {"-n"}, "ionice": {"-c", "-n", "-p"}, "setsid": set(), "stdbuf": {"-i", "-o", "-e"},
             "timeout": {"-s", "-k", "--signal", "--kill-after"}, "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"}}
_GIT_GLOBAL_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                   "--config-env"}
_GIT_READONLY = {"status", "log", "diff", "show", "ls-files", "ls-tree", "ls-remote", "rev-parse", "rev-list",
                 "describe", "blame", "grep", "shortlog", "cat-file", "show-ref", "for-each-ref", "name-rev",
                 "merge-base", "diff-tree", "diff-index", "diff-files", "count-objects", "check-ignore",
                 "check-attr", "version", "help", "var", "verify-commit", "verify-tag", "show-branch", "cherry",
                 "range-diff", "whatchanged", "format-patch", "annotate", "get-tar-commit-id"}
_LIST_FLAGS = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}


def _basename(value: str) -> str:
    return os.path.basename(value.rstrip("/"))


def _parse(command: str):
    """Split into command segments (lists of words) plus nested substitution strings."""
    segments, nested, words, word = [], [], [], []
    heredocs, pending = [], []
    started = False
    i, n, quote = 0, len(command), None

    def flush_word():
        nonlocal word, started
        if started:
            words.append("".join(word))
        word, started = [], False

    def flush_segment():
        nonlocal words
        flush_word()
        if words:
            segments.append(words)
        words = []

    def substitution(start):
        """Handle $( or backtick at command[start]; return index after it."""
        if command[start] == "`":
            end = command.find("`", start + 1)
            end = n if end < 0 else end
            nested.append(command[start + 1:end])
            return min(end + 1, n)
        depth, j = 1, start + 2
        while j < n and depth:
            depth += {"(": 1, ")": -1}.get(command[j], 0)
            j += 1
        nested.append(command[start + 2:j - 1 if depth == 0 else j])
        return j

    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word.append(c)
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\" and i + 1 < n:
                i += 1
                word.append(command[i])
            elif c == "`" or command.startswith("$(", i):
                i = substitution(i)
                continue
            else:
                word.append(c)
        elif c in "'\"":
            quote, started = c, True
        elif c == "\\" and i + 1 < n:
            i += 1
            if command[i] != "\n":
                word.append(command[i])
                started = True
        elif c == "`" or command.startswith("$(", i):
            i = substitution(i)
            started = True
            continue
        elif c == "#" and not started:
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif c in " \t":
            flush_word()
        elif c == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            j = i + 2 + (command.startswith("-", i + 2))
            while j < n and command[j] in " \t":
                j += 1
            k = j
            while k < n and command[k] not in " \t\n;&|()<>":
                k += 1
            pending.append(command[j:k].strip("'\""))
            i = k
            continue
        elif c in ";\n|()":
            flush_segment()
            if c == "\n" and pending:
                lines, i = [], i + 1
                while i < n and pending:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line = command[i:end]
                    i = end + 1
                    if line.strip() == pending[0]:
                        pending.pop(0)
                        heredocs.append("\n".join(lines))
                        lines = []
                    else:
                        lines.append(line)
                if lines:
                    heredocs.append("\n".join(lines))
                continue
        elif c == "&":
            if (word and word[-1] in "<>") or command.startswith("&>", i):
                word.append(c)
                started = True
            else:
                flush_segment()
        else:
            word.append(c)
            started = True
        i += 1
    flush_segment()
    return segments, nested, heredocs


def _unwrap(words):
    """Return (words at the real command position, nested shell strings)."""
    extra = []
    i = 0
    while i < len(words):
        word = words[i]
        name = _basename(word)
        if _ASSIGN.match(word) or word in _KEYWORDS:
            i += 1
        elif name in _WRAPPERS:
            if name == "command" and any(w in {"-v", "-V"} for w in words[i + 1:i + 3]):
                return [], extra
            takes = _WRAPPERS[name]
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "--":
                option = words[i]
                if name == "env" and (option in {"-S", "--split-string"}
                                       or option.startswith("--split-string=")
                                       or option.startswith("-S") and len(option) > 2):
                    # env parses this argument as another command line.  It
                    # is commonly quoted as one shell word, so it must be
                    # recursively classified rather than treated as data.
                    if option.startswith("--split-string="):
                        extra.append(option.split("=", 1)[1])
                        i += 1
                    elif option.startswith("-S") and option != "-S":
                        extra.append(option[2:])
                        i += 1
                    else:
                        i += 2
                        if i - 1 < len(words):
                            extra.append(words[i - 1])
                    continue
                i += 2 if option in takes else 1
            if i < len(words) and words[i] == "--":
                i += 1
            if name == "timeout":
                i += 1  # duration
            while name in {"env", "sudo"} and i < len(words) and _ASSIGN.match(words[i]):
                i += 1
        else:
            return words[i:], extra
    return [], extra


def _is_flag_cluster_with(arg: str, letters: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and any(ch in letters for ch in arg[1:])


def _git_mutation(args) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        flag = args[i]
        if flag in _GIT_GLOBAL_ARG:
            i += 2
        else:
            i += 1
    if i >= len(args):
        return False
    sub, tail = args[i], args[i + 1:]
    positional = [x for x in tail if not x.startswith("-")]
    if sub in _GIT_READONLY:
        return False
    if sub == "branch":
        if any(x in {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy", "--force",
                     "--set-upstream-to", "--unset-upstream", "--edit-description", "--track", "--no-track"}
               or x.startswith(("--set-upstream-to=", "--track=")) or _is_flag_cluster_with(x, "dDmMcCfu")
               for x in tail):
            return True
        return bool(positional) and not any(x in _LIST_FLAGS for x in tail)
    if sub == "tag":
        if any(x in {"-d", "-a", "-s", "-f", "-m", "-u", "-F", "-e", "--delete", "--annotate", "--sign", "--force",
                     "--message", "--local-user", "--file", "--edit"} or x.startswith(("--message=", "--file=", "--local-user="))
               for x in tail):
            return True
        return bool(positional) and not any(x in _LIST_FLAGS for x in tail)
    if sub == "remote":
        return not (not tail or tail == ["-v"] or tail == ["--verbose"] or tail[0] in {"show", "get-url"} or
                    (tail[0] in {"-v", "--verbose"} and len(tail) > 1 and tail[1] in {"show", "get-url"}))
    if sub == "config":
        writes = {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--rename-section",
                  "--remove-section", "set", "unset", "edit", "rename-section", "remove-section"}
        if any(x in writes for x in tail):
            return True
        reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin",
                 "--show-scope", "--name-only"}
        if any(x in reads for x in tail) or (tail and tail[0] in {"get", "list", "show"}):
            return False
        return len(positional) != 1
    if sub == "stash":
        return not (tail and tail[0] in {"list", "show"})
    if sub == "reflog":
        return bool(tail) and tail[0] not in {"show"} and not tail[0].startswith("-")
    if sub == "worktree":
        return not (tail and tail[0] == "list")
    if sub == "notes":
        return bool(tail) and tail[0] not in {"list", "show"} and not tail[0].startswith("-")
    if sub == "submodule":
        return bool(tail) and tail[0] not in {"status", "summary"} and not tail[0].startswith("-")
    return True  # fetch/pull/clone/init, writers, aliases and unknown subcommands


def _flag_value(args, names):
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _tool_mutation(exe, args) -> bool:
    sub = args[0] if args else ""
    if exe == "git":
        return _git_mutation(args)
    if exe in {"gh", "glab"}:
        if len(args) >= 2 and args[0] in {"pr", "mr", "issue", "repo", "release", "workflow", "run", "secret", "variable"}:
            return args[1] in {"create", "merge", "close", "reopen", "edit", "comment", "review", "ready", "upload",
                               "delete", "fork", "rename", "archive", "set", "remove", "enable", "disable",
                               "rerun", "cancel", "transfer", "sync", "publish", "note"}
        if sub == "api":
            method = _flag_value(args, {"-X", "--method"})
            return bool(method and method.upper() != "GET") or any(a in {"-f", "-F", "--field", "--raw-field", "--input"} for a in args)
        return False
    if exe in {"npm", "pnpm", "yarn", "bun"}:
        if exe == "yarn" and sub == "npm" and len(args) > 1:
            sub = args[1]
        return sub in {"publish", "version", "deprecate", "unpublish", "dist-tag", "owner", "access", "token", "adduser", "login", "logout"}
    if exe == "cargo":
        return sub in {"publish", "yank", "owner", "login", "logout"}
    if exe in {"twine", "poetry", "uv", "flit"}:
        return sub in {"upload", "publish"}
    if exe.startswith("python"):
        return len(args) >= 3 and args[0] == "-m" and args[1] == "twine" and args[2] == "upload"
    if exe == "docker":
        return sub in {"push", "tag", "login", "logout"} or "--push" in args
    if exe == "helm":
        return sub in {"push", "install", "upgrade", "uninstall", "rollback"}
    if exe == "kubectl":
        return sub in {"apply", "create", "delete", "patch", "replace", "edit", "scale", "rollout", "set", "label",
                       "annotate", "taint"}
    return False


def classify_command(command: str, _depth: int = 0) -> str | None:
    """Return a denial reason, or ``None`` when a command is allowed."""
    if not isinstance(command, str):
        return None
    if _depth > 5:
        return REASON
    segments, nested, heredocs = _parse(command)
    stdin_shell = False
    for text in nested:
        if classify_command(text, _depth + 1):
            return REASON
    for segment in segments:
        words, extra = _unwrap(segment)
        for text in extra:
            if classify_command(text, _depth + 1):
                return REASON
        if not words:
            continue
        exe, args = _basename(words[0]), words[1:]
        if exe in _SHELLS or exe in {"source", "."}:
            stdin_shell = stdin_shell or not any(_is_flag_cluster_with(a, "c") for a in args)
            for j, arg in enumerate(args):
                if _is_flag_cluster_with(arg, "c"):
                    if j + 1 < len(args) and classify_command(args[j + 1], _depth + 1):
                        return REASON
                    break
        elif exe == "eval":
            if classify_command(" ".join(args), _depth + 1):
                return REASON
        elif _tool_mutation(exe, args):
            return REASON
    # Heredoc bodies are data unless a shell may execute them from stdin.
    if stdin_shell and any(classify_command(body, _depth + 1) for body in heredocs):
        return REASON
    return None


def publication_denial(command: str) -> str | None:
    return classify_command(command)
