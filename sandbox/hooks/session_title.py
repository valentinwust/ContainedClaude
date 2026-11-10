#!/usr/bin/env python3
"""SessionStart hook inside a ContainedClaude session: when a session is resumed, give it back its title. A
session a Remote Control server starts again after a restart comes back under a generated name in the session
registry (what ListAgents shows), though claude.ai still shows its title and custom-title.json still holds it
(DEVELOPMENT.md §5.10).
"""
import json
import os
import sys


def main():
    e = json.load(sys.stdin)
    if e.get("source") != "resume":
        return 0
    folder = os.path.join(os.path.dirname(e["transcript_path"]), e["session_id"])
    with open(os.path.join(folder, "custom-title.json")) as fh:
        title = json.load(fh)["customTitle"]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "sessionTitle": title}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, KeyError, TypeError, ValueError):     # no title stored, or not in the form above: nothing to give back
        sys.exit(0)
