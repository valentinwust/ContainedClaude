#!/usr/bin/python3 -I
"""Quality-of-life features for Claude Code OUTSIDE the sandbox (install.sh --extras, uninstall.sh): hooks in
the user's own settings.json ($CLAUDE_CONFIG_DIR or ~/.claude), run from this clone. For now only the notice
before auto-compaction (sandbox/hooks/context_notice.py). Each entry is recognised by the hook script's file
name, so `add` adds it once and replaces an entry from another clone path; `remove` takes it out again and
leaves everything else in the file as it was.

  host_extras.py add|remove|status
"""
import json
import os
import sys

CLONE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
HOOKS = [("PostToolUse", "*", "context_notice.py")]          # (event, matcher, script in sandbox/hooks)


def settings_path():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")


def command(script):
    return "/usr/bin/python3 -I '%s'" % os.path.join(CLONE, "sandbox", "hooks", script)


def ours(entry, script):
    return any(script in (h.get("command") or "") and "/sandbox/hooks/" in (h.get("command") or "")
               for h in entry.get("hooks") or [] if isinstance(h, dict))


def load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def save(path, data):
    real = os.path.realpath(path)                    # a symlinked settings.json stays a symlink
    os.makedirs(os.path.dirname(real), exist_ok=True)
    mode = os.stat(real).st_mode & 0o777 if os.path.exists(real) else 0o600
    tmp = real + ".containedclaude-tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def change(add):
    path = settings_path()
    try:
        data = load(path)
    except (OSError, ValueError) as e:
        print("cannot use %s (%s); fix it first" % (path, e), file=sys.stderr)
        return 1
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        print("cannot use %s: \"hooks\" is not an object" % path, file=sys.stderr)
        return 1
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    for event, matcher, script in HOOKS:
        entries = [e for e in hooks.get(event) or [] if not (isinstance(e, dict) and ours(e, script))]
        if add:
            entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": command(script)}]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks")
    if json.dumps(data, sort_keys=True) == before:
        print("%s: the extras are %s" % (path, "already there" if add else "not there"))
        return 0
    save(path, data)
    print("%s the extras %s %s (notice before auto-compaction; new sessions)" % (
        "added" if add else "removed", "to" if add else "from", path))
    return 0


def status():
    try:
        data = load(settings_path())
    except (OSError, ValueError):
        return 1
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    found = all(any(isinstance(e, dict) and ours(e, s) for e in hooks.get(ev) or []) for ev, _, s in HOOKS)
    print("installed" if found else "not installed")
    return 0 if found else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("add", "remove", "status"):
        sys.exit("usage: host_extras.py add|remove|status")
    sys.exit(status() if cmd == "status" else change(cmd == "add"))
