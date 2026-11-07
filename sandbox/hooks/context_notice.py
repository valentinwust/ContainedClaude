#!/usr/bin/env python3
"""PostToolUse hook (every tool) inside a ContainedClaude session: when the agent that called the tool (the main
session or a subagent) is within MARGIN tokens of auto-compaction, tell it once (additionalContext), so it can
write down what a summary would lose. Silent again until its usage drops below that, e.g. after a compaction.

Context used = the newest assistant record's usage in the agent's own transcript (input + cache creation + cache
read tokens, what Claude Code counts) plus a rough estimate of this tool's result, which Claude Code counts before
its next request. Compaction starts BUFFER tokens below the window (/context: "Autocompact buffer 33k"). The
window, first match wins: CLAUDE_CODE_AUTO_COMPACT_WINDOW, the
subagent definition's autoCompactWindow, modelSettings.<model>.autoCompactWindow in the settings files (local >
project > user), else 200k (Claude Code's own fallback), or 1M once usage is past 200k.
"""
import glob
import json
import os
import sys

BUFFER = 33000                                 # Claude Code compacts this far below the window
MARGIN = 25000                                 # warn this far before that: room to write notes
DEFAULT_WINDOW, LONG_WINDOW = 200000, 1000000
TAIL = 65536
STATE = "/tmp/.containedclaude-context"       # /tmp is private to the session


def newest_usage(path):
    """(tokens, model) of the newest assistant record with usage, reading the file backwards in chunks."""
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    with fh:
        end = fh.seek(0, 2)
        rest = b""
        while end > 0:
            start = max(0, end - TAIL)
            fh.seek(start)
            lines = (fh.read(end - start) + rest).split(b"\n")
            rest = lines.pop(0) if start else b""          # maybe cut; completed by the next chunk
            for line in reversed(lines):
                if b'"usage"' not in line or b'"assistant"' not in line:
                    continue
                try:
                    msg = json.loads(line).get("message") or {}
                    u = msg["usage"]
                    tokens = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                              + u.get("cache_read_input_tokens", 0))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if tokens > 0:                               # synthetic messages carry zero usage
                    return tokens, msg.get("model") or ""
            end = start
    return None


def load_json(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def positive_int(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def agent_window(agent_type, dirs):
    """autoCompactWindow from the frontmatter of the agent definition named agent_type."""
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.md"))):
            try:
                with open(f) as fh:
                    head = fh.read(8192).split("\n")
            except OSError:
                continue
            if not head or head[0].strip() != "---":
                continue
            fields = {}
            for line in head[1:]:
                if line.strip() == "---":
                    break
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip().strip("'\"")
            if fields.get("name") == agent_type:
                return positive_int(fields.get("autoCompactWindow"))
    return None


def window(model, tokens, agent_type, project):
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    w = positive_int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if w is None and agent_type:
        w = agent_window(agent_type, [os.path.join(project, ".claude", "agents"), os.path.join(config, "agents")])
    if w is None:
        for f in (os.path.join(project, ".claude", "settings.local.json"),
                  os.path.join(project, ".claude", "settings.json"), os.path.join(config, "settings.json")):
            ms = load_json(f).get("modelSettings")
            entry = ms.get(model) if isinstance(ms, dict) else None
            w = positive_int(entry.get("autoCompactWindow")) if isinstance(entry, dict) else None
            if w is not None:
                break
    if w is None:
        w = LONG_WINDOW if tokens > DEFAULT_WINDOW else DEFAULT_WINDOW
    return w


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    transcript, session = data.get("transcript_path") or "", data.get("session_id") or ""
    agent = data.get("agent_id") or ""
    if not transcript or not session:
        return 0
    if agent:          # <project dir>/<session>/subagents/agent-<id>.jsonl next to <project dir>/<session>.jsonl
        transcript = os.path.join(os.path.dirname(transcript), session, "subagents", "agent-%s.jsonl" % agent)
    found = newest_usage(transcript)
    if not found:
        return 0
    tokens, model = found
    tokens += len(json.dumps(data.get("tool_response"))) // 3
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    w = window(model, tokens, data.get("agent_type") if agent else None, project)
    flag = os.path.join(STATE, "".join(c if c.isalnum() or c in "-_" else "_" for c in session + "." + (agent or "main")))
    trigger = w - BUFFER
    if tokens < trigger - MARGIN:
        if os.path.exists(flag):
            try:
                os.unlink(flag)
            except OSError:
                pass
        return 0
    if os.path.exists(flag):
        return 0
    try:
        os.makedirs(STATE, exist_ok=True)
        open(flag, "w").close()
    except OSError:
        pass
    msg = ("containedclaude: your context holds about %dk tokens and auto-compaction starts at about %dk, so it "
           "will summarize this conversation soon and details that live only in the conversation may be lost. "
           "Consider writing down what matters (state, decisions, paths, open work) where you or the user will find "
           "it; then continue as you judge best." % (tokens // 1000, max(trigger, 0) // 1000))
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
