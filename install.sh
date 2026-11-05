#!/usr/bin/env bash
# Install the `containedclaude` command (a symlink into ~/.local/bin) and create site.ini if missing.
#   ./install.sh [SITE_TEMPLATE]     default: site.curry.ini (use site.example.ini for another cluster)
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
template="${1:-site.curry.ini}"
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
