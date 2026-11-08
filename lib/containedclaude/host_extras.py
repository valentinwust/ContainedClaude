#!/usr/bin/python3 -I
"""Quality-of-life features for Claude Code OUTSIDE the sandbox (install.sh --extras, uninstall.sh), in the
user's own settings.json ($CLAUDE_CONFIG_DIR or ~/.claude), run from this clone:
- the notice before auto-compaction (hook sandbox/hooks/context_notice.py),
- compaction before an idle session's prompt cache expires (mod sandbox/mods/idle-compact, through
  env.CLAUDE_CODE_PLUGIN_DIRS),
- the [claude] values of this clone's site.ini for both (env CONTAINEDCLAUDE_*), written at install time.
Ours is recognised by the hook script's and the mod's path ending, so `add` adds it once and replaces what an
earlier run or another clone path wrote; `remove` takes it out again and leaves everything else as it was.

  host_extras.py add|remove|status      status: installed (0), not installed (1), outdated (2)
"""
import json
import os
import sys

CLONE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(CLONE, "lib"))
from containedclaude.config import site_config      # noqa: E402

HOOKS = [("PostToolUse", "*", "context_notice.py")]          # (event, matcher, script in sandbox/hooks)
MOD_TAIL = os.sep + os.path.join("sandbox", "mods", "idle-compact")
MOD = CLONE + MOD_TAIL
VALUES = {"CONTAINEDCLAUDE_COMPACT_NOTICE": "compact_notice",          # env name: [claude] key
          "CONTAINEDCLAUDE_IDLE_COMPACT_AFTER": "idle_compact_after",
          "CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS": "idle_compact_min_tokens"}


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
    for key in ("hooks", "env"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise ValueError('"%s" is not an object' % key)
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


def applied(data, add):
    """data (not changed) with our entries taken out and, with add, put in as this clone and site.ini have them."""
    data = json.loads(json.dumps(data))
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
    env = data.setdefault("env", {})
    dirs = [d for d in str(env.get("CLAUDE_CODE_PLUGIN_DIRS") or "").split(os.pathsep)
            if d and not d.rstrip(os.sep).endswith(MOD_TAIL)]
    for name in VALUES:
        env.pop(name, None)
    if add:
        site = site_config()
        env["CONTAINEDCLAUDE_COMPACT_NOTICE"] = site.get("claude", "compact_notice", fallback="").strip() or "0"
        if site.get("claude", "idle_compact", fallback="").strip().lower() in ("1", "yes", "true", "on"):
            dirs.append(MOD)
            for name in ("CONTAINEDCLAUDE_IDLE_COMPACT_AFTER", "CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS"):
                env[name] = site.get("claude", VALUES[name], fallback="").strip()
    if dirs:
        env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(dirs)
    else:
        env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
    if not env:
        data.pop("env")
    return data


def change(add):
    path = settings_path()
    try:
        data = load(path)
    except (OSError, ValueError) as e:
        print("cannot use %s (%s); fix it first" % (path, e), file=sys.stderr)
        return 1
    new = applied(data, add)
    if new == data:
        print("%s: the extras are %s" % (path, "already there" if add else "not there"))
        return 0
    save(path, new)
    print("%s the extras %s %s (notice before auto-compaction, compaction before an idle session's cache "
          "expires; new sessions)" % ("added" if add else "removed", "to" if add else "from", path))
    return 0


def status():
    try:
        data = load(settings_path())
    except (OSError, ValueError):
        print("unreadable")
        return 1
    if applied(data, False) == data:
        print("not installed")
        return 1
    if applied(data, True) == data:
        print("installed")
        return 0
    print("outdated (another clone path or other site.ini values): run install.sh --extras again")
    return 2


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("add", "remove", "status"):
        sys.exit("usage: host_extras.py add|remove|status")
    sys.exit(status() if cmd == "status" else change(cmd == "add"))
