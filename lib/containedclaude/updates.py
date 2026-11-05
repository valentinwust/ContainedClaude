"""Update check for ContainedClaude itself (the clone), the host's Claude Code and Codex (the sandbox runs the
host's installs and cannot update them inside): once a day at session start, compare the clone with its
upstream and the installed versions with the latest on npm and say so; `containedclaude update` pulls the
clone and runs the two updaters on the host."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from . import CLONE, compat
from .config import STATE

CACHE = os.path.join(STATE, "update-check.json")
INTERVAL = 24 * 3600
TOOLS = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}     # name -> npm package
UPDATE_CMD = {"claude": ["update"], "codex": ["update"]}
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
CLONE_KEY = "ContainedClaude"                       # the clone's entry next to the tools
LAUNCHER = os.path.join(CLONE, "bin", "containedclaude")
PULLED_VAR = "CONTAINEDCLAUDE_UPDATE_PULLED"        # `update` re-execs itself after a pull: the old HEAD
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never wait for a password at a session start


# ---- the clone itself ----------------------------------------------------------------------------------
def _git(args, timeout=10):
    """stdout of a successful `git -C <clone> ...` (None on failure, timeout or without git)."""
    git = compat.tool("git")
    if not git:
        return None
    try:
        r = subprocess.run([git, "-C", CLONE] + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=timeout, env=GIT_ENV)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def clone_head():
    """The clone's HEAD commit, read from .git/ without running git (also works for a clone owned by someone
    else, which git refuses to look at); None if the clone is not a git checkout."""
    gitdir = os.path.join(CLONE, ".git")
    try:
        if os.path.isfile(gitdir):                          # a worktree: "gitdir: PATH"
            with open(gitdir) as fh:
                gitdir = fh.read().split(":", 1)[1].strip()
        with open(os.path.join(gitdir, "HEAD")) as fh:
            head = fh.read().strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        try:
            with open(os.path.join(gitdir, ref)) as fh:
                return fh.read().strip() or None
        except OSError:
            with open(os.path.join(gitdir, "packed-refs")) as fh:
                for line in fh:
                    if line.strip().endswith(" " + ref):
                        return line.split()[0]
    except (OSError, IndexError):
        pass
    out = _git(["rev-parse", "HEAD"])
    return out.strip() if out else None


def _clone_writable():
    return os.access(os.path.join(CLONE, ".git"), os.W_OK)


def clone_status(fetch=True):
    """{"installed": short HEAD, "upstream": "origin/main"|None, "behind": n|None}. `behind` is None when it
    could not be checked: no upstream, git unusable, or the fetch failed (no network; a clone we can't write
    to is compared with its last fetch)."""
    head = clone_head()
    res = {"installed": head[:7] if head else "?", "upstream": None, "behind": None}
    if not head:
        return res
    up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not up:
        return res
    res["upstream"] = up.strip()
    if fetch and _clone_writable() and _git(["fetch", "--quiet"], timeout=15) is None:
        return res
    n = _git(["rev-list", "--count", "HEAD..@{u}"])
    if n and n.strip().isdigit():
        res["behind"] = int(n)
    return res


def _clone_text(r):
    if r.get("behind"):
        return "%d commit%s behind %s" % (r["behind"], "" if r["behind"] == 1 else "s", r["upstream"])
    if r.get("behind") == 0:
        return "up to date with %s" % r["upstream"]
    return "%s not reachable" % r["upstream"] if r.get("upstream") else "not checked"


def pull_clone(out=sys.stderr):
    """`git pull --ff-only` in the clone if that is safe: writable by us, an upstream, no local changes to
    tracked files. Returns "updated", "unchanged" or "skipped" (the reason is printed)."""
    def skip(why):
        out.write("containedclaude: clone %s not pulled: %s\n" % (CLONE, why))
        return "skipped"
    if not os.path.exists(os.path.join(CLONE, ".git")):
        return skip("not a git checkout")
    if not _clone_writable():
        return skip("not writable (a shared install: its owner updates it)")
    git = compat.tool("git")
    if not git:
        return skip("git not found")
    if not _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]):
        return skip("the current branch has no upstream (or git cannot use this checkout)")
    dirty = _git(["status", "--porcelain", "--untracked-files=no"])
    if dirty is None:
        return skip("git status failed")
    names = [l[3:] for l in dirty.splitlines() if l.strip()]      # "XY path" per line
    if names:
        return skip("local changes in %s%s" % (", ".join(names[:3]),
                                                 " and %d more" % (len(names) - 3) if len(names) > 3 else ""))
    before = clone_head()
    out.write("containedclaude: running git pull --ff-only in %s\n" % CLONE)
    out.flush()
    try:
        r = subprocess.run([git, "-C", CLONE, "pull", "--ff-only", "--quiet"], timeout=120, env=GIT_ENV)
    except (OSError, subprocess.TimeoutExpired) as e:
        return skip(str(e))
    if r.returncode != 0:
        return skip("git pull failed (local commits that are not on the upstream, or no network)")
    return "updated" if clone_head() != before else "unchanged"


# ---- the host's Claude Code and Codex --------------------------------------------------------------------


def _installed(name):
    exe = compat.tool(name)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    m = VERSION_RE.search(out)
    return m.group(0) if m else "?"


def _latest(package, timeout=5.0):
    try:
        with urllib.request.urlopen("https://registry.npmjs.org/%s/latest" % package, timeout=timeout) as r:
            v = json.load(r).get("version", "")
        return v if VERSION_RE.fullmatch(v) else None
    except Exception:
        return None


def _key(v):
    return tuple(int(x) for x in v.split("."))


def _is_current(installed, latest):
    """True if the installed version is the latest known one (False when behind or when either is unknown,
    so the tool's own updater decides then)."""
    try:
        return bool(installed and latest) and _key(installed) >= _key(latest)
    except ValueError:
        return False


def check(fetch=True):
    """{tool: {"installed": version|"?", "latest": version|None}} for the installed tools, after the clone's
    entry ({"installed": commit, "upstream", "behind"}); fresh (a fetch and two registry lookups)."""
    res = {CLONE_KEY: clone_status(fetch)}
    for name, package in TOOLS.items():
        inst = _installed(name)
        if inst is None:
            continue
        res[name] = {"installed": inst, "latest": _latest(package)}
    return res


def _load_cache():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_cache(res):
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        with open(CACHE, "w") as fh:
            json.dump({"checked": time.time(), "tools": res}, fh)
    except OSError:
        pass


def report(res, out):
    """Print installed/latest per tool and a warning with instructions if something is behind."""
    behind = []
    parts = []
    for name, r in res.items():
        inst, latest = r.get("installed"), r.get("latest")
        if name == CLONE_KEY:
            parts.append("%s %s (%s)" % (name, inst, _clone_text(r)))
            if r.get("behind"):
                behind.append(name)
            continue
        parts.append("%s %s (latest %s)" % (name, inst, latest or "unknown"))
        try:
            if latest and inst and _key(latest) > _key(inst):
                behind.append(name)
        except ValueError:
            pass
    if parts:
        out.write("containedclaude: " + "   ".join(parts) + "\n")
    if behind:
        how = ["git pull in the clone" if n == CLONE_KEY else "%s %s" % (n, " ".join(UPDATE_CMD[n])) for n in behind]
        out.write("containedclaude: WARNING: %s %s not up to date. Update outside the sandbox: "
                  "`containedclaude update` (runs %s); plugins: `containedclaude admin` + /plugin update\n" %
                  (" and ".join(behind), "is" if len(behind) == 1 else "are", " + ".join(how)))
    return bool(behind)


def notice(out=sys.stderr):
    """At most once a day: check and report (silent in between and when the check fails entirely)."""
    c = _load_cache()
    if c and time.time() - c.get("checked", 0) < INTERVAL:
        return
    out.write("containedclaude: daily update check (git fetch, npm registry; a few seconds)...\n")
    out.flush()
    res = check()
    _save_cache(res)
    report(res, out)


# ---- "did the binaries change?" and the quick self-check after a change --------------------------------
IDENTITY = os.path.join(STATE, "tool-identity.json")


def identity():
    """{tool: "realpath size mtime"} of the installed claude/codex binaries and the clone's HEAD commit: a
    change means an update (or a reinstall, or a `git pull`) happened on the host since the last session,
    found without running anything."""
    ids = {}
    head = clone_head()
    if head:
        ids[CLONE_KEY] = head
    for name in TOOLS:
        exe = compat.tool(name)
        if not exe:
            continue
        try:
            rp = os.path.realpath(exe)
            st = os.stat(rp)
            ids[name] = "%s %d %d" % (rp, st.st_size, int(st.st_mtime))
        except OSError:
            pass
    return ids


def _load_identity():
    try:
        with open(IDENTITY) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_identity(ids):
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        with open(IDENTITY, "w") as fh:
            json.dump(ids, fh)
    except OSError:
        pass


def changed_tools():
    """Tools whose binary differs from the last recorded one (None if nothing was recorded yet)."""
    old = _load_identity()
    if old is None:
        return None
    new = identity()
    return [t for t in new if old.get(t) != new[t]]


def quick_check(root, out=sys.stderr, what="updated tools"):
    """Session-level doctor on `root` (a few seconds): the summary line only, the FAIL lines if any.
    Returns True if it passed."""
    from . import doctor
    import io
    buf = io.StringIO()
    out.write("containedclaude: %s: running the quick sandbox self-check (`containedclaude doctor`)...\n" % what)
    out.flush()
    try:
        ok = doctor.doctor(root, out=buf) == 0
    except Exception as e:                      # never let the check itself stop a start
        out.write("containedclaude: WARNING: self-check could not run: %s\n" % e)
        return False
    notes = [l.strip()[6:].strip()[:110] for l in buf.getvalue().splitlines() if l.startswith("  note  ")]
    if ok:
        out.write("containedclaude: self-check passed (ALL CHECKS PASSED)\n")
        out.write("".join("  note: %s\n" % n for n in notes[:4]))
    else:
        fails = [l for l in buf.getvalue().splitlines() if "FAIL" in l]
        out.write("containedclaude: WARNING: self-check FAILED after %s; run `containedclaude doctor %s` for "
                  "details:\n%s" % (what, root, "".join("  %s\n" % l.strip()[:110] for l in fails[:8])))
    return ok


def check_after_change(root, out=sys.stderr):
    """At a session start: if claude/codex changed since the last start, run the quick doctor on this project
    (warn and continue on failure: the host's fail-closed pieces are checked by compat anyway)."""
    changed = changed_tools()
    if changed:
        quick_check(root, out, "%s changed since the last session" % " and ".join(changed))
    _save_identity(identity())


def update_all(out=sys.stderr, check_only=False, project=None, pull=True, argv=()):
    """`containedclaude update`: `git pull --ff-only` the clone (then continue in the pulled code), run the
    host's `claude update` and `codex update` (if installed), check the versions, and if anything changed run
    the quick test + doctor on `project` (or a throw-away project). `argv`: the command's own arguments, for
    the re-exec after a pull."""
    rc = 0
    before = identity()
    pulled = os.environ.pop(PULLED_VAR, None)        # we are the re-exec after a pull: the HEAD before it
    if pulled:
        before[CLONE_KEY] = pulled
    if not check_only and pull:
        if pull_clone(out) == "updated":
            out.flush()
            env = dict(os.environ)
            env[PULLED_VAR] = before.get(CLONE_KEY) or "?"
            os.execve(sys.executable, [sys.executable, "-I", LAUNCHER, "update", "--no-pull"] + list(argv), env)
    if not check_only:
        pre = check(fetch=False)            # `codex update` reinstalls even when current: only run what is behind
        for name, args in UPDATE_CMD.items():
            exe = compat.tool(name)
            if not exe:
                out.write("containedclaude: %s not installed, skipped\n" % name)
                continue
            inst, latest = pre.get(name, {}).get("installed"), pre.get(name, {}).get("latest")
            if _is_current(inst, latest):
                out.write("containedclaude: %s %s is the latest release, skipped\n" % (name, inst))
                continue
            out.write("containedclaude: running %s %s\n" % (name, " ".join(args)))
            out.flush()
            try:
                r = subprocess.run([exe] + args, timeout=900)
                rc = rc or r.returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                out.write("containedclaude: %s update failed: %s\n" % (name, e))
                rc = rc or 1
        compat._cache.clear()
    res = check()
    _save_cache(res)
    behind = report(res, out)
    if check_only:
        return rc
    changed = [t for t in identity() if before.get(t) != identity()[t]]
    if not changed:
        out.write("containedclaude: nothing changed%s\n" % ("" if behind else "; everything up to date"))
        return rc
    out.write("containedclaude: %s changed: checking that everything still works\n" % " and ".join(changed))
    ok = self_test(project, out)
    _save_identity(identity())
    return rc or (0 if ok else 1)


def self_test(project, out=sys.stderr):
    """Quick `test` + session `doctor` on `project`, or on a throw-away project below the home folder."""
    from . import project as proj
    from .config import HOME
    import shutil
    import tempfile
    root = proj.find_root(project) if project else None
    tmp = None
    if not root:
        tmp = tempfile.mkdtemp(prefix=".containedclaude-selftest-", dir=HOME)
        try:
            proj.init(tmp, out=io_null())
        except proj.InitError as e:
            out.write("containedclaude: cannot create a test project in %s: %s\n" % (HOME, e))
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        root = tmp
    try:
        from . import CLONE
        r = subprocess.run([sys.executable, "-I", os.path.join(CLONE, "bin", "containedclaude"), "test", root],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
        lines = r.stdout.splitlines()
        summary = [l for l in lines if "EVERYTHING WORKS" in l or "failed" in l.lower()]
        fails = [l for l in lines if l.strip().startswith("FAIL")]
        out.write("containedclaude: test: %s\n%s" % ((summary or ["no result"])[-1].strip(),
                                                      "".join("  %s\n" % l.strip()[:110] for l in fails[:8])))
        ok = r.returncode == 0 and not fails
        ok = quick_check(root, out, "the update") and ok
    finally:
        if tmp:
            from . import policy
            try:        # its runtime folder and Codex folder trust too; both named after the real path
                shutil.rmtree(policy.project_runtime(os.path.realpath(tmp)))
            except OSError:
                pass
            proj.codex_untrust([os.path.realpath(tmp)])
            shutil.rmtree(tmp, ignore_errors=True)
            compat.unregister([tmp])
    return ok


def io_null():
    import io
    return io.StringIO()
