#!/usr/bin/env python3
"""PreToolUse hook for Write/Edit/MultiEdit/NotebookEdit inside a ContainedClaude session.

Not a security boundary (the sandbox makes these paths read-only anyway); it turns a confusing
"read-only file system" error into a clear explanation. It only looks at the target PATH, never at the
content, so it cannot block anything by accidental text matches.
"""
import json
import os
import sys


def under(p, roots):
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots if r)


def resolve(p):
    p = os.path.abspath(os.path.expanduser(p))
    head, tail = p, []
    while not os.path.exists(head) and head != "/":           # new file: resolve the existing parent
        head, t = os.path.split(head)
        tail.insert(0, t)
    return os.path.join(os.path.realpath(head), *tail)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    inp = data.get("tool_input") or {}
    path = inp.get("file_path") or inp.get("notebook_path")
    if not path:
        return 0
    p = resolve(path)
    env = os.environ
    writable = env.get("CONTAINEDCLAUDE_WRITABLE", "").split(os.pathsep) + ["/tmp"]
    cfgdir = env.get("CLAUDE_CONFIG_DIR")
    protected = env.get("CONTAINEDCLAUDE_PROTECTED", "").split(os.pathsep)
    # set by the launcher from policy.config_ro (empty in `containedclaude admin`)
    ro_in_cfg = [x for x in env.get("CONTAINEDCLAUDE_CONFIG_RO", "").split(os.pathsep) if x]
    rw_in_cfg = [os.path.join(cfgdir, "plugins", "data")] if cfgdir else []
    if under(p, protected) or (under(p, ro_in_cfg) and not under(p, rw_in_cfg)):
        msg = ("%s is read-only in this sandbox (protected: it could run code outside the sandbox later, or it "
               "belongs to the sandbox setup). Ask the user if it really needs to change; they edit it from "
               "outside. See CLAUDE_environment.md." % p)
    elif under(p, writable + ([cfgdir, env.get("CODEX_HOME")] if cfgdir else [])):
        return 0
    else:
        project = env.get("CONTAINEDCLAUDE_PROJECT", "PROJECT")
        msg = ("%s is outside the writable area of this sandbox (%s). Write inside the project instead, or ask "
               "the user to run `containedclaude add-write %s FOLDER` outside the sandbox (writable from the "
               "next session on; same rules as the project)." % (p, ", ".join(w for w in writable if w), project))
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
