#!/usr/bin/env python3
"""PostToolUse hook (every tool) inside a ContainedClaude session: when the agent that called the tool (the main
session or a subagent) is within MARGIN tokens of auto-compaction, tell it once (additionalContext), so it can
write down what a summary would lose. Silent again until its usage drops below that, e.g. after a compaction.

Context used = the newest assistant record's usage in the agent's own transcript (input + cache creation + cache
read tokens, what Claude Code counts) plus a rough estimate (characters / 3) of what came in after it and goes
into the next request: tool results of parallel calls and attachments already written, and this tool's result. Compaction starts BUFFER tokens below the window (/context: "Autocompact buffer 33k"). The
window, first match wins: CLAUDE_CODE_AUTO_COMPACT_WINDOW, the
subagent definition's autoCompactWindow, modelSettings.<model>.autoCompactWindow in the settings files (local >
project > user), else 200k (Claude Code's own fallback), or 1M once usage is past 200k.
"""
import glob
import json
import os
import stat
import sys

BUFFER = 33000                                 # Claude Code compacts this far below the window
MARGIN = 25000                                 # warn this far before that: room to write notes
DEFAULT_WINDOW, LONG_WINDOW = 200000, 1000000
TAIL = 65536
STATE = "/tmp/.containedclaude-context-%d" % os.getuid()     # private /tmp in the sandbox, shared outside


def state_dir():
    """STATE, made if missing and used only if it is our own real folder (outside the sandbox /tmp is shared
    with other users); else None (then the hook stays silent rather than repeat the notice)."""
    try:
        os.mkdir(STATE, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        st = os.lstat(STATE)
    except OSError:
        return None
    return STATE if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077 else None


def added_chars(rec):
    """Characters of a transcript record that the next request carries: user messages and tool results (not
    the toolUseResult copy) and attachments (hook output, files). Metadata records count nothing."""
    if rec.get("type") == "user":
        return len(json.dumps((rec.get("message") or {}).get("content") or ""))
    if rec.get("type") == "attachment":
        return len(json.dumps(rec.get("attachment") or ""))
    return 0


def tool_ids(rec):
    content = (rec.get("message") or {}).get("content") if rec.get("type") == "user" else None
    return {c.get("tool_use_id") for c in content if isinstance(c, dict)} if isinstance(content, list) else set()


def newest_usage(path):
    """(tokens, model, chars after, tool ids after): the newest assistant record with usage, and what the
    records written after it add (added_chars) and which tool results are among them. Reads the file backwards
    in chunks."""
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    after, ids = 0, set()
    with fh:
        end = fh.seek(0, 2)
        rest = b""
        while end > 0:
            start = max(0, end - TAIL)
            fh.seek(start)
            lines = (fh.read(end - start) + rest).split(b"\n")
            rest = lines.pop(0) if start else b""          # maybe cut; completed by the next chunk
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                u = msg.get("usage") if rec.get("type") == "assistant" else None
                if isinstance(u, dict):
                    try:
                        tokens = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                  + u.get("cache_read_input_tokens", 0))
                    except TypeError:
                        tokens = 0
                    if tokens > 0:                           # synthetic messages carry zero usage
                        return tokens, msg.get("model") or "", after, ids
                    continue
                after += added_chars(rec)
                ids |= tool_ids(rec)
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
    tokens, model, after, ids = found
    if not data.get("tool_use_id") or data.get("tool_use_id") not in ids:     # this result, unless written
        after += len(json.dumps(data.get("tool_response")))
    tokens += after // 3
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    w = window(model, tokens, data.get("agent_type") if agent else None, project)
    d = state_dir()
    if d is None:
        return 0
    flag = os.path.join(d, "".join(c if c.isalnum() or c in "-_" else "_" for c in session + "." + (agent or "main")))
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
        os.close(os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
    except OSError:
        return 0                                     # without the flag it would repeat at every call
    msg = ("containedclaude: your context holds about %dk tokens and auto-compaction starts at about %dk, so it "
           "will summarize this conversation soon and details that live only in the conversation may be lost. "
           "Consider writing down what matters (state, decisions, paths, open work) where you or the user will find "
           "it; then continue as you judge best." % (tokens // 1000, max(trigger, 0) // 1000))
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
