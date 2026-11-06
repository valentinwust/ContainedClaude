#!/usr/bin/env python3
"""Hook for messages between Claude sessions in a ContainedClaude session (UserPromptSubmit, PreToolUse
SendMessage): contained sessions exchange messages only with the sessions of their own project, and with
their own agents and teammates.

- UserPromptSubmit: a message another session sent (`<cross-session-message from="...">`) is not delivered
  unless it comes from a session of this project.
- PreToolUse SendMessage: `main`, this session's agents and teammates and the sessions of this project are
  allowed; any other recipient (a session over Remote Control, in the cloud, of another project) is refused
  with an explanation.

The sessions of this project are the records in $CLAUDE_CONFIG_DIR/sessions (a folder per project) whose
socket answers. Only recipients and message headers are looked at, never the message text. Formats follow
Claude Code 2.1.319 (DEVELOPMENT.md, version dependencies); `containedclaude test` checks them.
"""
import glob
import hashlib
import json
import os
import re
import socket
import sys

RESERVED = {"main", "team-lead", "user", "system"}      # Claude Code's names for this session and its team
AGENT_TOOLS = {"Agent", "Task"}
PEER_PROMPT = re.compile(r"^\s*(?:Another Claude session sent a message:\s*)?<cross-session-message\b")
HEADER = re.compile(r"<cross-session-message\b([^>]*)>")
ATTR = lambda name: re.compile(r'\b%s="([^"]*)"' % name)
FROM, FROM_NAME = ATTR("from"), ATTR("from-name")
NAME_REF = re.compile(r"^(.*\S)\s*\[([0-9a-f]{6,12})\]$")      # "name [ref]", as ListAgents shows them
REF = re.compile(r"^[0-9a-f]{6,12}$")
LOCAL_ID = re.compile(r"^local_[0-9a-f-]{8,72}$")


def ref_of(kind, ident):
    """The ref ListAgents shows for an entry (sha256 of "kind:id"; 6 to 12 hex digits of it are used)."""
    return hashlib.sha256(("%s:%s" % (kind, ident)).encode()).hexdigest()[:12]


def alive(sock):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(sock)
        return True
    except OSError:
        return False
    finally:
        s.close()


def project_sessions(check_alive=True):
    """names, session ids and refs of this project's running sessions (this one included)"""
    names, sids, refs = set(), set(), set()
    for f in glob.glob(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "sessions", "*.json")):
        try:
            with open(f) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict):
            continue
        sock, sid = r.get("messagingSocketPath"), r.get("sessionId")
        if not isinstance(sock, str) or (check_alive and not alive(sock)):
            continue
        name = r.get("name") or os.path.basename(str(r.get("cwd") or "").rstrip("/"))
        if isinstance(name, str) and name:
            names.add(name)
        refs.add(ref_of("session", sock))
        if isinstance(sid, str) and sid:
            sids.add(sid)
            refs.add(ref_of("session", "sid:" + sid))
    return names, sids, refs


def is_address(a):
    return (a.startswith(("uds:", "bridge:", "did:", "sid:")) or bool(LOCAL_ID.match(a))
            or (a.startswith("/") and a.endswith(".sock")))


def local_address(a, sids):
    # a local socket: inside the sandbox, only the sockets of this project's sessions exist
    if a.startswith("uds:") or (a.startswith("/") and a.endswith(".sock")):
        return True
    return a.startswith("sid:") and a[4:] in sids


def own_agents(data):
    """names and ids of this session's agents (Agent tool calls in its transcript) and teammates"""
    names, ids = set(), set()

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "tool_use" and o.get("name") in AGENT_TOOLS:
                n = (o.get("input") or {}).get("name")
                if isinstance(n, str):
                    names.add(n)
            if isinstance(o.get("agentId"), str):
                ids.add(o["agentId"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    try:
        with open(data.get("transcript_path") or "", errors="replace") as fh:
            for line in fh:
                if '"agentId"' in line or '"Agent"' in line or '"Task"' in line:
                    try:
                        walk(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    for f in glob.glob(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "teams", "*", "config.json")):
        try:
            with open(f) as fh:
                members = json.load(fh).get("members") or []
        except (OSError, ValueError, AttributeError):
            continue
        for m in members:
            if isinstance(m, dict):
                names.update(v for v in (m.get("name"),) if isinstance(v, str))
                ids.update(v for v in (m.get("agentId"),) if isinstance(v, str))
    return names, ids


def inbound(data):
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not PEER_PROMPT.match(prompt):
        return None
    sids = project_sessions(check_alive=False)[1]
    for m in HEADER.finditer(prompt):
        f, n = FROM.search(m.group(1)), FROM_NAME.search(m.group(1))
        sender = f.group(1) if f else ""
        if not local_address(sender, sids):
            who = "%s (%s)" % (n.group(1), sender) if n else (sender or "an unknown sender")
            return {"decision": "block", "reason": (
                "ContainedClaude: a message from the session %s was not delivered: contained sessions only take "
                "messages from the sessions of their own project." % who)}
    return None


def outbound(data):
    inp = data.get("tool_input") or {}
    to = inp.get("to")
    if not isinstance(to, str):
        return None
    to = to.strip()
    kind = inp.get("recipient_kind")
    if kind in ("main", "agent") or to in RESERVED:
        return None
    if kind == "session" or is_address(to):
        if local_address(to, project_sessions(check_alive=False)[1]):
            return None
    else:
        names, sids, refs = project_sessions()
        m = NAME_REF.match(to)
        name, ref = (m.group(1), m.group(2)) if m else (to, to if REF.match(to) else None)
        if ref is None and name in names:
            return None
        anames, aids = own_agents(data)
        if ref is not None:
            arefs = {ref_of(k, i) for i in aids for k in ("subagent", "teammate")}
            if any(r.startswith(ref) for r in refs | arefs):
                return None
        elif name in anames or name in aids:
            return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": (
        "Not sent to %r: from a ContainedClaude session, messages go only to the other sessions of this "
        "project, this session's agents and teammates, and main (not to sessions over Remote Control, in the "
        "cloud or of other projects). For your own agents, use the id the Agent tool returned. To reach any "
        "other session, ask the user to pass the message on."
        % to)}}


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    event = data.get("hook_event_name")
    if event == "UserPromptSubmit":
        out = inbound(data)
    elif event == "PreToolUse" and data.get("tool_name") == "SendMessage":
        out = outbound(data)
    else:
        out = None
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
