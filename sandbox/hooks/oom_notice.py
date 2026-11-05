#!/usr/bin/env python3
"""PostToolUse hook (Bash) inside a ContainedClaude session: when a process was killed by the session's memory
limit since the last command, say so (exit 2: the message is shown to the agent). A plain "Killed" / exit 137
is otherwise easy to misread.

The limit is a systemd scope (cgroup) around the sandbox, named in CONTAINEDCLAUDE_SCOPE; its memory.events
is readable through /sys/fs/cgroup.
"""
import glob
import os
import sys

SEEN = "/tmp/.containedclaude-oom-seen"       # /tmp is private to the session


def scope_dir(unit):
    for depth in range(2, 8):
        hits = glob.glob("/sys/fs/cgroup/" + "*/" * depth + unit + ".scope")
        if hits:
            return hits[0]
    return None


def main():
    unit = os.environ.get("CONTAINEDCLAUDE_SCOPE")
    d = scope_dir(unit) if unit else None
    if not d:
        return 0
    try:
        with open(os.path.join(d, "memory.events")) as fh:
            kills = dict(l.split() for l in fh if l.strip()).get("oom_kill", "0")
        kills = int(kills)
    except (OSError, ValueError):
        return 0
    try:
        with open(SEEN) as fh:
            seen = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0
    if kills <= seen:
        return 0
    try:
        with open(SEEN, "w") as fh:
            fh.write("%d\n" % kills)
    except OSError:
        pass
    try:
        st = os.statvfs("/tmp")
        tmp_used = (st.f_blocks - st.f_bfree) * st.f_frsize / 2.0 ** 30
    except OSError:
        tmp_used = 0.0
    limit = os.environ.get("CONTAINEDCLAUDE_SESSION_MEM", "the configured limit")
    sys.stderr.write(
        "containedclaude: %d process(es) killed by the session memory limit (%s for all processes of this session "
        "together, plus files in /tmp: %.1f GB used). Run memory-heavy work as a job (cc-run --mem ...), or "
        "remove large files from /tmp; the user can raise session_mem in .contained/config.ini.\n"
        % (kills - seen, limit, tmp_used))
    return 2


if __name__ == "__main__":
    sys.exit(main())
