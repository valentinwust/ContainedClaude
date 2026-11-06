"""The sandbox policy, shared by interactive sessions (login node) and jobs (compute nodes).

Layers (outermost first):
  1. a private mount namespace, so network shares that the automounter mounts AFTER the start never propagate
     in (they would arrive writable): nsinit.py in sessions (also their pid namespace and /tmp), `unshare
     --user --map-current-user --mount --propagation private` in jobs.
  2. bwrap: the whole filesystem read-only (recursively), secrets hidden, the project (+ configured extra
     paths) writable, protected files inside the project read-only again, own pid/ipc namespace,
     no new privileges (setuid binaries do not elevate), seccomp filter against terminal injection.
  3. jobs only: apptainer runs inside this sandbox in unprivileged user-namespace mode.
"""
import glob
import os
import random
import stat
import struct
import sys
import tempfile
import threading
import time

from . import CLONE, compat
from .config import CLAUDE_CONFIG_DIR, CODEX_HOME, DATA_HOME, HOME, UID, inner_mounts, mem_mb

SHIM = os.path.join(CLONE, "sandbox", "shim")
SANDBOX_BIN = os.path.join(CLONE, "sandbox", "bin")
SOCK_IN_SANDBOX = "/tmp/.containedclaude"     # broker socket directory as seen inside the sandbox
XDG_RUNTIME_IN_SANDBOX = "/tmp/.xdg-runtime"
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
# caches, below <project>/.cache (sandbox_env); uv/python: `uv python install` (~/.local is read-only)
CACHE_VARS = {"XDG_CACHE_HOME": "", "PIP_CACHE_DIR": "pip", "CONDA_PKGS_DIRS": "conda/pkgs",
              "MPLCONFIGDIR": "matplotlib", "npm_config_cache": "npm", "UV_PYTHON_INSTALL_DIR": "uv/python"}
# what a job without an image has and an image job gets too (apptainer --containall cleans the environment)
IMAGE_ENV = ("USER", "LOGNAME", "MPLBACKEND", "HDF5_USE_FILE_LOCKING") + THREAD_VARS + tuple(CACHE_VARS)
JUPYTER_RUNTIME_IN_SANDBOX = "/tmp/jupyter-runtime"   # sessions: the real one (~/.local/share/jupyter/runtime) is hidden
ENVDOC = "CLAUDE_environment.md"           # on disk: a notice for agents outside; inside: envdoc_inside over it
NSINIT = os.path.join(CLONE, "lib", "containedclaude", "nsinit.py")
SYS_PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

# Commands replaced by a stand-in that explains the alternative (they could not work anyway: no Slurm auth,
# no keys, no user bus). Pure convenience; the real enforcement is what the sandbox can reach.
SHIM_SESSION = ["sbatch", "srun", "salloc", "sattach", "sbcast", "scrontab", "squeue", "scancel", "sacct", "sinfo",
                "scontrol", "sprio", "sshare", "sstat", "apptainer", "singularity", "run-singularity",
                "ssh", "scp", "sftp", "slogin", "systemd-run", "crontab"]
SHIM_JOB = [c for c in SHIM_SESSION if c not in ("apptainer", "singularity", "run-singularity")]

# The contained Claude config folder (CLAUDE_CONFIG_DIR) and Codex home (CODEX_HOME) are shared by all contained
# projects: one login, one set of settings and plugins. Their entries fall into groups, so that one project's
# sessions cannot influence another's (DEVELOPMENT §5.7; doctor reports entries in none of the groups, which a
# tool update may add):
# - read-only inside (they define behaviour: hooks, commands, plugins, mods, ...),
CLAUDE_CONFIG_RO = ["settings.json", "settings.local.json", "CLAUDE.md", "hooks", "plugins", "skills", "agents",
                    "commands", "output-styles", "rules", "keybindings.json", "statusline.sh", "dev-mods",
                    "cowork_plugins", "workflows", "themes"]
CLAUDE_CONFIG_RW_UNDER_RO = ["plugins/data"]
# - recorded by a session and read back later (transcripts for --resume, auto-memory, plans, rewind
#   checkpoints, pasted content, background jobs, ...): per project, in <project>/.contained/state/claude/,
CLAUDE_CONFIG_PER_PROJECT = ["projects", "file-history", "plans", "todos", "paste-cache", "jobs", "agent-memory",
                             "memory", "remote-control", "shares", "uploads", "chrome", "ccr"]
CLAUDE_CONFIG_PER_PROJECT_FILES = ["history.jsonl"]    # appended in place (verified), so a file bind works
# - alive for a session only and sourced, executed or connected to (shell snapshots and hook environment
#   files go into every Bash command; session records, teams, tasks, daemon socket): per project, in the
#   project's runtime folder (tmpfs, project_runtime),
CLAUDE_CONFIG_PER_RUNTIME = ["sessions", "shell-snapshots", "session-env", "teams", "tasks", "daemon"]
# - shared and writable: root files Claude replaces by rename (a file bind would break them; .claude.json holds
#   the account, onboarding and per-project trust/allowed tools; the login), caches and diagnostics.
CLAUDE_CONFIG_SHARED = [".claude.json", ".credentials.json", ".config.json", ".last-cleanup", ".update.lock",
                        ".last-update-result.json", ".deep-link-register-failed", "loop.md", "daemon.json",
                        "daemon.status.json", "daemon.scheduled.status.json", "daemon.log",
                        "mcp-needs-auth-cache.json", "mcp-discovery-cache", "hfi-auth.json",
                        "gh-pr-status-cache.json", "remote-settings.json", "backups", "cache", "statsig",
                        "telemetry", "debug", "traces", "feedback", "feedback-bundles", "startup-perf",
                        "usage-data", "dump-prompts", "state", "local", "seed-admin", "ide"]
# Created (empty) by project.ensure_state if missing: bwrap can only make EXISTING paths read-only, and a missing
# CLAUDE.md/skills/... would otherwise be creatable by one project's agent and load in every other project.
CLAUDE_CONFIG_PRECREATE_DIRS = [d for d in CLAUDE_CONFIG_RO if "." not in d] + CLAUDE_CONFIG_PER_PROJECT + \
    CLAUDE_CONFIG_PER_RUNTIME
CLAUDE_CONFIG_PRECREATE_FILES = {"CLAUDE.md": "", "settings.local.json": "{}\n", "history.jsonl": ""}
# Codex, the same groups:
# - read-only inside: what defines its behaviour (config.toml with MCP servers and notify commands, hooks,
#   global instructions, rules, prompts, skills, plugins) and packages/, where it would install programs it
#   runs (its background server, switched off by ensure_state). Changed in `containedclaude admin`; the folder
#   trust Codex would save in config.toml is written by the launcher (project.codex_trust),
CODEX_HOME_RO = ["config.toml", "hooks.json", "AGENTS.md", "rules", "prompts", "skills", "plugins", "packages"]
CODEX_HOME_PRECREATE_DIRS = ["rules", "prompts", "skills", "plugins", "packages"]
CODEX_HOME_PRECREATE_FILES = {"AGENTS.md": "", "hooks.json": "{}\n"}
# - per project, persistent: a project's threads (rollouts for `codex resume`, prompt history),
CODEX_HOME_PER_PROJECT = ["sessions", "archived_sessions", "tui-thread-reference-capabilities"]
CODEX_HOME_PER_PROJECT_FILES = ["history.jsonl", "session_index.jsonl", "session_index.jsonl.lock"]   # appended in place
# - per project, runtime: shell snapshots, the helper programs it puts into tmp/, the pid files and socket links
#   of its background server (which lives in the session's /tmp and pid namespace),
CODEX_HOME_PER_RUNTIME = ["shell_snapshots", "tmp", ".tmp", "app-server-control", "app-server-daemon"]
# - shared and writable: the login, its memory and thread index, caches, logs.
CODEX_HOME_SHARED = ["auth.json", "auth.lock", "installation_id", "version.json", "models_cache.json", "cache",
                     "thread-writer-locks", "log", "logs", ".sandbox_migration", ".sqlite-maintenance.lock",
                     ".codex-global-state.json"]
CODEX_HOME_SHARED_PATTERNS = ("*.sqlite", "*.sqlite-*")

# Environment variables never passed into a sandbox (sockets/credentials of the host session).
DROP_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "CODEX_", "SSH_", "GPG_", "DBUS_", "KRB5", "XDG_RUNTIME_DIR",
                     "DISPLAY", "WAYLAND_", "XAUTHORITY", "SESSION_MANAGER", "VSCODE_", "TERM_PROGRAM_",
                     "GIT_ASKPASS", "SUDO_", "SLURM_", "MUNGE", "AI_AGENT", "CLAUDE_PID", "CLAUDE_EFFORT",
                     "CLAUDE_PLUGIN", "CONTAINEDCLAUDE_")


# ---- automount ------------------------------------------------------------------------------------------
def automount(paths, timeout=90.0, report=None):
    """Touch each path so the automounter mounts it before the private namespace is created.
    All in parallel threads with one overall deadline, so a dead NFS server cannot block the start.
    `report(message)` is called once when shares have not answered after 2 s, so a waiting user sees why.
    Returns the paths that could not be reached."""
    results = {}

    def touch(p):
        try:
            os.listdir(p) if os.path.isdir(p) else os.stat(p)
            results[p] = True
        except OSError:
            results[p] = False

    paths = list(dict.fromkeys(paths))
    threads = [threading.Thread(target=touch, args=(p,), daemon=True) for p in paths]
    for t in threads:
        t.start()
    deadline = time.time() + timeout
    soon = time.time() + 2.0
    for t in threads:
        t.join(max(0.0, soon - time.time()))
    pending = [p for p in paths if p not in results]
    if pending and report:
        report("waiting for network shares that have not answered yet (up to %d s; a share that does not answer "
               "stays invisible inside): %s" % (timeout, " ".join(pending)))
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    return [p for p in paths if not results.get(p)]


# ---- seccomp --------------------------------------------------------------------------------------------
def seccomp_program():
    """Classic BPF: allow everything except ioctl(TIOCSTI/TIOCLINUX) (typing into the user's terminal after the
    session ends) and non-native syscall ABIs. x86_64 only; returns None elsewhere."""
    if os.uname().machine != "x86_64":
        return None
    LD, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
    ALLOW, ERRNO = 0x7FFF0000, 0x00050000
    EPERM, ENOSYS = 1, 38
    AUDIT_ARCH_X86_64, X32_BIT, NR_IOCTL = 0xC000003E, 0x40000000, 16
    TIOCSTI, TIOCLINUX = 0x5412, 0x541C
    prog = [
        (LD, 0, 0, 4),                       # 0: A = arch
        (JEQ, 1, 0, AUDIT_ARCH_X86_64),      # 1: native -> 3
        (RET, 0, 0, ERRNO | ENOSYS),         # 2: other ABI (i386 int80): refuse
        (LD, 0, 0, 0),                       # 3: A = nr
        (JGE, 5, 0, X32_BIT),                # 4: x32 ABI -> 10
        (JEQ, 0, 3, NR_IOCTL),               # 5: ioctl -> 6, else -> 9
        (LD, 0, 0, 24),                      # 6: A = low 32 bits of args[1] (request)
        (JEQ, 2, 0, TIOCSTI),                # 7: -> 10
        (JEQ, 1, 0, TIOCLINUX),              # 8: -> 10
        (RET, 0, 0, ALLOW),                  # 9
        (RET, 0, 0, ERRNO | EPERM),          # 10
    ]
    return b"".join(struct.pack("=HBBI", *ins) for ins in prog)


def seccomp_fd():
    prog = seccomp_program()
    if prog is None:
        return None
    f = tempfile.TemporaryFile()
    f.write(prog)
    f.flush()
    f.seek(0)
    fd = os.dup(f.fileno())
    f.close()
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


# ---- bwrap arguments ------------------------------------------------------------------------------------
def _exists(p):
    return os.path.lexists(p)


def _hide(p):
    try:
        st = os.lstat(p)
    except OSError:
        return []
    if stat.S_ISLNK(st.st_mode):
        try:
            st = os.stat(p)
            p = os.path.realpath(p)
        except OSError:
            return []
    if stat.S_ISDIR(st.st_mode):
        return ["--tmpfs", p]          # made read-only at the end (after binds below it are in place)
    return ["--ro-bind", "/dev/null", p]


def envdoc_inside(cfg):
    """What agents read as the project's CLAUDE_environment.md inside the sandbox (written by write_envdoc)."""
    return os.path.join(cfg.contained_dir, "run", ENVDOC)


def pinned_dirs(paths, roots):
    """The folders between the writable root a protected path lies in and the path itself (<project>/.git for
    .git/config; sub, sub/.git for sub/.git/config). Bound onto themselves (writable, as before) they become mount
    points, which cannot be renamed or removed (EBUSY), so the read-only files stay where they are. Outer folders
    first: a bind covers whatever was mounted below its target before."""
    out = []
    for p in paths:
        root = max((r for r in roots if p.startswith(r.rstrip(os.sep) + os.sep)), key=len, default=None)
        if root is None:
            continue                    # not writable anyway
        d = os.path.dirname(p)
        while len(d) > len(root.rstrip(os.sep)):
            if d not in out and os.path.isdir(d) and not os.path.islink(d):
                out.append(d)
            d = os.path.dirname(d)
    return sorted(out, key=lambda d: d.count(os.sep))


def ro_inner_mounts(cfg):
    """Mount points inside the writable paths that the sandbox binds read-only (those it can reach)."""
    out = []
    for mp in inner_mounts(cfg.write_paths()):
        if os.path.isdir(mp) and os.access(mp, os.X_OK):
            out.append(mp)
        else:
            compat.warn_once("mount %s inside a writable folder is not accessible; left as it is" % mp)
    return out


def effective_protected(cfg):
    """The protected paths as they are bound: existing ones, symlinks resolved on the host side (bwrap binds by
    path: a link in the path would protect the wrong place, and an absolute one makes the start fail), only
    those inside a writable area (everything else is read-only anyway), without duplicates."""
    roots = [r.rstrip(os.sep) + os.sep for r in cfg.write_paths()]
    out = []
    for p in cfg.protect_paths():
        if not _exists(p):
            continue
        rp = os.path.realpath(p)
        if os.path.exists(rp) and any(rp.startswith(r) for r in roots) and rp not in out:
            out.append(rp)
    return out


def _shim_targets(names):
    seen, out = set(), []
    path_dirs = (compat.inner("PATH") or "").split(os.pathsep) + ["/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin",
                                                                "/usr/local/sbin", "/sbin"]
    for n in names:
        for d in path_dirs:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                rp = os.path.realpath(p)
                if rp not in seen and not rp.startswith(CLONE + os.sep):
                    seen.add(rp)
                    out += ["--ro-bind", SHIM, rp]
    return out


def tmp_size(cfg):
    """[sandbox] tmp_size in bytes (0 = no limit)."""
    v = cfg.get("sandbox", "tmp_size")
    if v.lower() in ("", "0", "none", "unlimited"):
        return 0
    return int(mem_mb(v.upper()) * 1024 * 1024)


def inner_setenv():
    """The user's original LD_LIBRARY_PATH / LD_PRELOAD, set INSIDE the sandbox only (bwrap --setenv), never in
    the environment of unshare/bwrap, which run outside it."""
    return {k: compat.inner(k) for k in ("LD_LIBRARY_PATH", "LD_PRELOAD") if compat.inner(k)}


def session_mem(cfg):
    """[sandbox] session_mem in bytes (0 = no limit): all processes of a session together, plus /tmp."""
    v = cfg.get("sandbox", "session_mem")
    if v.lower() in ("", "0", "none", "unlimited"):
        return 0
    return int(mem_mb(v.upper()) * 1024 * 1024)


def session_int(cfg, key):
    v = cfg.get("sandbox", key)
    return int(v) if v.isdigit() else 0


SESSION_NICE = 10


def session_cores(cfg):
    """The CPUs an interactive session may use (session_cpus of them, chosen at random among the launcher's
    own), or None if unlimited. Affinity is inherited by every process inside; a process could widen it again,
    so this is a courtesy cap, like the niceness (which cannot be undone without privileges)."""
    n = session_int(cfg, "session_cpus")
    try:
        have = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None
    if not n or n >= len(have):
        return None
    return sorted(random.sample(have, n))


def session_scope(cfg):
    """Resource limits for a whole interactive session: a transient systemd user scope (cgroup) around the
    sandbox with MemoryMax (session_mem), TasksMax (session_tasks) and, where the user manager has the cpu
    controller, CPUQuota (session_cpus). Returns (command prefix, extra environment for it). The prefix is
    empty (with a warning) when nothing is configured or systemd-run --user cannot be used here. The scope needs
    the host's XDG_RUNTIME_DIR; inside the sandbox bwrap --setenv puts the sandbox value back."""
    size, tasks, cpus = session_mem(cfg), session_int(cfg, "session_tasks"), session_int(cfg, "session_cpus")
    props = []
    if size:
        props += ["MemoryMax=%d" % size] + compat.SCOPE_PROPS
    if tasks:
        props.append("TasksMax=%d" % tasks)
    if cpus and "cpu" in compat.user_controllers():
        props.append("CPUQuota=%d%%" % (cpus * 100))
    if not props:
        return [], {}
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % UID
    err = compat.scope_error(runtime, props)
    if err:
        compat.warn_once("no session memory/task limit (systemd-run --user --scope unusable: %s)" % err)
        return [], {}
    unit = "containedclaude-%d-%s" % (os.getpid(), os.urandom(2).hex())
    prefix = [compat.tool("systemd-run"), "--user", "--scope", "--quiet", "--unit", unit]
    for p in props:
        prefix += ["-p", p]
    env = {"XDG_RUNTIME_DIR": runtime, "CONTAINEDCLAUDE_SCOPE": unit}
    if size:
        env["CONTAINEDCLAUDE_SESSION_MEM"] = cfg.get("sandbox", "session_mem")
    return prefix + ["--"], env


def _own_dir(p):
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == UID and (st.st_mode & 0o077) == 0


def project_runtime(project):
    """Per-project folder on the host (tmpfs) for what the sessions of ONE project share while they run: xdg/
    (bound as XDG_RUNTIME_DIR inside: Claude's cc-socks/ sockets) and the CLAUDE_CONFIG_PER_RUNTIME /
    CODEX_HOME_PER_RUNTIME entries (project_store). Other projects, other users and unsandboxed sessions have
    other folders. In the user's /run/user tmpfs when there is one, else a folder of ours in /tmp; either must
    be a directory of ours with mode 0700 and no symlink, or the start is refused."""
    from .broker import project_id
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not _own_dir(base):
        base = "/tmp/containedclaude-%d" % UID
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
        if not _own_dir(base):
            raise OSError("%s exists but is not a private folder of yours (another user's?): remove or rename it" % base)
    d = os.path.join(base, "containedclaude", project_id(project))
    os.makedirs(os.path.join(d, "xdg"), 0o700, exist_ok=True)
    return d


def project_store(cfg, runtime, mode="session"):
    """The per-project entries of the shared Claude config and Codex home (the lists above), bound over their
    places inside: what a session records and reads back later lives WITH the project in
    <project>/.contained/state/{claude,codex}/ (gitignored; read-only at that path inside), so transcripts and
    memory move and go with the project and no session reads or edits another project's; what lives only as
    long as a session (and is sourced, executed or connected to) is in the project's runtime folder
    (project_runtime, tmpfs). In sessions (not admin), folders on no list (a tool update added them) are per
    project too, until classified; unknown files stay shared (unknown_folders). Creates them; returns
    [(host source, path inside), ...]. OSError if the state folder is not writable."""
    state = os.path.join(cfg.contained_dir, "state")
    unknown = mode == "session"
    groups = [(state, "claude", CLAUDE_CONFIG_DIR, CLAUDE_CONFIG_PER_PROJECT +
               (unknown_folders(claude_config_unclassified()) if unknown else []), CLAUDE_CONFIG_PER_PROJECT_FILES),
              (runtime, "claude", CLAUDE_CONFIG_DIR, CLAUDE_CONFIG_PER_RUNTIME, ())]
    if cfg.bool("sandbox", "codex") and os.path.isdir(CODEX_HOME):
        groups += [(state, "codex", CODEX_HOME, CODEX_HOME_PER_PROJECT +
                    (unknown_folders(codex_home_unclassified()) if unknown else []), CODEX_HOME_PER_PROJECT_FILES),
                   (runtime, "codex", CODEX_HOME, CODEX_HOME_PER_RUNTIME, ())]
    out = []
    for base, sub, target, dirs, files in groups:
        for name in dirs:
            src = os.path.join(base, sub, name)
            os.makedirs(src, 0o700, exist_ok=True)
            out.append((src, os.path.join(target, name)))
        for name in files:
            src = os.path.join(base, sub, name)
            os.makedirs(os.path.dirname(src), 0o700, exist_ok=True)
            os.close(os.open(src, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
            out.append((src, os.path.join(target, name)))
    return out


def unclassified_entries(folder, known, patterns=()):
    """Entries of a shared config folder that are in none of the lists above (a tool update added them): they
    are shared between projects until classified. For doctor."""
    import fnmatch
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if n not in known and not any(fnmatch.fnmatch(n, p) for p in patterns))


# `containedclaude test` puts a folder of this name into the shared Claude config, a stand-in for one a tool
# update adds; it counts only for that test's own session (named in TEST_UNKNOWN_ENV), never for sessions
# of other projects that start meanwhile, nor in the doctor's report.
TEST_UNKNOWN_PREFIX = ".selftest-unknown-"
TEST_UNKNOWN_ENV = "CONTAINEDCLAUDE_TEST_UNKNOWN"


def claude_config_unclassified():
    """[(name, full path)] of the shared Claude config's entries on no list."""
    known = set(CLAUDE_CONFIG_RO + CLAUDE_CONFIG_PER_PROJECT + CLAUDE_CONFIG_PER_PROJECT_FILES +
                CLAUDE_CONFIG_PER_RUNTIME + CLAUDE_CONFIG_SHARED)
    own = os.environ.get(TEST_UNKNOWN_ENV)
    return [(n, os.path.join(CLAUDE_CONFIG_DIR, n)) for n in unclassified_entries(CLAUDE_CONFIG_DIR, known)
            if not n.startswith(TEST_UNKNOWN_PREFIX) or n == own]


def codex_home_unclassified():
    """[(name, full path)] of the Codex home's entries on no list."""
    known = set(CODEX_HOME_RO + CODEX_HOME_PER_PROJECT + CODEX_HOME_PER_PROJECT_FILES + CODEX_HOME_PER_RUNTIME +
                CODEX_HOME_SHARED)
    return [(n, os.path.join(CODEX_HOME, n)) for n in
            unclassified_entries(CODEX_HOME, known, CODEX_HOME_SHARED_PATTERNS)]


def unknown_folders(entries):
    """Names of the real folders (no symlinks) among unclassified entries: per project in sessions. Files
    stay shared: the tools replace them by rename, which a bind over a single file would break."""
    return [n for n, p in entries if os.path.isdir(p) and not os.path.islink(p)]


def config_ro(cfg, mode):
    """Entries of the shared config folders that are read-only inside (none in admin mode)."""
    if mode != "session":
        return []
    out = [os.path.join(CLAUDE_CONFIG_DIR, r) for r in CLAUDE_CONFIG_RO]
    if cfg.bool("sandbox", "codex"):
        out += [os.path.join(CODEX_HOME, r) for r in CODEX_HOME_RO]
    return out


def session_command(cfg, mode, sock_dir, runtime, store, seccomp=None, scope=()):
    """Full command prefix for an interactive sandbox (session/admin), up to and excluding `--`. nsinit.py
    creates the namespaces (a pid namespace with pids starting at a random base, so that the sessions of one
    project never repeat a pid: Claude names its session socket and record after it), applies niceness and CPU
    affinity (session_cpus), mounts the session's tmpfs on /tmp (size-limited by tmp_size; /var/tmp and
    /dev/shm are sub-folders of it) and keeps the broker socket folder and the project's runtime folder
    (project_runtime) reachable below it. `store`: project_store(cfg). `scope`: the systemd-run prefix from
    session_scope (outermost)."""
    outer = [SYS_PYTHON, "-I", NSINIT, "--tmp-size", str(tmp_size(cfg) or 0),
             "--pid-base", str(random.randrange(100000, 3000000)),
             "--keep", os.path.join(runtime, "xdg"), XDG_RUNTIME_IN_SANDBOX]
    if sock_dir:
        outer += ["--keep", sock_dir, SOCK_IN_SANDBOX]
    cores = session_cores(cfg)
    if cores:
        outer += ["--nice", str(SESSION_NICE), "--cpus", ",".join(map(str, cores))]
    setenv = inner_setenv()
    setenv["XDG_RUNTIME_DIR"] = XDG_RUNTIME_IN_SANDBOX     # the host's value is only for systemd-run outside
    return list(scope) + outer + ["--"] + bwrap_args(cfg, mode, sock_dir=sock_dir, seccomp=seccomp,
                                                     setenv=setenv, store=store)


def bwrap_args(cfg, mode, sock_dir=None, scratch=None, gpu=False, chdir=None, seccomp=None, setenv=None,
               store=()):
    """mode: 'session' (interactive, login node), 'admin' (session with the contained Claude config fully
    writable, for /login and plugin installs) or 'job' (compute node, apptainer allowed).
    Sessions and admin run under nsinit.py (session_command), which already made the pid namespace and
    mounted the session's tmpfs on /tmp (with /var/tmp and /dev/shm inside it, the broker socket folder and
    the runtime folder below it): re-bind the three read-write under the read-only root (the socket folder
    read-only again), no --unshare-pid. Jobs: scratch (node-local) is bound on /tmp, own pid namespace.
    sock_dir: the broker's socket folder on the host (sessions). store: project_store(), the project's
    entries of the shared config folders (transcripts, memory, session records, shell snapshots, Codex
    rollouts, ...), bound over their places."""
    a = [compat.tool("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if mode == "job":
        if os.path.exists("/dev/fuse"):
            a += ["--dev-bind", "/dev/fuse", "/dev/fuse"]
        if gpu:
            for d in sorted(glob.glob("/dev/nvidia*")) + sorted(glob.glob("/dev/dri")):
                a += ["--dev-bind", d, d]
        a += ["--bind", scratch, "/tmp"]
    else:
        a += ["--bind", "/tmp", "/tmp", "--bind", "/dev/shm", "/dev/shm"]
    if os.path.isdir("/var/tmp"):
        a += ["--tmpfs", "/var/tmp"] if mode == "job" else ["--bind", "/var/tmp", "/var/tmp"]

    hidden_dirs = []
    for p in cfg.hide_paths():
        h = _hide(p)
        a += h
        if h and h[0] == "--tmpfs":
            hidden_dirs.append(h[1])
    for p in cfg.expose_paths():
        if os.path.exists(p):
            a += ["--ro-bind", p, p]

    for p in cfg.write_paths():
        if os.path.isdir(p):
            a += ["--bind", p, p]
    # other file systems mounted inside a writable folder (a share, a user's FUSE mount): read-only again. The
    # writable bind above is recursive, so without this they would be writable through the project
    for mp in ro_inner_mounts(cfg):
        a += ["--ro-bind", mp, mp]

    if mode in ("session", "admin"):
        for d in (CLAUDE_CONFIG_DIR, CODEX_HOME if cfg.bool("sandbox", "codex") else None):
            if d and os.path.isdir(d):
                a += ["--bind", d, d]
        for p in config_ro(cfg, mode):
            if _exists(p):
                a += ["--ro-bind", p, p]
        for rel in CLAUDE_CONFIG_RW_UNDER_RO:
            p = os.path.join(CLAUDE_CONFIG_DIR, rel)
            if os.path.isdir(p):
                a += ["--bind", p, p]
        if sock_dir:            # the broker's socket folder: connecting works on a read-only mount
            a += ["--ro-bind", SOCK_IN_SANDBOX, SOCK_IN_SANDBOX]      # kept there by nsinit
        for src, dest in store:         # per project: transcripts, memory, session records, snapshots, ...
            a += ["--bind", src, dest]

    protected = effective_protected(cfg)
    for d in pinned_dirs(protected, cfg.write_paths()):
        a += ["--bind", d, d]
    envdoc, inside = os.path.join(cfg.project, ENVDOC), envdoc_inside(cfg)
    for p in protected:
        # the sandbox description in place of the on-disk notice (without it the notice shows: the safe side)
        src = inside if p == envdoc and os.path.isfile(inside) else p
        a += ["--ro-bind", src, p]

    a += _shim_targets(SHIM_JOB if mode == "job" else SHIM_SESSION)
    # optional flags are detected from `bwrap --help` (compat.py) so a bwrap update can't break the start
    # sessions: XDG_RUNTIME_DIR is the project's runtime folder, kept below /tmp by nsinit; jobs: empty
    for d in [XDG_RUNTIME_IN_SANDBOX] if mode == "job" else [JUPYTER_RUNTIME_IN_SANDBOX]:
        a += ["--dir" if compat.has("bwrap", "--dir") else "--tmpfs", d]
    if compat.has("bwrap", "--remount-ro"):
        for d in hidden_dirs:          # empty placeholders: read-only, so nothing seems to be saved there
            a += ["--remount-ro", d]
    elif hidden_dirs:
        compat.warn_once("bwrap lacks --remount-ro: hidden folders are empty but writable (in memory only)")
    if mode == "job":                  # sessions already run in nsinit's pid namespace (unique pids)
        a += ["--unshare-pid"]
    for f in ("--unshare-ipc", "--unshare-cgroup-try"):
        if compat.has("bwrap", f):
            a.append(f)
    a.append("--die-with-parent")
    if seccomp is not None:
        if compat.has("bwrap", "--seccomp"):
            a += ["--seccomp", str(seccomp)]
        else:
            compat.warn_once("bwrap lacks --seccomp: terminal injection (TIOCSTI) is not filtered")
    # variables from a job request: set INSIDE the sandbox only, never in the environment of unshare/bwrap
    # (which run outside it)
    for k, v in sorted((setenv or {}).items()):
        a += ["--setenv", k, v]
    a += ["--chdir", chdir or cfg.project]
    return a


def outer_unshare():
    return compat.outer_unshare()


def sandbox_env(cfg, mode, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROP_ENV_PREFIXES)}
    # the user's original PATH (incl. project environments): only looked up INSIDE the sandbox; unshare/bwrap and
    # nsinit are started with absolute paths and exec bwrap/the command by absolute path or inside
    env["PATH"] = SANDBOX_BIN + os.pathsep + (compat.inner("PATH") or "/usr/bin:/bin")
    env["CONTAINEDCLAUDE"] = "1"
    env["CONTAINEDCLAUDE_MODE"] = mode
    env["CONTAINEDCLAUDE_PROJECT"] = cfg.project
    env["CONTAINEDCLAUDE_CLONE"] = CLONE
    env["CONTAINEDCLAUDE_WRITABLE"] = os.pathsep.join(cfg.write_paths())
    env["CONTAINEDCLAUDE_PROTECTED"] = os.pathsep.join(effective_protected(cfg))
    env["XDG_RUNTIME_DIR"] = XDG_RUNTIME_IN_SANDBOX
    env["TMPDIR"] = "/tmp"
    env.setdefault("MPLBACKEND", "Agg")
    env.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")
    # caches (pip, uv, npm, huggingface, torch, conda packages, matplotlib) live in the project, in sessions AND
    # jobs: the real home is read-only, /tmp is RAM on the login node and deleted after a job, and the project is
    # on the shared file system, so downloads (models, packages) are shared by sessions and jobs
    cache = os.path.join(cfg.project, ".cache")
    for k, sub in CACHE_VARS.items():
        env[k] = os.path.join(cache, sub) if sub else cache
    # git refuses repositories owned by someone else ("dubious ownership"), and on the SDS every file belongs to
    # nobody (curry can't map the owner names the SDS reports): list the writable folders and everything below
    # them as safe. GIT_CONFIG_* is "protected" config, the only kind git accepts safe.directory from.
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        n = 0
    for p in cfg.write_paths():
        for v in (p, p + "/*"):
            env["GIT_CONFIG_KEY_%d" % n] = "safe.directory"
            env["GIT_CONFIG_VALUE_%d" % n] = v
            n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    if mode in ("session", "admin"):
        env["JUPYTER_RUNTIME_DIR"] = JUPYTER_RUNTIME_IN_SANDBOX      # kernel connection files (the real dir is hidden)
        env["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR
        n = session_int(cfg, "session_cpus")
        if n:                           # BLAS/OpenMP would otherwise start one thread per core of the login node
            for k in THREAD_VARS:
                env.setdefault(k, str(n))
        env["CONTAINEDCLAUDE_CONFIG_RO"] = os.pathsep.join(config_ro(cfg, mode))
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CONTAINEDCLAUDE_SOCKET"] = os.path.join(SOCK_IN_SANDBOX, "broker.sock")
        if cfg.bool("sandbox", "codex"):
            env["CODEX_HOME"] = CODEX_HOME
    else:
        env["HOME"] = "/tmp/home"          # writable throw-away HOME; the real home stays readable at its path
        # apptainer (only jobs have it): never the setuid helper (it cannot work in the sandbox)
        env["APPTAINER_USERNS"] = "1"
        env["APPTAINER_CACHEDIR"] = os.path.join(cfg.project, cfg.get("jobs", "apptainer_cache"))
        env["CONTAINEDCLAUDE_REAL_HOME"] = HOME
        # kernelspecs the user installed with --user live in the real home (HOME is /tmp/home here)
        jp = os.path.join(DATA_HOME, "jupyter")
        env["JUPYTER_PATH"] = jp + (os.pathsep + env["JUPYTER_PATH"] if env.get("JUPYTER_PATH") else "")
    if extra:
        env.update(extra)
    return env


def check_host():
    """Return a list of problems with the host (missing tools, user namespaces disabled)."""
    problems = list(compat.problems_sandbox())
    try:
        with open("/proc/sys/user/max_user_namespaces") as fh:
            if int(fh.read().strip()) == 0:
                problems.append("unprivileged user namespaces are disabled (user.max_user_namespaces = 0)")
    except OSError:
        pass
    if sys.version_info < (3, 6):
        problems.append("python >= 3.6 required")
    return problems
