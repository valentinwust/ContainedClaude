#!/usr/bin/env bash
# Install the `containedclaude` command (a symlink into ~/.local/bin) and create site.ini if missing.
#   ./install.sh [--shell-guard] [--extras] [SITE_TEMPLATE]   default template: site.curry.ini (site.example.ini elsewhere)
#   --shell-guard: one line in ~/.bashrc (or ~/.zshrc) that loads lib/shell-guard.sh: `claude` / `codex` in a
#                  ContainedClaude project ask whether to start contained; adds `cclaude` / `ccodex`
#   --extras:      quality-of-life features for Claude Code outside the sandbox too, in your own settings.json:
#                  the notice before auto-compaction and compaction before an idle session's prompt cache
#                  expires, with the [claude] values of site.ini; added once, run from this clone (again after
#                  changing those values)
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
guard=0 extras=0 template=site.curry.ini
for a in "$@"; do
  case "$a" in
    --shell-guard) guard=1;;
    --extras) extras=1;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) echo "usage: $0 [--shell-guard] [--extras] [SITE_TEMPLATE]" >&2; exit 2;;
    *) template="$a";;
  esac
done
case "$(basename "${SHELL:-}")" in
  bash) rc="$HOME/.bashrc";;
  zsh) rc="${ZDOTDIR:-$HOME}/.zshrc";;
  *) rc="";;
esac
if (( guard )) && [[ -z "$rc" ]]; then
  echo "--shell-guard: only bash and zsh are supported (your shell: ${SHELL:-unknown})" >&2; exit 1
fi
[[ "$template" = /* ]] || template="$here/$template"
bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$bin"
ln -sfn "$here/bin/containedclaude" "$bin/containedclaude"
if [[ -f "$here/site.ini" ]]; then
  echo "keeping existing $here/site.ini"
else
  cp "$template" "$here/site.ini"; echo "created $here/site.ini from $(basename "$template")"
fi
echo "installed $bin/containedclaude -> $here/bin/containedclaude"
case ":$PATH:" in *":$bin:"*) ;; *) echo "note: $bin is not on your PATH";; esac
[[ -x /usr/bin/python3 ]] || echo "warning: /usr/bin/python3 not found (the launcher uses the system python)"
for t in bwrap unshare sbatch apptainer claude; do command -v "$t" >/dev/null || echo "warning: $t not found"; done
mark="# containedclaude shell guard"
if (( guard )); then
  line="if [ -f '$here/lib/shell-guard.sh' ]; then . '$here/lib/shell-guard.sh'; fi  $mark"
  if [[ -f "$rc" ]] && grep -qxF -- "$line" "$rc"; then
    echo "the shell guard is already in $rc"
  else
    # an older line (another clone path) is replaced; written in place, so a symlinked rc stays one
    rest="$( { grep -vF -- "$mark" "$rc" 2>/dev/null || true; } )"
    if [[ -n "$rest" ]]; then printf '%s\n' "$rest" "$line"; else printf '%s\n' "$line"; fi > "$rc"
    echo "added the shell guard to $rc (new shells; this one: . $rc)"
  fi
elif [[ -n "$rc" ]] && ! grep -qF -- "$mark" "$rc" 2>/dev/null; then
  echo "optional: $here/install.sh --shell-guard adds cclaude / ccodex and makes claude / codex ask first in a project"
fi
if (( extras )); then
  /usr/bin/python3 -I "$here/lib/containedclaude/host_extras.py" add
else
  st=0; /usr/bin/python3 -I "$here/lib/containedclaude/host_extras.py" status >/dev/null 2>&1 || st=$?
  if (( st == 2 )); then
    echo "note: the extras in your Claude settings are outdated (site.ini [claude] or the clone moved): $here/install.sh --extras updates them"
  elif (( st != 0 )); then
    echo "optional: $here/install.sh --extras adds the compaction notices (before auto-compaction, and before an idle session's cache expires) to Claude Code outside the sandbox too"
  fi
fi
