#!/usr/bin/env bash
# Undo install.sh: remove the `containedclaude` link from ~/.local/bin (only if it points into this clone) and
# the shell guard line from ~/.bashrc / ~/.zshrc (install.sh --shell-guard).
#   ./uninstall.sh            the link and that line only; says what else exists and how to remove it
#   ./uninstall.sh --state    also the host-side state in ~/.local/share/containedclaude: the contained Claude
#                             config (login, plugins, settings), the Codex login, the project
#                             registry and the update caches. Other files there (your own notes) stay.
# Never touched: this clone (delete it yourself afterwards) and the projects. A project keeps .contained/,
# .contained_claude.sh, CLAUDE_environment.md and the @CLAUDE_environment.md line in CLAUDE.md; delete those
# yourself if you want (the folder works as a normal folder either way).
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
state="${XDG_DATA_HOME:-$HOME/.local/share}/containedclaude"
bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
known=(claude-config codex-home admin write_paths.txt update-check.json tool-identity.json)   # what we create there
with_state=0
for a in "$@"; do
  case "$a" in
    --state) with_state=1;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "usage: $0 [--state]" >&2; exit 2;;
  esac
done

# 1. the link
link="$bin/containedclaude"
if [[ -L "$link" ]]; then
  if [[ "$(readlink -f "$link" 2>/dev/null || true)" == "$(readlink -f "$here/bin/containedclaude")" ]]; then
    rm -f "$link"; echo "removed $link"
  else
    echo "kept $link: it points to $(readlink "$link"), not into this clone"
  fi
elif [[ -e "$link" ]]; then
  echo "kept $link: not a symlink, so not made by install.sh"
else
  echo "no $link (nothing to remove)"
fi

# 1b. the shell guard line (install.sh --shell-guard), from either shell's startup file
mark="# containedclaude shell guard"
for rc in "$HOME/.bashrc" "${ZDOTDIR:-$HOME}/.zshrc"; do
  [[ -f "$rc" ]] && grep -qF -- "$mark" "$rc" || continue
  rest="$( { grep -vF -- "$mark" "$rc" || true; } )"
  printf '%s\n' "$rest" > "$rc"; echo "removed the shell guard line from $rc"
done

# 2. projects: listed for information only (from the registry, before it is removed)
projects=()
if [[ -f "$state/write_paths.txt" ]]; then
  while IFS= read -r p; do
    [[ -f "$p/.contained/config.ini" ]] && projects+=("$p")
  done < "$state/write_paths.txt"
fi

# 3. the state
if [[ -d "$state" ]]; then
  if (( with_state )); then
    running="$(pgrep -u "$(id -u)" -f 'python3 -I ([^ ]*/)?bin/containedclaude( |$)' || true)"
    if [[ -n "$running" ]]; then
      echo "containedclaude sessions are still running (pids: $(echo "$running" | tr '\n' ' ')); stop them first" >&2
      exit 1
    fi
    for f in "${known[@]}"; do
      [[ -e "$state/$f" ]] || continue
      rm -rf -- "${state:?}/$f"; echo "removed $state/$f"
    done
    if rmdir "$state" 2>/dev/null; then
      echo "removed $state"
    else
      echo "kept $state: it still holds files ContainedClaude did not create:"
      ls -A "$state" | sed 's/^/  /'
    fi
  else
    echo "kept $state (the contained Claude config with login, plugins and settings; the Codex login; the project" \
         "registry): \`$0 --state\` removes it"
  fi
fi

# 4. what stays
echo "kept this clone, $here: delete it yourself (rm -rf) once nothing else uses it"
if (( ${#projects[@]} )); then
  echo "kept the projects (each has .contained/, .contained_claude.sh, CLAUDE_environment.md and the" \
       "@CLAUDE_environment.md line in CLAUDE.md; delete those yourself if you like):"
  printf '  %s\n' "${projects[@]}"
fi
