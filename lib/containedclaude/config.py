"""Configuration: built-in defaults < site.ini (in the clone) < <project>/.contained/config.ini.

Python 3.9 compatible, standard library only (runs on login and compute nodes with the system python).
"""
import configparser
import glob
import os
import re
import time

from . import CLONE

HOME = os.path.expanduser("~")
UID = os.getuid()
DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
STATE = os.path.join(DATA_HOME, "containedclaude")
CLAUDE_CONFIG_DIR = os.path.join(STATE, "claude-config")
CODEX_HOME = os.path.join(STATE, "codex-home")
MARKER = ".contained"            # <project>/.contained/ (read-only inside the sandbox)
WRITE_MARKER = ".contained_writable"    # in each extra write path: no project may be set up in or below it

# Below ~/.local/share, ~/.config and ~/.cache, tools follow the XDG variable when it is set: such paths are
# hidden at the default place and where the variable points (an older copy may remain at the default place).
XDG_HOMES = (("~/.local/share/", "XDG_DATA_HOME"), ("~/.config/", "XDG_CONFIG_HOME"),
             ("~/.cache/", "XDG_CACHE_HOME"))


def with_xdg(items):
    out = list(items)
    for item in items:
        for prefix, var in XDG_HOMES:
            base = os.environ.get(var)
            if base and item.startswith(prefix) and os.path.normpath(base) != os.path.expanduser(prefix[:-1]):
                alt = os.path.join(base, item[len(prefix):])
                if alt not in out:
                    out.append(alt)
    return out


# Paths hidden inside every sandbox (credentials, agent/IPC sockets, other agents' configs, shell histories).
# Directories become empty, files become /dev/null. Globs and ~ are allowed.
DEFAULT_HIDE = with_xdg("""
~/.ssh ~/.gnupg ~/.claude ~/.claude.json ~/.codex ~/.copilot ~/.cursor-server ~/.vscode-server ~/.vscode
~/.config/gh ~/.config/hub ~/.config/gcloud ~/.config/rclone ~/.config/git/credentials ~/.git-credentials
~/.git-credential-cache ~/.netrc ~/.pypirc ~/.docker ~/.aws ~/.azure ~/.kube ~/.apptainer ~/.singularity
~/.local/share/jupyter/runtime ~/.local/share/keyrings ~/.cache/keyring-* ~/.krb5cc* ~/.vnc ~/.Xauthority
~/.ICEauthority ~/.bash_history ~/.zsh_history ~/.python_history ~/.sh_history ~/.Rhistory ~/.lesshst ~/.viminfo
~/.local/share/containedclaude
/run/user/{uid} /run/munge /run/slurm /var/run/munge /run/screen /run/tmux /run/dbus
""".split())

# Re-exposed read-only after hiding (program files living inside hidden folders).
DEFAULT_EXPOSE = """
~/.codex/packages
""".split()

# Inside the project these stay read-only: they run code or define the sandbox outside of it later.
DEFAULT_PROTECT = """
.contained .contained_claude.sh CLAUDE_environment.md .git/hooks .git/config .git/info
.claude/settings.json .claude/settings.local.json .claude/hooks .codex .mcp.json .envrc .vscode .idea
""".split()

# The same kinds of files in subfolders (nested git repos, a sub-folder opened in an editor or Claude, another
# ContainedClaude project inside this one, whose config and starter script run outside): found at start (up to
# NESTED_DEPTH folders deep, within NESTED_BUDGET seconds) and kept read-only too.
NESTED_PROTECT = """
.git/config .git/hooks .git/info .claude/settings.json .claude/settings.local.json .claude/hooks .codex .mcp.json
.envrc .vscode .idea .contained .contained_claude.sh
""".split()
NESTED_DEPTH = 3
NESTED_BUDGET = 3.0
NESTED_SKIP = {".git", ".cache", ".contained", "node_modules", "__pycache__", ".snakemake", ".venv", "venv", "envs",
               ".ipynb_checkpoints"}
# Submodules keep their git dir in <gitdir>/modules/<name>/ (their own .git is only a file pointing there, so the
# walk above never sees it): these entries of each submodule git dir are read-only too.
GITDIR_PROTECT = ["config", "hooks", "info"]
SUBMODULE_DEPTH = 8                  # folder levels below <gitdir>/modules (names may contain "/"; nesting)
# Only compared before/after a session (git needs them absent or valid, so they can't be bound read-only). Checked
# in every git dir and in its linked worktrees (<gitdir>/worktrees/*).
GITDIR_WATCH = ["commondir", "config.worktree"]

DEFAULTS = {
    "sandbox": {
        "write_paths": "",
        "hide_paths": "",
        "expose_paths": "",
        "protect_paths": "",
        "automount": "~",
        "mcp_from_user_config": "no",
        "codex": "yes",
        "tmp_size": "32G",
        "session_mem": "32G",
        "session_cpus": "8",             # cores of the login node a session may use (affinity + nice, or CPUQuota)
        "session_tasks": "300",          # processes + threads of a session together (TasksMax)
        "allowed_roots": "~",            # site.ini only (never a project's config): see allowed_roots()
    },
    "jobs": {
        "default_image": "none",
        "cpus": "4",
        "mem": "16G",
        "time": "04:00:00",
        "partition": "",
        "gpu_gres": "gpu:1",
        "extra_sbatch": "",
        "max_cpus": "64",
        "max_mem": "500G",
        "max_time": "7-00:00:00",
        "max_jobs": "1000",
        "max_gpu_jobs": "2",
        "max_array": "1000",
        "max_total_cpus": "",
        "max_total_mem": "",
        "image_binds": "~",
        "executed_dir": "jobs/executed",
        "apptainer_cache": ".cache/apptainer",
    },
    "claude": {
        "compact_notice": "25000",           # tokens before auto-compaction to tell the agent; 0 = never
        "compact_offer": "180000",           # main session: past this, it may ask for compaction; 0 = never
        "idle_compact": "yes",               # notes + compaction before the prompt cache expires (sandbox/mods)
        "idle_compact_after": "50",          # minutes idle (the cache lasts 60)
        "idle_compact_min_tokens": "200000",
    },
}


class ConfigError(Exception):
    pass


SITE_DEFAULT = "site.curry.ini"     # copied to site.ini by install.sh / init if site.ini is missing


def ensure_site_ini():
    """Create <clone>/site.ini from the default site template if missing; returns the path if it was created."""
    site, tmpl = os.path.join(CLONE, "site.ini"), os.path.join(CLONE, SITE_DEFAULT)
    if os.path.exists(site) or not os.path.isfile(tmpl):
        return None
    import shutil
    shutil.copyfile(tmpl, site)
    return site


# Folders a writable path must never be or contain: code or settings there runs OUTSIDE the sandbox later
# (shell startup files in ~, ~/.config/git/config or $XDG_CONFIG_HOME/git/config, the launcher and jobwrap.py in
# the clone, ~/.local/bin, the contained state with the shared Claude config), plus everything the sandbox hides
# (a writable bind of a folder holding a hidden path would cover the hiding).
def _critical():
    fixed = [HOME, CLONE, STATE, os.path.join(HOME, ".local", "bin"), os.path.join(HOME, ".config"), CONFIG_HOME,
             os.path.join(HOME, ".ssh")]
    hidden = [p for p in expand_globs(DEFAULT_HIDE) if os.path.lexists(p)]
    out = []
    for p in fixed + hidden:
        rp = os.path.realpath(p)
        if rp not in out:
            out.append(rp)
    return out


def unsafe_write_paths(paths):
    """[(path, reason)] for writable paths that would let the sandbox change what runs outside it."""
    bad, home = [], os.path.realpath(HOME)
    under = lambda a, b: a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    for p in paths:
        p = os.path.realpath(p)
        for c in _critical():
            if under(c, p):
                bad.append((p, "it is or contains %s" % c))
                break
            if c != home and under(p, c):           # inside ~ is fine, inside the others is not
                bad.append((p, "it is inside %s" % c))
                break
    return bad


# ---- where projects may live (site policy) ------------------------------------------------------------
def site_config():
    """Defaults + site.ini only, without any project's config."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    cp.read(os.path.join(CLONE, "site.ini"))
    return cp


def allowed_roots():
    """[sandbox] allowed_roots from site.ini: projects and write_paths must lie STRICTLY below one of them
    (real paths; `~` is the user's own home). Site-wide on purpose: a project's own config cannot widen it.
    Empty = no restriction."""
    return [os.path.realpath(p) for p in expand_globs(_list(site_config().get("sandbox", "allowed_roots",
                                                                               fallback="")))]


def mounts(text=None):
    """[(mount point, fstype)] from /proc/self/mountinfo (later lines are mounted on top of earlier ones)."""
    if text is None:
        try:
            with open("/proc/self/mountinfo") as fh:
                text = fh.read()
        except OSError:
            return []
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 7 or "-" not in f[6:]:
            continue
        mp = f[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
        out.append((mp, f[f.index("-", 6) + 1]))
    return out


def krb5_shares(paths, text=None):
    """Mount points with Kerberos authentication (NFS sec=krb5*: the SDS) that any of `paths` lies on. Such a
    share refuses everything once the user's ticket has expired."""
    if text is None:
        try:
            with open("/proc/self/mountinfo") as fh:
                text = fh.read()
        except OSError:
            return []
    krb = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 10 or "-" not in f[6:]:
            continue
        if "sec=krb5" in f[-1]:                  # super options, e.g. ...,sec=krb5p,...
            krb.append(f[4].replace("\\040", " "))
    out = []
    for mp in krb:
        if any(p == mp or p.startswith(mp.rstrip(os.sep) + os.sep) for p in paths) and mp not in out:
            out.append(mp)
    return out


def _containing_mount(path, ms):
    best = None
    for mp, t in ms:
        if path == mp or path.startswith(mp.rstrip(os.sep) + os.sep) or mp == os.sep:
            if best is None or len(mp) >= len(best[0]):
                best = (mp, t)
    return best


def placement_problems(paths, check_mounts=True, ms=None):
    """[(real path, reason)] for writable paths outside the site's allowed locations: each must lie strictly
    below an allowed root and on the SAME mount as that root (not on something mounted inside it, e.g. a user's
    FUSE/sshfs mount). Mounts INSIDE a writable path are not refused: the policy binds them read-only
    (inner_mounts)."""
    roots, bad = allowed_roots(), []
    if ms is None and check_mounts:
        ms = mounts()
    for p in paths:
        rp = os.path.realpath(p)
        root = next((r for r in roots if rp.startswith(r.rstrip(os.sep) + os.sep)), None)
        if roots and root is None:
            bad.append((rp, "not below one of the allowed locations %s (allowed_roots in site.ini)" %
                        " ".join(roots)))
            continue
        if ms and root is not None:
            a, b = _containing_mount(rp, ms), _containing_mount(root, ms)
            if a != b:
                bad.append((rp, "on a different file system (%s, %s) than %s (%s); only the cluster's own "
                                "file systems are allowed, not something mounted inside them" %
                            (a[0], a[1], root, b[0] if b else "?")))
    return bad


def inner_mounts(paths, ms=None):
    """Mount points strictly inside the given (writable) paths, on this host: the sandbox makes them
    read-only, so a share or FUSE mount buried in a project can't be written through the project."""
    if ms is None:
        ms = mounts()
    out = []
    for p in paths:
        rp = os.path.realpath(p).rstrip(os.sep) + os.sep
        for mp, t in ms:
            if mp.startswith(rp) and mp not in out:
                out.append(mp)
    return sorted(out, key=lambda m: m.count(os.sep))


def _list(value):
    return [x for x in re.split(r"[\s,]+", value or "") if x]


def expand(p, project=None):
    p = p.replace("{uid}", str(UID)).replace("{home}", HOME)
    if project:
        p = p.replace("{project}", project)
    return os.path.expanduser(p)


def expand_globs(items, project=None):
    out = []
    for item in items:
        p = expand(item, project)
        hits = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
        out.extend(hits)
    return out


class Config:
    def __init__(self, project):
        self.project = os.path.realpath(project)
        self.cp = configparser.ConfigParser(interpolation=None)
        self.cp.read_dict(DEFAULTS)
        self.sources = []
        for f in (os.path.join(CLONE, "site.ini"), os.path.join(self.project, MARKER, "config.ini")):
            if os.path.isfile(f):
                self.cp.read(f)
                self.sources.append(f)

    def get(self, section, key):
        return self.cp.get(section, key, fallback="").strip()

    def list(self, section, key):
        """Paths and names: split on white space and commas."""
        return _list(self.get(section, key))

    def lines(self, section, key):
        """One entry per line (values that contain commas or spaces, such as Slurm options)."""
        return [l.strip() for l in self.get(section, key).splitlines() if l.strip()]

    def bool(self, section, key):
        return self.get(section, key).lower() in ("1", "yes", "true", "on")

    # ---- derived ---------------------------------------------------------------------------------------
    @property
    def contained_dir(self):
        return os.path.join(self.project, MARKER)

    def write_paths(self):
        extra = [os.path.realpath(p) for p in expand_globs(self.list("sandbox", "write_paths"), self.project)]
        return [self.project] + [p for p in extra if p != self.project]

    def hide_paths(self):
        return expand_globs(DEFAULT_HIDE + self.list("sandbox", "hide_paths"), self.project)

    def expose_paths(self):
        return expand_globs(DEFAULT_EXPOSE + self.list("sandbox", "expose_paths"), self.project)

    def protect_paths(self):
        rel = DEFAULT_PROTECT + self.list("sandbox", "protect_paths")
        out = []
        for r in rel:
            p = os.path.normpath(os.path.join(self.project, expand(r, self.project)))
            out.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])
        out += [os.path.join(w, WRITE_MARKER) for w in self.write_paths()[1:]]
        return out + [p for p in self.nested_protect_paths() if p not in out]

    def nested_protect_paths(self):
        """Existing NESTED_PROTECT paths in subfolders and GITDIR_PROTECT paths of submodules."""
        return self._scan()[0]

    def git_dirs(self):
        """The project's .git, nested repositories' .git folders and submodule git dirs (no symlinks)."""
        return self._scan()[1]

    def watch_paths(self):
        """GITDIR_WATCH files of every git dir and its linked worktrees (existing or not): compared only."""
        out = []
        for g in self.git_dirs():
            for d in [g] + sorted(glob.glob(os.path.join(glob.escape(g), "worktrees", "*"))):
                out += [os.path.join(d, w) for w in GITDIR_WATCH]
        return out

    def _scan(self):
        """One bounded walk (depth, time; cached until reset with `cfg._scanned = None`)."""
        if getattr(self, "_scanned", None) is not None:
            return self._scanned
        found, deadline = [], time.time() + NESTED_BUDGET
        skip = NESTED_SKIP | {self.get("jobs", "executed_dir").split("/")[0]}
        firsts = {r.split("/")[0] for r in NESTED_PROTECT}
        self.nested_truncated = False
        top = os.path.join(self.project, ".git")
        gitdirs = [top] if os.path.isdir(top) and not os.path.islink(top) else []
        # the project's own submodules first: the most likely ones to be used outside the sandbox
        subs = [s for g in gitdirs for s in self._submodules(g, deadline)]
        for root, dirs, files in os.walk(self.project):
            if time.time() > deadline:
                self.nested_truncated = True
                break
            depth = 0 if root == self.project else os.path.relpath(root, self.project).count(os.sep) + 1
            if depth:
                names = firsts.intersection(dirs + files)
                for r in NESTED_PROTECT:
                    if r.split("/")[0] in names and os.path.lexists(os.path.join(root, r)):
                        found.append(os.path.join(root, r))
                g = os.path.join(root, ".git")
                if ".git" in dirs and not os.path.islink(g):
                    gitdirs.append(g)
                    subs += self._submodules(g, deadline)
            dirs[:] = [] if depth >= NESTED_DEPTH else sorted(d for d in dirs if d not in skip)
        for s in subs:
            found += [os.path.join(s, r) for r in GITDIR_PROTECT if os.path.lexists(os.path.join(s, r))]
        self._scanned = (found, gitdirs + subs)
        return self._scanned

    def _submodules(self, gitdir, deadline):
        """Submodule git dirs below gitdir/modules (a folder with a HEAD file), nested submodules included."""
        out, stack = [], [(os.path.join(gitdir, "modules"), 0)]
        while stack:
            if time.time() > deadline:
                self.nested_truncated = True
                break
            d, level = stack.pop()
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                p = os.path.join(d, n)
                if os.path.islink(p) or not os.path.isdir(p):
                    continue
                if os.path.isfile(os.path.join(p, "HEAD")):
                    out.append(p)
                    if level < SUBMODULE_DEPTH:
                        stack.append((os.path.join(p, "modules"), level + 1))
                elif level < SUBMODULE_DEPTH:
                    stack.append((p, level + 1))
        return out

    def automount(self):
        return expand_globs(self.list("sandbox", "automount"), self.project)

    def automount_explicit(self):
        """Entries without wildcards: report these if they cannot be mounted."""
        return [expand(p, self.project) for p in self.list("sandbox", "automount") if not any(c in p for c in "*?[")]

    def image_binds(self):
        return [p for p in expand_globs(self.list("jobs", "image_binds"), self.project) if os.path.exists(p)]


# ---- resource parsing (shared by broker and docs) ------------------------------------------------------
_MEM = re.compile(r"(\d+)([KMGT]?)", re.I)


def mem_mb(s):
    m = _MEM.fullmatch(str(s).strip())
    if not m:
        raise ConfigError("bad memory value %r (use e.g. 16G, 500M)" % s)
    n, u = int(m.group(1)), m.group(2).upper() or "M"
    return n * {"K": 1.0 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[u]


def time_min(s):
    """Slurm time formats: MM, MM:SS, HH:MM:SS, D-HH, D-HH:MM, D-HH:MM:SS -> minutes (rounded up)."""
    s = str(s).strip()
    if not re.fullmatch(r"(\d+-)?\d+(:\d+){0,2}", s):
        raise ConfigError("bad time value %r (use e.g. 04:00:00 or 2-00:00:00)" % s)
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
        parts = [int(x) for x in s.split(":")] + [0, 0]
        h, m, sec = parts[0], parts[1], parts[2]
    else:
        parts = [int(x) for x in s.split(":")]
        if len(parts) == 1:
            h, m, sec = 0, parts[0], 0
        elif len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        else:
            h, m, sec = parts
    return days * 1440 + h * 60 + m + (1 if sec else 0)
