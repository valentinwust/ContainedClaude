#!/usr/bin/env python3
"""UserPromptSubmit hook of the idle-compact mod: when the prompt is the mod's one-line notice (LINE in
register.ts), give the model the notice itself as additionalContext, which the person doesn't see."""
import json
import re
import sys

LINE = re.compile(r"Idle for \d+ min with \d+k tokens: notes, then compaction before the prompt cache expires\.")
NOTICE = ("containedclaude: the prompt cache expires soon, so the conversation will be compacted right after "
          "your reply, and details that live only in the conversation may be lost. Write down what matters "
          "(state, decisions, paths, open work) where you or the user will find it, then reply briefly. "
          "Background tasks that are still running notify you when they end: don't wait for them now.")

try:
    prompt = json.load(sys.stdin).get("prompt")
except (ValueError, AttributeError):
    sys.exit(0)
if isinstance(prompt, str) and LINE.fullmatch(prompt.strip()):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": NOTICE}}))
