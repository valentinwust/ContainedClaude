#!/usr/bin/env python3
"""PostToolUse hook (every tool) inside a ContainedClaude session: when the agent that called the tool (the main
session or a subagent) is within MARGIN tokens of auto-compaction, tell it once (additionalContext), so it can
write down what a summary would lose. Silent again until its usage drops below that, e.g. after a compaction.
Subagents are also told to go on with their task, read-only ones (read_only) to put their findings in their next
message instead of writing them down.

Context used = the newest assistant record's usage in the agent's own transcript (input + cache creation + cache
read tokens, what Claude Code counts) plus an estimate of what came in after it and goes into the next request:
tool results of parallel calls and attachments already written, and this tool's result. Text counts characters
/ 3. Images and PDFs arrive as base64 (an image of 2000 x 1000 pixels is ~600k characters but ~2.7k tokens), so
they count what the API charges: width x height / 750 after scaling to 2576 pixels on the long edge and 3.75
megapixels (within ~10% of the measured cost of 155 images in real sessions), a PDF ~3000 per page.
Compaction starts BUFFER tokens below the window (/context: "Autocompact buffer 33k"). The window, first match
wins: CLAUDE_CODE_AUTO_COMPACT_WINDOW, the subagent definition's autoCompactWindow (agent_definition: also a
plugin's agents, e.g. the cc mod's workers at 213k),
modelSettings.<model>.autoCompactWindow in the settings files (local > project > user), the model's context
window in Claude Code's model catalog (fetched from Anthropic, so it knows new models; at most 200k with
CLAUDE_CODE_DISABLE_1M_CONTEXT), else 200k (Claude Code's own fallback), or 1M once usage is past 200k.

With CONTAINEDCLAUDE_COMPACT_OFFER (contained sessions), the main session also hears once, past that many tokens
and before the notice above is due, that it may ask for compaction (the cc mod acts on it).
"""
import base64
import binascii
import glob
import json
import os
import re
import stat
import struct
import sys

BUFFER = 33000                                 # Claude Code compacts this far below the window
MARGIN = 25000                                 # warn this far before that: room to write notes (default)
DEFAULT_WINDOW, LONG_WINDOW = 200000, 1000000
TAIL = 65536
STATE = "/tmp/.containedclaude-context-%d" % os.getuid()     # private /tmp in the sandbox, shared outside
IMAGE_EDGE, IMAGE_AREA, IMAGE_MAX = 2576, 3750000, 4784       # the API's image limits; tokens = area / 750
PDF_PAGE = 3000                                # tokens per PDF page (text + page image), rough
BINARY = ("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR", "JVBERi")  # base64 of PNG, JPEG, GIF, RIFF (WebP), PDF
HEADER = 262144                                # base64 characters decoded to find an image's size


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


def image_size(raw):
    """(width, height) from the header of a PNG, GIF or JPEG, else None."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        return struct.unpack(">II", raw[16:24])
    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        return struct.unpack("<HH", raw[6:10])
    if raw[:2] == b"\xff\xd8":                    # JPEG: segments up to the frame header (SOFn)
        i = 2
        while i + 9 <= len(raw) and raw[i] == 0xFF:
            marker = raw[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    return None


def image_tokens(size):
    """What the API charges for an image of `size` pixels; the most an image can cost if the size is unknown."""
    if not size or min(size) <= 0:
        return IMAGE_MAX
    w, h = size
    scale = min(1.0, IMAGE_EDGE / max(w, h))
    scale = min(scale, (IMAGE_AREA / (w * h)) ** 0.5)
    return min(IMAGE_MAX, int(w * h * scale * scale / 750) + 1)


def binary_tokens(s):
    """Tokens of a base64 image or PDF (a string starting with one of BINARY); None for any other string."""
    if len(s) < 1000 or not s.startswith(BINARY):
        return None
    try:
        raw = base64.b64decode(s if s.startswith("JVBERi") else s[:HEADER - HEADER % 4])
    except (binascii.Error, ValueError):
        return None
    if raw[:4] == b"%PDF":
        pages = len(re.findall(rb"/Type\s*/Page(?!s)", raw)) or \
            max([int(n) for n in re.findall(rb"/Count\s+(\d+)", raw)] or [0]) or len(raw) // 50000 + 1
        return pages * PDF_PAGE
    if raw[:4] == b"RIFF" and raw[8:12] != b"WEBP":
        return None
    return image_tokens(image_size(raw))


def weigh(value):
    """Estimated tokens of a JSON value the next request carries: text at characters / 3, base64 images and
    PDFs (binary_tokens) at what the API charges for them."""
    binary = 0
    def walk(v):
        nonlocal binary
        if isinstance(v, str):
            t = binary_tokens(v)
            if t is None:
                return v
            binary += t
            return ""
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return len(json.dumps(walk(value))) // 3 + binary


def added_tokens(rec):
    """Tokens of a transcript record that the next request carries (weigh): user messages and tool results (not
    the toolUseResult copy) and attachments (hook output, files). Metadata records and the attachments that only
    record something (RECORD_ONLY) count nothing."""
    if rec.get("type") == "user":
        return weigh((rec.get("message") or {}).get("content") or "")
    if rec.get("type") == "attachment" and (rec.get("attachment") or {}).get("type") not in RECORD_ONLY:
        return weigh(rec.get("attachment") or "")
    return 0


# Attachments that are never sent: a copy of the prompt (up to ~70k tokens, after compactions), the deferred-tool
# bookkeeping, a hook's raw output (what the model gets is the hook_additional_context record next to it)
RECORD_ONLY = {"prompt_snapshot", "deferred_tools_record", "hook_success"}


def tool_ids(rec):
    content = (rec.get("message") or {}).get("content") if rec.get("type") == "user" else None
    return {c.get("tool_use_id") for c in content if isinstance(c, dict)} if isinstance(content, list) else set()


def newest_usage(path):
    """(tokens, model, tokens after, tool ids after): the newest assistant record with usage, and what the
    records written after it add (added_tokens) and which tool results are among them. Reads the file backwards
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
                after += added_tokens(rec)
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


def frontmatter(path):
    """Top-level fields of a Markdown file's frontmatter: values as strings, block lists ("- x") as lists;
    indented (nested) lines are skipped."""
    try:
        with open(path) as fh:
            head = fh.read(8192).split("\n")
    except OSError:
        return {}
    if not head or head[0].strip() != "---":
        return {}
    fields, key = {}, None
    for line in head[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t", "-"):
            item = line.strip()
            if item.startswith("- ") and isinstance(fields.get(key), list):
                fields[key].append(item[2:].strip().strip("'\""))
            continue
        key, _, v = line.partition(":")
        key, v = key.strip(), v.strip()
        fields[key] = v.strip("'\"") if v else []
    return fields


def agent_definition(agent_type, project, config):
    """Frontmatter of the definition of agent_type: in the project's and the user's agents folders, or for
    "<plugin>:<name>" in the agents folder of that plugin among CLAUDE_CODE_PLUGIN_DIRS (the cc mod's
    workers); {} if none (built-in types)."""
    plugin, _, name = agent_type.rpartition(":")
    if plugin:
        dirs = [os.path.join(p, "agents") for p in (os.environ.get("CLAUDE_CODE_PLUGIN_DIRS") or "").split(os.pathsep)
                if p and load_json(os.path.join(p, ".claude-plugin", "plugin.json")).get("name") == plugin]
    else:
        dirs = [os.path.join(project, ".claude", "agents"), os.path.join(config, "agents")]
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.md"))):
            fields = frontmatter(f)
            if fields.get("name") == name:
                return fields
    return {}


def catalog_window(config, model):
    """The model's context window as Claude Code reads it from its model catalog cache (runtime.max_input_tokens,
    else context_window), from the newest catalog file that lists the model; None if none does."""
    def mtime(f):
        try:
            return os.path.getmtime(f)
        except OSError:
            return 0
    def get(d, key):
        return d.get(key) if isinstance(d, dict) else None
    files = glob.glob(os.path.join(config, "cache", "model-catalog", "*.json"))
    for f in sorted(files, key=mtime, reverse=True):
        models = get(get(load_json(f).get("catalog"), "config"), "models")
        for m in models if isinstance(models, list) else []:
            if get(m, "id") == model:
                w = positive_int(get(get(m, "runtime"), "max_input_tokens") or get(m, "context_window"))
                if w is not None:
                    return min(w, DEFAULT_WINDOW) if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") else w
    return None


def window(model, tokens, definition, project, config):
    w = positive_int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if w is None:
        w = positive_int(definition.get("autoCompactWindow"))
    if w is None:
        for f in (os.path.join(project, ".claude", "settings.local.json"),
                  os.path.join(project, ".claude", "settings.json"), os.path.join(config, "settings.json")):
            ms = load_json(f).get("modelSettings")
            entry = ms.get(model) if isinstance(ms, dict) else None
            w = positive_int(entry.get("autoCompactWindow")) if isinstance(entry, dict) else None
            if w is not None:
                break
    if w is None and model:
        w = catalog_window(config, model)
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
        after += weigh(data.get("tool_response"))
    tokens += after
    try:                     # the config's [claude] compact_notice, through the session settings' env; 0 = off
        margin = int(os.environ.get("CONTAINEDCLAUDE_COMPACT_NOTICE", MARGIN))
    except ValueError:
        margin = MARGIN
    try:                     # [claude] compact_offer (contained sessions only); unset or 0 = off
        offer = 0 if agent else int(os.environ.get("CONTAINEDCLAUDE_COMPACT_OFFER") or 0)
    except ValueError:
        offer = 0
    if margin <= 0 and offer <= 0:
        return 0
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    definition = agent_definition(data.get("agent_type") or "", project, config) if agent else {}
    w = window(model, tokens, definition, project, config)
    d = state_dir()
    if d is None:
        return 0
    flag = os.path.join(d, "".join(c if c.isalnum() or c in "-_" else "_" for c in session + "." + (agent or "main")))
    trigger = w - BUFFER
    near = margin > 0 and tokens >= trigger - margin
    if once(flag, near):
        tail = NEAR_MAIN if not agent else NEAR_READER if read_only(data.get("agent_type"), definition) else NEAR_SUB
        msg = NEAR % (tokens // 1000, max(trigger, 0) // 1000) + tail
    elif once(flag + ".offer", offer > 0 and tokens >= offer and not near):
        msg = OFFER % (tokens // 1000)
    else:
        return 0
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}, sys.stdout)
    return 0


NEAR = ("containedclaude: your context holds about %dk tokens and auto-compaction starts at about %dk, so it will "
        "summarize this conversation soon and details that live only in the conversation may be lost. ")
NEAR_MAIN = ("Consider writing down what matters (state, decisions, paths, open work) where you or the user will find "
             "it; then continue as you judge best.")
# subagents: a worker told only "may be lost" stopped and handed back unfinished work (2026-11-11)
GO_ON = "then go on with your task: compaction keeps the task, so it is no reason to stop or hand back early."
NEAR_SUB = ("Consider writing down what matters (state, decisions, paths, open work) where you or your caller will "
            "find it, " + GO_ON)
NEAR_READER = "Put the findings so far in your next message, " + GO_ON      # can't write files
WRITERS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
READ_ONLY_BUILTIN = {"Explore", "Plan"}       # built-in types without these tools (no definition file)


def read_only(agent_type, definition):
    """Whether the agent can't write files: no Write, Edit or Bash in its definition's tools (minus
    disallowedTools); without a definition, a built-in read-only type."""
    if not definition:
        return agent_type in READ_ONLY_BUILTIN
    def names(v):
        items = v if isinstance(v, list) else v.strip("[]").split(",")
        return {i.strip().strip("'\"").split("(")[0].strip() for i in items if i.strip()}
    tools = names(definition.get("tools") or "")
    able = WRITERS if not tools or "*" in tools else tools & WRITERS     # no tools field: all tools
    return not able - names(definition.get("disallowedTools") or "")
# cc-compact: sandbox/bin, done by the cc mod (sandbox/mods/cc/hooks/register.ts)
OFFER = ("containedclaude: your context holds about %dk tokens. If much of it is no longer needed (images and logs "
         "already read, finished subtasks) and a good part of the work remains (more than about 20 tool calls), "
         "you can have it compacted: write down what matters, run `cc-compact \"<what the summary must keep>\"`, "
         "and end your reply. The conversation is compacted when your turn ends, and you are prompted to "
         "continue. Otherwise carry on; this notice comes once.")


def once(flag, due):
    """True the first time `due` holds since it last didn't (the flag file remembers); False if the flag can't
    be made, as the notice would otherwise repeat at every call."""
    if not due:
        if os.path.exists(flag):
            try:
                os.unlink(flag)
            except OSError:
                pass
        return False
    if os.path.exists(flag):
        return False
    try:
        os.close(os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
    except OSError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
