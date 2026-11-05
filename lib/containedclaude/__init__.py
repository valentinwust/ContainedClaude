"""ContainedClaude: run Claude Code (and its jobs) in a sandbox that can only write to one project folder."""
import os

__version__ = "0.1.0"

CLONE = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
