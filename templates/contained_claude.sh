#!/bin/bash -p
# Start Claude Code in the ContainedClaude sandbox for this folder. Works from any current directory.
# Created by `containedclaude init` (ContainedClaude {{version}}); read-only inside the sandbox.
# This runs outside the sandbox, so it starts no program found via PATH: fixed /bin/bash, -p (no BASH_ENV, CDPATH
# or functions from the environment), builtins, and /usr/bin/readlink only if this file is reached via a symlink.
src="${BASH_SOURCE[0]}"
if [[ -L "$src" && -x /usr/bin/readlink ]]; then src="$(/usr/bin/readlink -f -- "$src")"; fi
if [[ "$src" == */* ]]; then dir="${src%/*}"; else dir=.; fi
here="$(cd -P -- "${dir:-/}" && pwd -P)" || exit 1
launcher="{{clone}}/bin/containedclaude"
if [[ ! -x "$launcher" ]]; then
  echo "ContainedClaude not found at {{clone}}; re-run 'containedclaude init --force $here' from your clone" >&2
  exit 1
fi
exec "$launcher" run "$here" "$@"
