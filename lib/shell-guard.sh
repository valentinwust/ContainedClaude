# ContainedClaude shell guard (bash and zsh), sourced from ~/.bashrc or ~/.zshrc (`install.sh --shell-guard`).
# `claude` / `codex` typed in a ContainedClaude project outside the sandbox ask first: Enter starts them
# contained, u as usual. Everywhere else, inside sandboxes and for commands that start no agent, nothing
# changes; without a terminal they only warn. `command claude` skips the question; `cclaude` / `ccodex` always
# start contained. It covers this shell only (not IDEs or other programs): the notice in the project's
# CLAUDE_environment.md stays the safety net.

# subcommands and flags that start no agent here (from `claude --help`, `codex --help`)
_ccg_claude=" -v --version -h --help agents attach auth auto-mode doctor gateway import install logs mcp "
_ccg_claude="$_ccg_claude plugin plugins purge rm setup-token stop kill ultrareview update upgrade "
_ccg_codex=" -V --version -h --help help agents apply a archive cloud completion debug delete doctor "
_ccg_codex="$_ccg_codex features login logout mcp migrate-nfs-db migrate-rollouts plugin queue sandbox "
_ccg_codex="$_ccg_codex unarchive update "

_cc_guard_root() {      # the project containing the current folder (the launcher's rule: .contained/config.ini)
    local d
    d=$(pwd -P) || return 1
    while :; do
        if [ -f "$d/.contained/config.ini" ]; then printf '%s\n' "$d"; return 0; fi
        if [ -z "$d" ] || [ "$d" = / ]; then return 1; fi
        d=${d%/*}
        [ -n "$d" ] || d=/
    done
}

_cc_contained() {       # _cc_contained claude|codex ARGS...: start it in the sandbox of the current folder
    if ! command -v containedclaude >/dev/null 2>&1; then
        printf 'containedclaude not found on PATH (install.sh links it into ~/.local/bin)\n' >&2
        return 127
    fi
    local tool=$1
    shift
    if [ "$tool" = claude ]; then
        containedclaude run -- "$@"
    else
        containedclaude shell -- "$tool" "$@"
    fi
}

_cc_guard() {           # _cc_guard claude|codex ARGS...: `claude` / `codex`
    local tool=$1 root free ans
    shift
    case "$tool" in claude) free=$_ccg_claude;; *) free=$_ccg_codex;; esac
    if [ $# -gt 0 ]; then
        case "$free" in *" $1 "*) command "$tool" "$@"; return;; esac
    fi
    if [ "${CONTAINEDCLAUDE:-}" = 1 ] || ! root=$(_cc_guard_root); then
        command "$tool" "$@"
        return
    fi
    if [ -t 0 ] && [ -t 2 ]; then
        printf '%s is a ContainedClaude project. %s: [Enter] contained, [u] uncontained, [Ctrl-C] cancel ' \
            "$root" "$tool" >&2
        read -r ans || { printf '\n' >&2; return 1; }
        case "$ans" in
            u|U) ;;
            *) _cc_contained "$tool" "$@"; return;;
        esac
    else
        printf '%s: warning: running uncontained in the ContainedClaude project %s ' "$tool" "$root" >&2
        printf '(no terminal to ask; cc%s starts it contained)\n' "$tool" >&2
    fi
    command "$tool" "$@"
}

_cc_always() {          # `cclaude` / `ccodex`: contained (inside a sandbox it already is)
    if [ "${CONTAINEDCLAUDE:-}" = 1 ]; then command "$@"; else _cc_contained "$@"; fi
}

function claude { _cc_guard claude "$@"; }
function codex { _cc_guard codex "$@"; }
function cclaude { _cc_always claude "$@"; }
function ccodex { _cc_always codex "$@"; }

for _cc_t in claude codex; do
    alias "$_cc_t" >/dev/null 2>&1 &&
        printf 'containedclaude shell guard: the alias %s takes precedence over the guard\n' "$_cc_t" >&2
done
unset _cc_t
