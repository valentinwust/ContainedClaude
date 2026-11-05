"""Feature detection for the external tools, so updates of bwrap/unshare/apptainer/claude degrade gracefully.

Principle: detect flags from each tool's own --help output (never compare version numbers). Flags that the
security of the sandbox depends on are REQUIRED: if missing, refuse to start with a clear message. Convenience
flags are OPTIONAL: if missing, leave them out and warn once. Results are cached per process.
"""
import os
import re
import shutil
import subprocess

from .config import MARKER, STATE

_cache = {}
_warned = set()
FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")

# bwrap: everything the policy cannot work without (recursive ro root, binds, tmpfs, own pid ns, die with launcher)
BWRAP_REQUIRED = ["--ro-bind", "--bind", "--dev-bind", "--tmpfs", "--dev", "--proc", "--unshare-pid",
                  "--die-with-parent", "--chdir", "--setenv"]
# optional: their absence weakens convenience or a secondary defence (each is reported by `doctor`)
BWRAP_OPTIONAL = ["--remount-ro", "--seccomp", "--unshare-ipc", "--unshare-cgroup-try", "--dir"]
UNSHARE_REQUIRED = ["--user", "--mount", "--propagation"]
APPTAINER_OPTIONAL = ["--userns", "--workdir", "--home", "--pwd", "--nv", "--containall", "--env"]
CLAUDE_OPTIONAL = ["--settings", "--strict-mcp-config", "--mcp-config"]


# ---- trusted host environment ---------------------------------------------------------------------------
# The launcher, the broker and jobwrap run OUTSIDE the sandbox. Every program they start (unshare, bwrap, sbatch,
# `claude --help`, ...) must come from a folder no sandbox can write to, also when a project environment is on the
# user's PATH.
# harden_host_env() therefore removes PATH / LD_LIBRARY_PATH / LD_PRELOAD entries inside any ContainedClaude
# project or registered write_path from this process's environment (inherited by sbatch -> jobs too) and keeps
# the original values in CONTAINEDCLAUDE_INNER_<NAME>, which are restored INSIDE the sandbox only.
SYSTEM_PATH = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin"]
INNER_VARS = ("PATH", "LD_LIBRARY_PATH", "LD_PRELOAD")
REGISTRY = os.path.join(STATE, "write_paths.txt")     # hidden inside every sandbox


def _under(p, roots):
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def registered():
    try:
        with open(REGISTRY) as fh:
            return [l.strip() for l in fh if l.strip().startswith("/")]
    except OSError:
        return []


def register(paths):
    """Remember writable paths of every project ever started, so tools there are never trusted on the host."""
    have = set(registered())
    new = [p for p in paths if p not in have]
    if new:
        try:
            os.makedirs(STATE, mode=0o700, exist_ok=True)
            with open(REGISTRY, "a") as fh:
                fh.write("".join(p + "\n" for p in new))
        except OSError:
            pass


def unregister(paths):
    """Forget write paths again (throw-away self-test projects)."""
    keep = [p for p in registered() if p not in paths]
    try:
        with open(REGISTRY, "w") as fh:
            fh.write("".join(p + "\n" for p in keep))
    except OSError:
        pass


def untrusted(p, extra=()):
    """True if p lies in a folder some sandbox can write to: inside a ContainedClaude project, or in a
    registered (or given) write path."""
    p = os.path.realpath(p)
    if _under(p, list(extra) + registered()):
        return True
    d = p
    while True:
        if os.path.isfile(os.path.join(d, MARKER, "config.ini")):
            return True
        if d in ("/", ""):
            return False
        d = os.path.dirname(d)


def inner(name):
    """The user's original value of PATH / LD_LIBRARY_PATH / LD_PRELOAD (for use inside the sandbox)."""
    return os.environ.get("CONTAINEDCLAUDE_INNER_" + name, os.environ.get(name))


def harden_host_env(extra=()):
    """Remove untrusted entries from PATH, LD_LIBRARY_PATH and LD_PRELOAD of this process (idempotent)."""
    for name in INNER_VARS:
        if name in os.environ:
            os.environ.setdefault("CONTAINEDCLAUDE_INNER_" + name, os.environ[name])
    trusted = lambda d: d and os.path.isabs(d) and not untrusted(d, extra)
    path = [d for d in os.environ.get("PATH", "").split(os.pathsep) if trusted(d)]
    os.environ["PATH"] = os.pathsep.join(path + [d for d in SYSTEM_PATH if d not in path])
    for name, sep in (("LD_LIBRARY_PATH", ":"), ("LD_PRELOAD", None)):
        if name in os.environ:
            parts = re.split(r"[: ]+", os.environ[name]) if sep is None else os.environ[name].split(sep)
            keep = [x for x in parts if trusted(x)]
            if keep:
                os.environ[name] = (" " if sep is None else sep).join(keep)
            else:
                del os.environ[name]
    _cache.clear()


def tool(name):
    """Absolute path of a host program from the trusted PATH (None if missing or only found in a writable
    place, also through a symlink)."""
    exe = shutil.which(name)
    if not exe or untrusted(exe):
        return None
    return exe


def flags(*cmd):
    """Set of --long-flags mentioned in `cmd --help` (cached). Empty set if the tool is missing."""
    key = tuple(cmd)
    if key not in _cache:
        exe = tool(cmd[0])
        out = ""
        if exe:
            try:
                r = subprocess.run([exe] + list(cmd[1:]) + ["--help"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
                out = r.stdout
            except (OSError, subprocess.TimeoutExpired):
                pass
        _cache[key] = set(FLAG_RE.findall(out))
    return _cache[key]


def has(tool, flag):
    if tool == "bwrap":
        return flag in flags("bwrap")
    if tool == "unshare":
        return flag in flags("unshare")
    if tool == "apptainer":
        return flag in flags("apptainer", "exec")
    if tool == "claude":
        return flag in flags("claude")
    raise ValueError(tool)


def likely(tool, flag):
    """Like has(), but True when the --help output couldn't be read at all (then the flag is passed as before
    and the tool itself reports a problem, instead of us silently dropping every flag)."""
    key = {"apptainer": ("apptainer", "exec")}.get(tool, (tool,))
    return not flags(*key) or has(tool, flag)


def warn_once(msg):
    if msg not in _warned:
        _warned.add(msg)
        import sys
        sys.stderr.write("containedclaude: warning: %s\n" % msg)


def problems_sandbox():
    """Blocking problems for creating a sandbox on this host (empty list = fine)."""
    p = []
    if not tool("bwrap"):
        return ["bwrap (bubblewrap) not found (outside ContainedClaude projects)"]
    if not tool("unshare"):
        return ["unshare (util-linux) not found (outside ContainedClaude projects)"]
    miss = [f for f in BWRAP_REQUIRED if not has("bwrap", f)]
    if miss:
        p.append("bwrap lacks required options %s (bwrap too old?)" % " ".join(miss))
    miss = [f for f in UNSHARE_REQUIRED if not has("unshare", f)]
    if not (has("unshare", "--map-current-user") or has("unshare", "--map-user")):
        miss.append("--map-current-user/--map-user")
    if miss:
        p.append("unshare lacks required options %s (util-linux too old?)" % " ".join(miss))
    return p


SCOPE_PROPS = ["MemorySwapMax=0", "OOMPolicy=continue"]   # with MemoryMax=...; continue: kill one process, not all


def user_controllers():
    """cgroup controllers the systemd user manager may use for its scopes (e.g. {'memory', 'pids'}; 'cpu'
    only if the admin delegates it: then CPUQuota= is a hard cap, otherwise the CPU limit is affinity+nice)."""
    p = "/sys/fs/cgroup/user.slice/user-%d.slice/user@%d.service/cgroup.subtree_control" % (os.getuid(),
                                                                                              os.getuid())
    try:
        with open(p) as fh:
            return set(fh.read().split())
    except OSError:
        return set()


def scope_error(runtime_dir, props):
    """Why `systemd-run --user --scope` with these properties cannot be used on this host ("" if it can). Tried
    once per process with exactly the properties the session uses."""
    key = ("scope", runtime_dir, tuple(props))
    if key not in _cache:
        exe = tool("systemd-run")
        if not exe:
            _cache[key] = "systemd-run not found"
        elif not runtime_dir or not os.path.isdir(runtime_dir):
            _cache[key] = "no XDG_RUNTIME_DIR (no systemd user session)"
        else:
            env = dict(os.environ, XDG_RUNTIME_DIR=runtime_dir)
            cmd = [exe, "--user", "--scope", "--quiet"]
            for p in props:
                cmd += ["-p", p]
            try:
                r = subprocess.run(cmd + ["--", "/bin/true"], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, universal_newlines=True, timeout=20)
                _cache[key] = "" if r.returncode == 0 else (r.stdout.strip().splitlines() or ["failed"])[-1]
            except (OSError, subprocess.TimeoutExpired) as e:
                _cache[key] = str(e)
    return _cache[key]


def optional_report():
    """(tool, flag, available) for doctor."""
    rows = [("bwrap", f, has("bwrap", f)) for f in BWRAP_OPTIONAL]
    if tool("apptainer"):
        rows += [("apptainer exec", f, has("apptainer", f)) for f in APPTAINER_OPTIONAL]
    if tool("claude"):
        rows += [("claude", f, has("claude", f)) for f in CLAUDE_OPTIONAL]
    return rows


def outer_unshare():
    """A private mount namespace around bwrap (keeps the user's own uid/gid mapping)."""
    a = [tool("unshare") or "unshare", "--user", "--mount", "--propagation", "private"]
    if has("unshare", "--map-current-user"):
        a.append("--map-current-user")
    else:
        a += ["--map-user=%d" % os.getuid(), "--map-group=%d" % os.getgid()]
    return a + ["--"]
