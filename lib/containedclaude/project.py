"""Project setup (init) and the interactive launcher (run)."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import CLONE, __version__, broker, compat, policy
from .config import (CLAUDE_CONFIG_DIR, CODEX_HOME, HOME, MARKER, SITE_DEFAULT, STATE, WRITE_MARKER, Config,
                     ensure_site_ini, krb5_shares, placement_problems, site_config, unsafe_write_paths)

TEMPLATES = os.path.join(CLONE, "templates")
IMPORT_LINE = "@CLAUDE_environment.md"
AGENTS_LINE = ("Read CLAUDE_environment.md, in the same folder as this file, first: it says whether you run in the "
               "ContainedClaude sandbox and, if so, its rules.")
# user settings copied into the contained Claude config on first setup (no hooks, status lines, helpers)
SEED_KEYS = ["model", "effortLevel", "theme", "tui", "modelSettings", "autoContinueAtUsageLimit",
             "alwaysThinkingEnabled", "outputStyle", "language", "includeCoAuthoredBy", "cleanupPeriodDays"]


class InitError(Exception):
    pass


def find_root(start):
    p = os.path.realpath(start)
    while True:
        if os.path.isfile(os.path.join(p, MARKER, "config.ini")):
            return p
        if p == "/":
            return None
        p = os.path.dirname(p)


def nesting_problem(root, write_paths=(), new=False):
    """Why a project at `root` must not be used (None if fine): it lies inside another project, or inside a
    write_path (marked with WRITE_MARKER by any user's project, or in this user's registry, or its own). A
    project's config and starter script must not be writable from any sandbox. new: init (the folder itself
    must not be a write path either; at a start a marker in the project folder itself is ignored: that folder
    is writable from its own sandbox)."""
    root = os.path.realpath(root)
    why = ("; that project's sandbox can rewrite this one's config and starter script, so nested projects are "
           "refused. Use a folder outside it")
    hint = (". If you did not create %s yourself, check it and delete it" %
               os.path.join(root, MARKER)) if os.path.isdir(os.path.join(root, MARKER)) else ""
    d = root if new else os.path.dirname(root)
    while True:
        m = os.path.join(d, WRITE_MARKER)
        if os.path.lexists(m):
            who = ", ".join("%s (%s)" % tuple(reversed(l.split(" ", 1))) if " " in l else l
                            for l in marker_lines(m)) or "a ContainedClaude project"
            where = root if d == root else "%s lies inside %s, which" % (root, d)
            return ("%s is a folder the sandbox of %s can write to%s%s. If no project writes there any more, "
                    "delete %s" % (where, who, why, hint, m))
        if d != root and os.path.isfile(os.path.join(d, MARKER, "config.ini")):
            return "%s lies inside the ContainedClaude project %s%s%s" % (root, d, why, hint)
        if d == "/":
            break
        d = os.path.dirname(d)
    for w in compat.registered() + [os.path.realpath(w) for w in write_paths]:
        if root.startswith(w.rstrip(os.sep) + os.sep):
            return ("%s lies inside %s, a write_path of a ContainedClaude project%s%s. If no project writes there "
                    "any more, remove that line from %s" % (root, w, why, hint, compat.REGISTRY))
    return None


def safe_write(path, text, mode=0o644):
    """Write without following symlinks: write a temp file next to it and rename over the path."""
    d = os.path.dirname(path)
    if os.path.islink(d) or os.path.islink(path):
        raise InitError("refusing to write %s: it (or its folder) is a symlink" % path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cc-tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_changed(path, text, mode=0o644):
    """safe_write only when the content differs, so an unchanged file is left as it is (sessions of the project
    that already run keep showing theirs). True if it was written."""
    try:
        if not os.path.islink(path):
            with open(path) as fh:
                if fh.read() == text:
                    return False
    except (OSError, ValueError):
        pass
    safe_write(path, text, mode)
    return True


def render(name, values):
    """A template with {{key}} from `values` and {{site:section.key}} from the built-in defaults + site.ini (a
    value of several lines goes on, commented, on lines of its own)."""
    with open(os.path.join(TEMPLATES, name)) as fh:
        text = fh.read()
    for k, v in values.items():
        text = text.replace("{{%s}}" % k, str(v))
    if "{{site:" in text:
        site = site_config()
        text = re.sub(r"\{\{site:(\w+)\.(\w+)\}\}", lambda m: "\n#    ".join(
            l.strip() for l in site.get(m.group(1), m.group(2), fallback="").splitlines() if l.strip()), text)
        text = re.sub(r"(?m)[ \t]+$", "", text)
    return text


# ---- shared contained config folders -------------------------------------------------------------------
def ensure_state():
    for d in (STATE, CLAUDE_CONFIG_DIR, CODEX_HOME, os.path.join(CLAUDE_CONFIG_DIR, "plugins", "data")):
        os.makedirs(d, mode=0o700, exist_ok=True)
    # the per-project entries of the Codex home (bound over inside, policy.project_store): mount points; its
    # read-only entries: created empty if missing (bwrap can only make existing paths read-only)
    for d in policy.CODEX_HOME_PER_PROJECT + policy.CODEX_HOME_PER_RUNTIME + policy.CODEX_HOME_PRECREATE_DIRS:
        os.makedirs(os.path.join(CODEX_HOME, d), mode=0o700, exist_ok=True)
    for f, text in [(f, "") for f in policy.CODEX_HOME_PER_PROJECT_FILES] + \
            sorted(policy.CODEX_HOME_PRECREATE_FILES.items()):
        if not os.path.lexists(os.path.join(CODEX_HOME, f)):
            safe_write(os.path.join(CODEX_HOME, f), text, 0o600)
    settings = os.path.join(CLAUDE_CONFIG_DIR, "settings.json")
    if not os.path.exists(settings):
        seed = {}
        try:
            with open(os.path.join(HOME, ".claude", "settings.json")) as fh:
                user = json.load(fh)
            seed = {k: user[k] for k in SEED_KEYS if k in user}
        except (OSError, ValueError):
            pass
        safe_write(settings, json.dumps(seed, indent=2) + "\n", 0o600)
    # bwrap can only make existing paths read-only: create the rest empty, so no project's agent can add a
    # CLAUDE.md, skill, agent or command that would load in every other contained project
    for d in policy.CLAUDE_CONFIG_PRECREATE_DIRS:
        p = os.path.join(CLAUDE_CONFIG_DIR, d)
        if not os.path.lexists(p):
            os.makedirs(p, mode=0o700)
    for f, text in policy.CLAUDE_CONFIG_PRECREATE_FILES.items():
        p = os.path.join(CLAUDE_CONFIG_DIR, f)
        if not os.path.lexists(p):
            safe_write(p, text, 0o600)
    # Codex's own sandbox (bwrap + seccomp, an inner layer here) blocks every socket, the job broker's unix
    # socket included, unless its workspace-write mode may use the network: allow that (the outer sandbox
    # gives sessions the network anyway). Once; the user's own settings in the file stay.
    toml = os.path.join(CODEX_HOME, "config.toml")
    try:
        with open(toml) as fh:
            text = fh.read()
    except OSError:
        text = ""
    new = text
    if "[sandbox_workspace_write]" not in new:
        new = new.rstrip("\n") + ("\n\n" if new.strip() else "") + \
            "# ContainedClaude: Codex's workspace-write sandbox may use sockets (the cc-* job commands talk\n" \
            "# to a unix socket) and the network, which the outer sandbox allows anyway.\n" \
            "[sandbox_workspace_write]\nnetwork_access = true\n"
    # no background server: Codex would copy itself (~500 MB) into packages/ (read-only inside) to run it
    if "daemon_auto_start" not in new:
        line = "daemon_auto_start = false   # ContainedClaude: no background server (it would install into packages/)\n"
        if re.search(r"(?m)^\[features\][ \t]*$", new):
            new = re.sub(r"(?m)^\[features\][ \t]*\n", lambda m: m.group(0) + line, new, 1)
        else:
            new = new.rstrip("\n") + "\n\n[features]\n" + line
    if new != text:
        safe_write(toml, new, 0o600)


def _toml_key(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def _git_top(path):
    """The top folder of the git repository `path` lies in (a .git folder or file above it), or None. Without
    running git (the launcher never runs programs on files a sandbox can write)."""
    d = path
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        if d == "/":
            return None
        d = os.path.dirname(d)


def codex_trust(cfg, folders=None):
    """Codex asks once per folder whether to trust it and saves the answer in its config.toml, which is read-only
    in sessions: record it here instead. Trust covers a folder and everything below it, except that inside a git
    repository Codex looks only at the repository's top folder. So: the project, the repository it lies in, and
    the repositories found inside it (a repository cloned during a session is asked about, and not saved)."""
    toml = os.path.join(CODEX_HOME, "config.toml")
    if not cfg.bool("sandbox", "codex") or not os.path.isfile(toml):
        return
    if folders is None:
        folders = [cfg.project, _git_top(cfg.project)] + [os.path.dirname(g) for g in cfg.git_dirs()
                                                          if os.path.basename(g) == ".git"]
    with open(toml) as fh:
        text = fh.read()
    if re.search(r"(?m)^[ \t]*projects[ \t]*=", text):
        return                          # an inline table of projects (not what Codex writes): leave it alone
    add = ""
    for f in dict.fromkeys(f for f in folders if f):
        header = "[projects.%s]" % _toml_key(f)
        if header not in text and header not in add:
            add += "\n%s\ntrust_level = \"trusted\"\n" % header
    if add:
        safe_write(toml, text.rstrip("\n") + "\n" + add, 0o600)


def codex_untrust(folders):
    """Removes what codex_trust recorded for these folders (the post-update self-test's throw-away project)."""
    toml = os.path.join(CODEX_HOME, "config.toml")
    try:
        with open(toml) as fh:
            text = fh.read()
    except OSError:
        return
    new = text
    for f in folders:
        new = new.replace("\n[projects.%s]\ntrust_level = \"trusted\"\n" % _toml_key(f), "")
    if new != text:
        safe_write(toml, new, 0o600)


def check_paths(cfg):
    """Problems with writable paths that would let the sandbox change what runs outside it."""
    return ["writable path %s is not allowed: %s (code there runs outside the sandbox: shell startup files, "
            "git/editor config, the ContainedClaude launcher)" % b for b in unsafe_write_paths(cfg.write_paths())]


OWNER_FILE = os.path.join(MARKER, "state", "owner")    # state/ is gitignored: a clone of the project is nobody's


def username():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError):
        return "uid %d" % os.getuid()


def recorded_owner(root):
    """The user name init wrote to .contained/state/owner ("" if the file is missing)."""
    try:
        with open(os.path.join(root, OWNER_FILE)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_owner(root):
    path = os.path.join(root, OWNER_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe_write(path, username() + "\n")


def foreign_owner_message(root, who):
    return ("%s was set up by %s; a project can only be used by the user who set it up (its config and starter "
            "script run as whoever starts them). Set up a folder of your own" % (root, who))


def owner_problem(root):
    """A project belongs to the user who set it up: its config and starter script run as whoever starts it, and
    its job records and logs are per user. The record init writes is the only thing consulted: file owners
    would not do (curry can't map the owner names the SDS reports: every file there shows as "nobody")."""
    who = recorded_owner(root)
    if who == username():
        return None
    if not who:
        return ("%s has no owner record (%s): set it up again with `containedclaude init --force %s`" %
                (root, OWNER_FILE, root))
    return foreign_owner_message(root, who)


class Slow:
    """`with Slow(out, "doing X"):` prints a line when the block takes longer than a moment (a file system that
    does not answer, a slow share), so a user who waits at the start sees what is going on, and a second line
    with the time when it is over. Silent otherwise."""
    def __init__(self, out, what, after=2.0):
        self.out, self.what, self.after, self.said = out, what, after, False

    def _say(self):
        self.said = True
        self.out.write("containedclaude: %s is taking a while...\n" % self.what)
        self.out.flush()

    def __enter__(self):
        self.t0 = time.time()
        self.timer = threading.Timer(self.after, self._say)
        self.timer.daemon = True
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.cancel()
        if self.said:
            self.out.write("containedclaude: %s: done after %.0f s\n" % (self.what, time.time() - self.t0))
            self.out.flush()


def kerberos_missing(paths):
    """The Kerberos-authenticated shares (NFS sec=krb5*: the SDS) among the mounts of `paths` when there is no
    valid ticket (`klist -s`); such a share refuses everything once the ticket has expired, as "Permission
    denied" after a wait. [] when no ticket is needed, when there is one, or when klist is not installed."""
    shares = krb5_shares([os.path.abspath(p) for p in paths])
    klist = compat.tool("klist") if shares else None
    if not klist:
        return []
    try:
        return [] if subprocess.run([klist, "-s"], timeout=10).returncode == 0 else shares
    except (OSError, subprocess.TimeoutExpired):
        return []


MARKED_FILE = os.path.join(MARKER, "state", "marked_write_paths")    # this project's root, then its marked paths
MARKER_HEADER = ("# ContainedClaude: the sandboxes of these projects (user, project folder) can write to this\n"
                 "# folder, so no project may be set up in it or below it. If a project no longer writes here,\n"
                 "# remove its line; delete this file when no line is left.\n")


def marker_lines(path):
    """The "user project" lines of a WRITE_MARKER file ([] if missing or unreadable)."""
    try:
        with open(path) as fh:
            return [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    except (OSError, UnicodeDecodeError):
        return []


def projects_inside(d):
    """ContainedClaude projects at or below `d` (bounded scan, like the nested protection)."""
    found = [d] if os.path.isfile(os.path.join(d, MARKER, "config.ini")) else []
    return found + [os.path.dirname(p) for p in Config(d).nested_protect_paths()
                    if os.path.basename(p) == MARKER and os.path.isfile(os.path.join(p, "config.ini"))]


def mark_write_paths(root, paths):
    """Put this project's line into the WRITE_MARKER file of each extra write path, so that no user can set up
    a project there that this sandbox could rewrite (nesting_problem). Before a folder's first marker: refuse
    if a project is already inside. Returns problems (init and start refused)."""
    me = "%s %s" % (username(), root)
    probs = []
    for w in paths:
        if not os.path.isdir(w):        # not there (yet): not bound either (policy.bwrap_args)
            continue
        m = os.path.join(w, WRITE_MARKER)
        if os.path.lexists(m) and (os.path.islink(m) or not os.path.isfile(m)):
            probs.append("%s is not a regular file: remove it" % m)
            continue
        lines = marker_lines(m)
        if me in lines:
            continue
        if not os.path.lexists(m):
            inner = projects_inside(w)
            if inner:
                probs.append("the write path %s contains the ContainedClaude project(s) %s, which this sandbox "
                             "could rewrite: remove that write path or move the project(s)" % (w, ", ".join(inner)))
                continue
        try:
            safe_write(m, MARKER_HEADER + "".join(l + "\n" for l in lines + [me]))
        except (OSError, InitError) as e:
            if not os.path.isfile(m):       # another user's marker we cannot rewrite does the job as well
                probs.append("cannot create %s (%s): it keeps other projects out of a folder this sandbox can "
                             "write to" % (m, getattr(e, "strerror", None) or e))
    return probs


def forget_old_marks(root, paths):
    """Take this project's line out of the markers of write paths it no longer has (or of all, after the
    project moved), from MARKED_FILE; delete a marker when no line is left. Then record the current ones."""
    rec = os.path.join(root, MARKED_FILE)
    try:
        with open(rec) as fh:
            old = [l.strip() for l in fh if l.strip()]
    except OSError:
        old = []
    old_root, old_paths = (old[0], old[1:]) if old else (root, [])
    me = "%s %s" % (username(), old_root)
    for w in old_paths:
        if w in paths and old_root == root:
            continue
        m = os.path.join(w, WRITE_MARKER)
        lines = marker_lines(m)
        if me not in lines or os.path.islink(m):
            continue
        rest = [l for l in lines if l != me]
        try:
            if rest:
                safe_write(m, MARKER_HEADER + "".join(l + "\n" for l in rest))
            else:
                os.unlink(m)
        except (OSError, InitError):
            pass
    if old != [root] + list(paths):
        try:
            safe_write(rec, "".join(l + "\n" for l in [root] + list(paths)))
        except (OSError, InitError):
            pass


def kerberos_problem(cfg):
    shares = kerberos_missing(cfg.write_paths())
    if not shares:
        return None
    return ("no valid Kerberos ticket (`klist`), and %s needs one (mounted with Kerberos authentication): run "
            "`kinit` or log in again, then retry" % ", ".join(shares))


def access_problem(path):
    """Why `path` cannot even be looked at (None if it can): it does not exist, or reading it is denied, which
    on the SDS usually means an expired Kerberos ticket."""
    try:
        os.stat(path)
        return None
    except FileNotFoundError:
        return "%s does not exist" % path
    except PermissionError:
        shares = kerberos_missing([path])
        return "%s: permission denied%s" % (path, "; no valid Kerberos ticket (`klist`) for %s: run `kinit` or log "
                                            "in again, then retry" % ", ".join(shares) if shares else "")
    except OSError as e:
        return "%s: %s" % (path, e.strerror or e)


def start_problems(root, cfg, check_mounts=True):
    """Everything that must be right before a project is used (run, doctor, env): writable paths safe and in
    the allowed locations, the project the user's own, not nested, its Kerberos share reachable."""
    probs = check_paths(cfg)
    probs += ["%s: %s" % b for b in placement_problems(cfg.write_paths(), check_mounts)]
    for p in (owner_problem(root), nesting_problem(root, cfg.write_paths()[1:]), kerberos_problem(cfg)):
        if p:
            probs.append(p)
    return probs


def gitignore_lines(cfg):
    """Generated or bulky files in the project that should not end up in git."""
    lines = ["/.cache/", "/CLAUDE_environment.md"]
    for key in ("executed_dir", "apptainer_cache"):
        rel = os.path.normpath(cfg.get("jobs", key)).strip("/")
        if rel and not rel.startswith("..") and not (rel + "/").startswith(".cache/"):
            lines.append("/%s/" % rel)
    return lines


def ensure_gitignore(cfg, out=sys.stdout):
    p = os.path.join(cfg.project, ".gitignore")
    text = open(p).read() if os.path.isfile(p) else ""
    have = {l.strip() for l in text.splitlines()}
    add = [l for l in gitignore_lines(cfg) if l not in have and l.lstrip("/") not in have]
    if add:
        sep = "" if not text or text.endswith("\n") else "\n"
        safe_write(p, text + sep + "# ContainedClaude (generated / caches)\n" + "\n".join(add) + "\n")
        out.write("added to %s: %s\n" % (p, " ".join(add)))


# ---- init ----------------------------------------------------------------------------------------------
def check_extra_write_paths(root, paths):
    """Validate `init --write` folders like the project itself: existing, allowed location, safe, and neither a
    project nor containing one (that project could not start any more: it would lie inside a write path).
    Returns the real paths (those inside the project dropped: already writable)."""
    out = []
    for w in paths:
        rw = os.path.realpath(os.path.expanduser(w))
        if not os.path.isdir(rw):
            raise InitError("--write %s: not an existing folder" % w)
        bad = unsafe_write_paths([rw]) + placement_problems([rw])
        if bad:
            raise InitError("--write %s is not allowed: %s" % bad[0])
        if os.path.isfile(os.path.join(rw, MARKER, "config.ini")):
            raise InitError("--write %s: this is a ContainedClaude project; give its owner's agent the folder, "
                            "not another project" % rw)
        inner = projects_inside(rw)
        if inner:
            raise InitError("--write %s contains the ContainedClaude project(s) %s, which could not start any "
                            "more (a project must not lie inside a writable folder of another one)" %
                            (rw, ", ".join(inner)))
        if rw == root or rw.startswith(root + os.sep):
            continue
        if rw not in out:
            out.append(rw)
    return out


def add_write_paths(target, paths, out=sys.stdout):
    """`containedclaude add-write DIR PATH...`: put more folders into write_paths of an existing project's
    config (same checks as `init --write`), and refresh CLAUDE_environment.md. Takes effect at the next start."""
    root = find_root(target)
    if not root:
        raise InitError("%s is not inside a ContainedClaude project" % target)
    problem = owner_problem(root)
    if problem:
        raise InitError(problem)
    cfg = Config(root)
    have = cfg.write_paths()[1:]
    extra = check_extra_write_paths(root, paths)
    new = [p for p in extra if p not in have]
    skipped = [os.path.realpath(os.path.expanduser(p)) for p in paths
               if os.path.realpath(os.path.expanduser(p)) not in new]
    if not new:
        out.write("nothing to add: %s already writable\n" % " ".join(skipped))
        return 0
    probs = mark_write_paths(root, have + new)
    if probs:
        raise InitError("; ".join(probs))
    cfg_path = os.path.join(root, MARKER, "config.ini")
    text = open(cfg_path).read()
    line = "write_paths = " + "\n    ".join(have + new)
    pat = re.compile(r"(?m)^write_paths[ \t]*=.*(?:\n[ \t]+\S.*)*")
    if pat.search(text):
        text = pat.sub(lambda m: line, text, 1)
    elif re.search(r"(?m)^\[sandbox\][ \t]*$", text):
        text = re.sub(r"(?m)^\[sandbox\][ \t]*$", lambda m: "[sandbox]\n" + line, text, 1)
    else:
        text = text.rstrip("\n") + "\n\n[sandbox]\n" + line + "\n"
    safe_write(cfg_path, text)
    forget_old_marks(root, have + new)
    write_envdoc(Config(root))
    out.write("added to write_paths of %s (from the next start):\n%s" % (root, "".join("  %s\n" % p for p in new)))
    if skipped:
        out.write("already writable: %s\n" % " ".join(skipped))
    return 0


def init(target, force=False, out=sys.stdout, write_paths=()):
    root = os.path.realpath(target)
    created = ensure_site_ini()
    if created:
        out.write("created %s from %s (cluster defaults; edit if needed)\n" % (created, SITE_DEFAULT))
    if os.path.exists(root) and not os.path.isdir(root):
        raise InitError("%s exists and is not a folder" % root)
    paths = {
        "config": os.path.join(root, MARKER, "config.ini"),
        "stub": os.path.join(root, ".contained_claude.sh"),
        "envdoc": os.path.join(root, "CLAUDE_environment.md"),
        "claude_md": os.path.join(root, "CLAUDE.md"),
        "agents_md": os.path.join(root, "AGENTS.md"),
    }
    bad = unsafe_write_paths([root])
    if bad:
        raise InitError("refusing to set up %s: %s. The project folder is writable inside the sandbox, so it must "
                        "not contain your home folder, ~/.config, ~/.local/bin, the ContainedClaude clone or a "
                        "hidden credential folder; use a sub-folder" % bad[0])
    bad = placement_problems([root])
    if bad:
        raise InitError("refusing to set up %s: %s. Projects may only lie below the locations in allowed_roots "
                        "(%s/site.ini)" % (bad[0][0], bad[0][1], CLONE))
    who = recorded_owner(root)              # init may (re)claim a folder without a record, never another user's
    if who and who != username():
        raise InitError("refusing to set up: %s" % foreign_owner_message(root, who))
    problem = nesting_problem(root, new=True)
    if problem:
        raise InitError("refusing to set up: %s" % problem)
    extra = check_extra_write_paths(root, write_paths)
    if os.path.isdir(root):
        inner = [p for p in projects_inside(root) if p != root]
        if inner:
            raise InitError("refusing to set up %s: it contains the ContainedClaude project(s) %s, whose config "
                            "and starter script would be writable from this sandbox (nested projects are "
                            "refused)" % (root, ", ".join(inner)))
    conflicts = []
    for p in (os.path.join(root, MARKER), paths["stub"], paths["envdoc"], os.path.join(root, ".claude"),
              paths["claude_md"], paths["agents_md"]):
        if os.path.lexists(p):
            conflicts.append(p)
    if conflicts and not force:
        out.write("Not changing anything: %s already has a Claude/ContainedClaude setup or files that would be\n"
                  "overwritten or modified:\n" % root)
        for c in conflicts:
            out.write("  %s\n" % c)
        out.write("Run again with --force to set it up anyway (changed files are backed up to "
                  "%s/backup-<date>/; .claude/ itself is never modified).\n" % os.path.join(root, MARKER))
        return 1

    probs = mark_write_paths(root, extra)
    if probs:
        raise InitError("refusing to set up %s: %s" % (root, "; ".join(probs)))
    os.makedirs(os.path.join(root, MARKER), exist_ok=True)
    for sub in ("logs", "run", "state"):
        os.makedirs(os.path.join(root, MARKER, sub), exist_ok=True)
    if conflicts:
        backup = os.path.join(root, MARKER, "backup-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup)
        for key in ("config", "stub", "envdoc", "claude_md", "agents_md"):
            p = paths[key]
            if os.path.isfile(p) and not os.path.islink(p):
                shutil.copy2(p, os.path.join(backup, os.path.basename(p)))
        out.write("backed up existing files to %s\n" % backup)

    values = {"clone": CLONE, "project": root, "version": __version__}
    config_text = render("config.ini", values)
    if extra:
        config_text = re.sub(r"(?m)^write_paths =[ \t]*$", lambda m: "write_paths = " + "\n    ".join(extra),
                             config_text, 1)
    safe_write(paths["config"], config_text)
    write_owner(root)
    forget_old_marks(root, extra)
    safe_write(paths["stub"], render("contained_claude.sh", values), 0o755)
    safe_write(os.path.join(root, MARKER, ".gitignore"), "logs/\nrun/\nstate/\nbackup-*/\n")
    safe_write(os.path.join(root, MARKER, "VERSION"), "containedclaude %s\nclone %s\ncommit %s\n" % (
        __version__, CLONE, git_commit()))

    ensure_import_line(root)
    p = paths["agents_md"]
    text = open(p).read() if os.path.isfile(p) else ""
    if AGENTS_LINE not in text:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        safe_write(p, text + sep + AGENTS_LINE + "\n")
    cfg = Config(root)
    write_envdoc(cfg)
    ensure_gitignore(cfg, out)
    ensure_state()
    out.write("Set up %s\n  start Claude with:  %s   (from anywhere)\n  edit limits/paths:  %s\n" % (
        root, paths["stub"], paths["config"]))
    if extra:
        out.write("  also writable:      %s\n" % "  ".join(extra))
    return 0


def ensure_import_line(root, out=sys.stderr):
    """Make `@CLAUDE_environment.md` the FIRST line of <root>/CLAUDE.md (created if missing). CLAUDE.md is
    writable inside the sandbox and gets rewritten by agents (`/init`), so this runs at every start too."""
    p = os.path.join(root, "CLAUDE.md")
    text = open(p).read() if os.path.isfile(p) else ""
    lines = text.splitlines()
    if lines and lines[0].strip() == IMPORT_LINE:
        return False
    rest = [l for l in lines if l.strip() != IMPORT_LINE]
    if rest and rest[0].strip():
        rest.insert(0, "")
    try:
        safe_write(p, "\n".join([IMPORT_LINE] + rest) + "\n")
    except (OSError, InitError) as e:
        out.write("containedclaude: warning: could not put %s into %s: %s\n" % (IMPORT_LINE, p, e))
        return False
    if text:
        out.write("containedclaude: restored `%s` as the first line of %s\n" % (IMPORT_LINE, p))
    return True


def tracked_protected_note(cfg, out=sys.stderr):
    """Warn when a protected file the user tracks in git differs between HEAD and another local branch: git
    cannot rewrite it inside the sandbox, so such a branch switch or merge leaves it as it is."""
    git = compat.tool("git")
    top = os.path.join(cfg.project, ".git")
    if not git or not os.path.isdir(top):
        return
    own = {".contained", ".contained_claude.sh", "CLAUDE_environment.md"}
    rel = [os.path.relpath(p, cfg.project) for p in cfg.protect_paths()]
    rel = [r for r in rel if r.split(os.sep)[0] not in own and not r.startswith(".git" + os.sep)]
    if not rel:
        return
    run = lambda *a: subprocess.run([git, "-C", cfg.project] + list(a), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout
    try:
        tracked = [l for l in run("ls-files", "--", *rel).splitlines() if l]
        if not tracked:
            return
        branches = [b for b in run("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines() if b][:30]
        differ = set()
        for b in branches:
            differ.update(l for l in run("diff", "--name-only", "HEAD", b, "--", *rel).splitlines() if l)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return
    if differ:
        out.write("containedclaude: note: %s differ(s) between HEAD and another branch and is read-only inside the "
                  "sandbox; a checkout or merge there leaves it unchanged (see EXPLANATIONS.md)\n" %
                  ", ".join(sorted(differ)))


def git_commit():
    try:
        return subprocess.run([compat.tool("git") or "git", "-C", CLONE, "rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout.strip() or "?"
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def refresh_stub(cfg, out=sys.stderr):
    """Rewrite an outdated .contained_claude.sh: it runs outside the sandbox, so fixes to it (like not using
    programs from PATH) must reach projects set up before. A missing one is not recreated."""
    p = os.path.join(cfg.project, ".contained_claude.sh")
    text = render("contained_claude.sh", {"clone": CLONE, "project": cfg.project, "version": __version__})
    try:
        with open(p) as fh:
            if fh.read() == text:
                return
    except OSError:
        return
    try:
        safe_write(p, text, 0o755)
        out.write("containedclaude: updated %s to the current version\n" % p)
    except (OSError, InitError) as e:
        out.write("containedclaude: warning: could not update %s: %s\n" % (p, e))


def write_envdoc(cfg):
    """The sandbox description goes to .contained/run/ (policy.envdoc_inside), which every sandbox mounts over
    the project's CLAUDE_environment.md; on disk that file is a notice for agents started without
    ContainedClaude. So an agent reads that it is sandboxed only when it is. Each file is written only when it
    changed: rewriting the notice at another start would show it to the project's running sessions. Returns the
    paths written."""
    g = lambda k: cfg.get("jobs", k)
    bullets = lambda xs: "\n".join("- `%s`" % x for x in xs) or "- (none)"
    values = {
        "project": cfg.project, "clone": CLONE, "version": __version__,
        "write_paths": bullets(cfg.write_paths()),
        "protect_paths": bullets(os.path.relpath(p, cfg.project) for p in cfg.protect_paths()),
        "image_binds": bullets(cfg.image_binds()),
        # the configured patterns, not their expansion: `/mnt/sds-hd/*/p/*` alone is dozens of folders
        "automount": bullets(cfg.list("sandbox", "automount")),
        "cpus": g("cpus"), "mem": g("mem"), "time": g("time"), "default_image": g("default_image"),
        "max_cpus": g("max_cpus"), "max_mem": g("max_mem"), "max_time": g("max_time"),
        "max_jobs": g("max_jobs"), "max_gpu_jobs": g("max_gpu_jobs"), "gpu_gres": g("gpu_gres"),
        "max_total_cpus": g("max_total_cpus") or "(no limit)", "max_total_mem": g("max_total_mem") or "(no limit)",
        "partition": g("partition") or "(cluster default)", "executed_dir": g("executed_dir"),
        "apptainer_cache": g("apptainer_cache"), "claude_config": CLAUDE_CONFIG_DIR,
        "tmp_size": cfg.get("sandbox", "tmp_size") or "no limit",
        "session_mem": cfg.get("sandbox", "session_mem") or "no limit",
        "session_cpus": policy.session_int(cfg, "session_cpus") or "all",
        "session_tasks": policy.session_int(cfg, "session_tasks") or "unlimited",
    }
    inside = policy.envdoc_inside(cfg)
    os.makedirs(os.path.dirname(inside), exist_ok=True)
    notice = os.path.join(cfg.project, policy.ENVDOC)
    pairs = [(inside, "CLAUDE_environment.md"), (notice, "CLAUDE_environment_outside.md")]
    return [p for p, tpl in pairs if write_changed(p, render(tpl, values))]


# ---- files that run code OUTSIDE the sandbox later: report changes after a session --------------------
def sensitive_snapshot(cfg):
    """Content hashes (not mtimes: NFS attribute caching) of the protected paths and git hooks. Leaves out
    what the launcher/broker write themselves (.contained/, CLAUDE_environment.md)."""
    import hashlib
    cfg._scanned = None             # look again: new nested repos/settings created during the session count too
    skip = {os.path.join(cfg.project, MARKER), os.path.join(cfg.project, "CLAUDE_environment.md")}
    skip |= {os.path.join(w, WRITE_MARKER) for w in cfg.write_paths()[1:]}     # other projects' launchers add lines
    paths = [p for p in cfg.protect_paths() if p not in skip] + cfg.watch_paths()
    for g in cfg.git_dirs():        # incl. submodule git dirs (.git/modules/*)
        hooks = os.path.join(g, "hooks")
        if os.path.isdir(hooks) and not os.path.islink(hooks):
            paths += [os.path.join(hooks, f) for f in os.listdir(hooks)]
    # a protected path that is a symlink: its target (inside a writable area) counts too
    roots = [r.rstrip(os.sep) + os.sep for r in cfg.write_paths()]
    for p in list(paths):
        if os.path.islink(p):
            rp = os.path.realpath(p)
            if rp not in paths and any(rp.startswith(r) for r in roots):
                paths.append(rp)
    snap = {}
    for p in paths:
        if p.endswith(".sample") and os.path.basename(os.path.dirname(p)) == "hooks":
            continue                    # git never runs *.sample hooks
        try:
            if not os.path.lexists(p):
                snap[p] = None          # absent (watch paths may not exist; same as a missing key)
            elif os.path.islink(p):
                snap[p] = "link:" + os.readlink(p)
            elif os.path.isdir(p):
                snap[p] = "dir:" + ",".join(sorted(os.listdir(p)))
            elif os.path.isfile(p):
                with open(p, "rb") as fh:
                    snap[p] = hashlib.sha256(fh.read(50 << 20)).hexdigest() + ":%o" % os.stat(p).st_mode
            else:
                snap[p] = "other"
        except OSError:
            snap[p] = None
    return snap


def report_changes(cfg, before, after, out=sys.stderr):
    changed = [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
    if not changed:
        return
    text = ("containedclaude: these files can run code outside the sandbox (git hooks, editor/Claude settings)\n"
            "and were created or changed during the session; review them before using them unsandboxed:\n" +
            "".join("  %s\n" % p for p in changed))
    # also kept as a file: after a dropped ssh connection there is no terminal to print to
    report = os.path.join(cfg.contained_dir, "state", "changes-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(os.path.dirname(report), exist_ok=True)
        safe_write(report, text)
        text += "(saved in %s)\n" % report
    except (OSError, InitError):
        pass
    try:
        out.write("\n" + text)
        out.flush()
    except OSError:
        pass


# ---- launcher ------------------------------------------------------------------------------------------
def claude_settings(cfg):
    py = sys.executable or "python3"
    hook = lambda name: os.path.join(CLONE, "sandbox", "hooks", name)
    cmd = lambda name: [{"type": "command", "command": "%s -I %s" % (py, hook(name)), "timeout": 10}]
    return {
        "hooks": {
            "PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": cmd("write_guard.py")},
                           # messages only to the sessions of this project and the session's own agents
                           {"matcher": "SendMessage", "hooks": cmd("peer_guard.py")}],
            # and only from the sessions of this project
            "UserPromptSubmit": [{"hooks": cmd("peer_guard.py")}],
            # after a command: tell the agent when a process was killed by the session memory limit
            "PostToolUse": [{"matcher": "Bash", "hooks": cmd("oom_notice.py")},
                            # after any tool, in the session and its subagents: close to auto-compaction
                            {"matcher": "*", "hooks": cmd("context_notice.py")},
                            # which of the listed sessions this one can message
                            {"matcher": "ListAgents", "hooks": cmd("peer_guard.py")}]},
        # routines run in the cloud with the account's connectors, outside the sandbox
        "permissions": {"deny": ["RemoteTrigger"]}}


def claude_env(cfg, user):
    """The [claude] values for every Claude process of the sandbox, through the settings' env block (also the
    sessions a Remote Control server starts): the compaction notice hook's margin and offer, the cc mod (worker
    agent types, compaction on request, waits on jobs) with Bash's longest foreground command at 57 minutes (for
    those waits, and below the 1-hour prompt cache) (DEVELOPMENT.md §5.13), and, with idle_compact, the idle-compact mod and its values (mods through
    CLAUDE_CODE_PLUGIN_DIRS, after any folders of the user's own)."""
    own = (user.get("env") or {}).get("CLAUDE_CODE_PLUGIN_DIRS") if isinstance(user.get("env"), dict) else None
    mods = [os.path.join(CLONE, "sandbox", "mods", "cc")]
    env = {"CONTAINEDCLAUDE_COMPACT_NOTICE": cfg.get("claude", "compact_notice") or "0",
           "CONTAINEDCLAUDE_COMPACT_OFFER": cfg.get("claude", "compact_offer") or "0",
           "BASH_MAX_TIMEOUT_MS": "3420000"}
    if cfg.bool("claude", "idle_compact"):
        mods.append(os.path.join(CLONE, "sandbox", "mods", "idle-compact"))
        env.update(CONTAINEDCLAUDE_IDLE_COMPACT_AFTER=cfg.get("claude", "idle_compact_after"),
                   CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS=cfg.get("claude", "idle_compact_min_tokens"))
    env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(([own] if own else []) + mods)
    return env


def session_settings(cfg, user):
    """The contained config's settings (`user`, parsed) with claude_settings added, plus no MCP servers unless
    mcp_from_user_config: what Claude reads as that settings.json in the project's sandbox. Every Claude
    process there gets them, also the sessions `claude remote-control` starts, which get none of the
    launcher's flags. The user's own entries stay. ValueError if hooks or permissions have a form Claude Code
    does not read."""
    if not isinstance(user, dict):
        raise ValueError("it is not a JSON object")
    hooks, perms = user.get("hooks", {}), user.get("permissions", {})
    if not (isinstance(hooks, dict) and all(isinstance(v, list) for v in hooks.values()) and isinstance(perms, dict)
            and isinstance(perms.get("deny", []), list)):
        raise ValueError("its hooks or permissions have a form Claude Code does not read")
    ours, out = claude_settings(cfg), dict(user)
    out["hooks"] = dict(hooks, **{e: hooks.get(e, []) + h for e, h in ours["hooks"].items()})
    deny = perms.get("deny", [])
    out["permissions"] = dict(perms, deny=deny + [r for r in ours["permissions"]["deny"] if r not in deny])
    out["env"] = dict(user.get("env") if isinstance(user.get("env"), dict) else {}, **claude_env(cfg, user))
    if not cfg.bool("sandbox", "mcp_from_user_config"):
        out["allowedMcpServers"] = []       # no MCP servers (with --strict-mcp-config, where it can be passed)
        out["disableClaudeAiConnectors"] = True     # claude.ai connectors not even loaded: blocked ones are
        # warned about on stderr, and a Remote Control server shows that warning in place of a session's error
    return out


def write_session_settings(cfg):
    """Write session_settings to policy.settings_inside (only when it changed; the sandbox binds it over the
    contained config's settings.json). Returns a problem, or None."""
    path = os.path.join(CLAUDE_CONFIG_DIR, "settings.json")
    try:
        with open(path) as fh:
            settings = session_settings(cfg, json.load(fh))
    except (OSError, ValueError) as e:
        return "the contained config's %s cannot be used (%s): fix it with `containedclaude admin`" % (path, e)
    inside = policy.settings_inside(cfg)
    os.makedirs(os.path.dirname(inside), exist_ok=True)
    write_changed(inside, json.dumps(settings, indent=2) + "\n", 0o600)
    return None


def run(project, claude_args, mode="session", out=sys.stderr, command=None):
    """Start Claude (or `command`, e.g. bash for `containedclaude shell`) in the sandbox of a project."""
    problem = access_problem(project)
    if problem:
        out.write("containedclaude: cannot start: %s\n" % problem)
        return 1
    root = find_root(project) if mode == "session" else project
    if not root:
        out.write("containedclaude: %s is not inside a project set up with `containedclaude init` "
                  "(no .contained/config.ini found)\n" % project)
        return 1
    cfg = Config(root)
    # admin: the fixed admin folder and its write path (the contained config) are made by the launcher itself
    with Slow(out, "checking the project and its file systems"):
        problems = start_problems(root, cfg) if mode == "session" else []
    if problems:
        out.write("containedclaude: cannot start: %s\n" % "; ".join(problems))
        return 1
    if mode == "session":
        with Slow(out, "marking the extra write paths"):
            problems = mark_write_paths(root, cfg.write_paths()[1:])
        if problems:
            out.write("containedclaude: cannot start: %s\n" % "; ".join(problems))
            return 1
        forget_old_marks(root, cfg.write_paths()[1:])
    # host tools only from folders no sandbox can write to (see compat.harden_host_env)
    if mode == "session":
        compat.register(cfg.write_paths())
    compat.harden_host_env(cfg.write_paths())
    problems = policy.check_host()
    if problems:
        out.write("containedclaude: cannot start: %s\n" % "; ".join(problems))
        return 1
    claude = compat.tool("claude")
    if not claude and not command:
        out.write("containedclaude: `claude` not found on PATH (programs inside ContainedClaude projects or "
                  "write_paths are never used by the launcher)\n")
        return 1
    ensure_state()
    codex_trust(cfg)
    rundir = os.path.join(cfg.contained_dir, "run")
    os.makedirs(rundir, exist_ok=True)
    if mode == "session":
        problem = write_session_settings(cfg)
        if problem:
            out.write("containedclaude: cannot start: %s\n" % problem)
            return 1
        write_envdoc(cfg)
        refresh_stub(cfg, out)
        ensure_import_line(root, out)
        tracked_protected_note(cfg, out)
        from . import updates
        updates.notice(out)             # once a day: installed vs latest Claude Code / Codex
        if not command:                 # a real Claude session (not shell/test): quick doctor if a binary changed
            updates.check_after_change(root, out)
    else:                   # admin: its settings.json is the real one, editable; the launcher's come by flag
        settings_path = os.path.join(rundir, "settings.json")
        safe_write(settings_path, json.dumps(claude_settings(cfg), indent=2) + "\n")
    mcp_path = os.path.join(rundir, "mcp.json")
    safe_write(mcp_path, json.dumps({"mcpServers": {}}) + "\n")

    say = lambda m: (out.write("containedclaude: %s\n" % m), out.flush())
    missing = [m for m in policy.automount(cfg.automount(), report=say) if m in cfg.automount_explicit()]
    if missing:
        out.write("containedclaude: not mounted, invisible in the sandbox: %s\n" % " ".join(missing))
    try:
        runtime = policy.project_runtime(cfg.project)   # shared by the sessions of this project (Claude messaging)
        store = policy.project_store(cfg, runtime,      # transcripts, memory, snapshots, Codex rollouts: per project
                                     "session" if mode == "session" else "admin")
    except OSError as e:
        out.write("containedclaude: cannot start: %s\n" % e)
        return 1
    sock_dir = tempfile.mkdtemp(prefix="containedclaude-%d-" % os.getuid(), dir="/tmp")
    os.chmod(sock_dir, 0o700)
    srv = broker.serve(cfg, os.path.join(sock_dir, "broker.sock"))
    with Slow(out, "reading the project's protected files"):
        before = sensitive_snapshot(cfg)
    sec = policy.seccomp_fd()
    scope, scope_env = policy.session_scope(cfg)       # memory limit for the whole session (cgroup)
    cmd = policy.session_command(cfg, "session" if mode == "session" else "admin", sock_dir, runtime, store,
                                 seccomp=sec, scope=scope)
    if command:
        cmd += ["--"] + list(command)
    else:
        cmd += ["--", os.path.realpath(claude)]
        # flags detected from `claude --help` so a Claude Code update that renames one doesn't stop the start.
        # Project sessions get the launcher's settings from the config's settings.json (write_session_settings)
        if mode != "session" and compat.likely("claude", "--settings"):
            cmd += ["--settings", settings_path]
        elif mode != "session":
            compat.warn_once("claude lacks --settings: the write hook is not installed (the sandbox still applies)")
        # the server for sessions started from claude.ai: Claude Code refuses to start it behind the MCP flags,
        # and the sessions it starts get no flags anyway (they have the settings' allowedMcpServers)
        server = mode == "session" and list(claude_args[:1]) == ["remote-control"]
        if not server and not cfg.bool("sandbox", "mcp_from_user_config"):
            if compat.likely("claude", "--strict-mcp-config") and compat.likely("claude", "--mcp-config"):
                # --mcp-config takes several files: the boolean flag after it ends the list, so that a
                # subcommand following the launcher's flags (`plugin install ...`) is not read as a file name
                cmd += ["--mcp-config", mcp_path, "--strict-mcp-config"]
            else:
                compat.warn_once("claude lacks --strict-mcp-config/--mcp-config: MCP servers from the contained "
                                 "config are NOT ignored (they still run inside the sandbox)")
        cmd += list(claude_args)
    env = policy.sandbox_env(cfg, "session" if mode == "session" else "admin")
    env.update(scope_env)
    proc = None

    def forward(signum, frame):     # hang-up (ssh dropped) / kill: end the sandbox, then clean up and report
        if proc is not None:
            try:
                proc.send_signal(signum)
            except OSError:
                pass
    old = {sig: signal.signal(sig, h) for sig, h in ((signal.SIGINT, signal.SIG_IGN), (signal.SIGHUP, forward),
                                                     (signal.SIGTERM, forward))}
    try:
        proc = subprocess.Popen(cmd, env=env, pass_fds=(sec,) if sec is not None else ())
        rc = proc.wait()
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(sock_dir, ignore_errors=True)
        if sec is not None:
            os.close(sec)
    report_changes(cfg, before, sensitive_snapshot(cfg), out)
    return rc


ADMIN_DIR = os.path.join(STATE, "admin")


def trust_folder(path):
    """Mark `path` as trusted in the contained config's .claude.json: what accepting Claude's trust dialog
    records, so the fixed admin folder is never asked about. Only when the file exists and parses (Claude
    creates it at its first start; until then the dialog is simply asked once)."""
    cfg = os.path.join(CLAUDE_CONFIG_DIR, ".claude.json")
    try:
        with open(cfg) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    entry = data.setdefault("projects", {}).setdefault(path, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    safe_write(cfg, json.dumps(data, indent=2) + "\n", 0o600)


def admin(claude_args, out=sys.stderr, command=None):
    """Claude (or `command`, e.g. codex) with the contained config folder and Codex home fully writable (to
    /login, install plugins, change settings). Still sandboxed; its 'project' is a fixed, otherwise empty folder
    in STATE, marked as trusted."""
    ensure_state()
    os.makedirs(os.path.join(ADMIN_DIR, MARKER), exist_ok=True)
    with open(os.path.join(ADMIN_DIR, MARKER, "config.ini"), "w") as fh:
        fh.write("[sandbox]\nwrite_paths = %s\n" % CLAUDE_CONFIG_DIR)
    trust_folder(ADMIN_DIR)
    return run(ADMIN_DIR, claude_args, mode="admin", out=out, command=command)
