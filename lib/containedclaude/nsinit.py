"""Outer layer of an interactive sandbox: replaces `unshare --user --map-current-user --mount --propagation
private` for sessions, mounts the session's private tmpfs ON /tmp (size-limited by tmp_size: bwrap < 0.7 has no
--size, and an unlimited tmpfs would let a session fill the login node's RAM) with /var/tmp and /dev/shm as
sub-folders of it, keeps folders the launcher needs reachable below it (the broker socket), and applies the
session's CPU courtesy limits (niceness, CPU affinity), inherited by everything inside.

    /usr/bin/python3 -I nsinit.py --pid-base N [--tmp-size BYTES] [--keep SRC DEST]... [--nice N]
                                  [--cpus 0,1,2] -- /abs/path/bwrap ARGS...

--pid-base N: a pid namespace of our own whose pids start at N (bwrap then runs without --unshare-pid). The
sessions of one project share a runtime folder, and Claude names its session socket and record after its pid,
so pids must not repeat between sessions: without this every sandbox's first process is pid 2. This process
stays outside the new namespace as the waiter; its child is pid 1 inside, sets the counter and acts as init
(reaps orphans, forwards SIGTERM/SIGHUP, returns bwrap's exit status; SIGINT is ignored like in the launcher,
the terminal delivers it to the foreground group itself).

The tmpfs sits directly on /tmp (mount root "/") on purpose: Codex's own sandbox (bwrap as well) derives the
canonical path of its /tmp folders from the mount table, and with /tmp as a sub-folder bind of a tmpfs mounted
elsewhere it computed a path that does not exist inside and failed. bwrap then re-binds /tmp, /var/tmp and
/dev/shm read-write under its read-only root. `--keep SRC DEST`: SRC (a folder in the host's /tmp, which this
covers) is opened before /tmp is covered and bound at DEST below the new /tmp. Standard library only (ctypes),
no threads, so unshare() of a user namespace is allowed.
"""
import ctypes
import os
import signal
import sys

CLONE_NEWNS, CLONE_NEWUSER, CLONE_NEWPID = 0x00020000, 0x10000000, 0x20000000
MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_BIND, MS_REC, MS_PRIVATE = 0x2, 0x4, 0x8, 0x1000, 0x4000, 0x40000
PR_SET_PDEATHSIG = 1
INSIDE = {"/var/tmp": "/tmp/.var-tmp", "/dev/shm": "/tmp/.shm"}     # bound from these sub-folders of /tmp


def _forward(pid):
    def handler(signum, frame):
        try:
            os.kill(pid, signum)
        except OSError:
            pass
    return handler


def _exit_status(status):
    return 128 + os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)


def supervise(libc, mount, base, cmd):
    """Run `cmd` in a new pid namespace whose pids start at `base` (see the module docstring). Never returns."""
    if libc.unshare(CLONE_NEWPID) != 0:
        die("nsinit: unshare(pid) failed: %s" % os.strerror(ctypes.get_errno()))
    child = os.fork()
    if child:                                   # the waiter, outside the new namespace
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _forward(child))
        _, status = os.waitpid(child, 0)
        sys.exit(_exit_status(status))
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)       # pid 1 inside: dies with the waiter, taking the namespace
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)   # a procfs of THIS pid namespace (bwrap
    write("/proc/sys/kernel/ns_last_pid", str(base))                   # looks itself up under /proc/<pid>)
    grandchild = os.fork()
    if grandchild == 0:
        os.execv(cmd[0], cmd)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _forward(grandchild))
    while True:                                 # init: reap everything, leave when bwrap is gone
        try:
            pid, status = os.wait()
        except ChildProcessError:
            os._exit(1)
        if pid == grandchild:
            os._exit(_exit_status(status))


def die(msg):
    sys.stderr.write("containedclaude: %s\n" % msg)
    sys.exit(125)


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def parse(argv):
    """Options (--keep may repeat) and the command after --."""
    if "--" not in argv:
        die("nsinit: usage: --pid-base N [--tmp-size BYTES] [--keep SRC DEST]... [--nice N] [--cpus LIST] "
            "-- CMD...")
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    o, keep, j = {}, [], 0
    try:
        while j < len(opts):
            if opts[j] == "--keep":
                keep.append((opts[j + 1], opts[j + 2]))
                j += 3
            else:
                o[opts[j]] = opts[j + 1]
                j += 2
    except IndexError:
        die("nsinit: option without value: %s" % opts[j])
    return o, keep, cmd


def main(argv):
    o, keep, cmd = parse(argv)
    size = int(o.get("--tmp-size", "0"))
    base = int(o.get("--pid-base", "0"))
    if not cmd or not os.path.isabs(cmd[0]):
        die("nsinit: need an absolute command")
    if base <= 0:
        die("nsinit: need --pid-base N (N > 0)")

    # CPU courtesy limits: lower priority (cannot be raised back without privileges) and a subset of the cores
    nice = int(o.get("--nice", "0"))
    if nice > 0:
        try:
            os.nice(nice)
        except OSError:
            pass
    if o.get("--cpus"):
        try:
            os.sched_setaffinity(0, {int(c) for c in o["--cpus"].split(",")})
        except (OSError, ValueError) as e:
            sys.stderr.write("containedclaude: warning: could not limit the CPUs (%s)\n" % e)

    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]

    def mount(src, dst, fstype, flags, data=None):
        if libc.mount(src.encode(), dst.encode(), fstype.encode() if fstype else None, flags,
                      data.encode() if data else None) != 0:
            die("nsinit: mounting %s on %s failed: %s" % (src, dst, os.strerror(ctypes.get_errno())))

    uid, gid = os.getuid(), os.getgid()
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0:
        die("nsinit: unshare failed: %s" % os.strerror(ctypes.get_errno()))
    try:
        write("/proc/self/setgroups", "deny")
    except OSError:
        pass                                    # kernels < 3.19: no setgroups file
    write("/proc/self/uid_map", "%d %d 1" % (uid, uid))
    write("/proc/self/gid_map", "%d %d 1" % (gid, gid))
    # private propagation: shares the automounter mounts later never arrive (they would arrive writable)
    mount("none", "/", None, MS_REC | MS_PRIVATE)
    kept = [(os.open(src, os.O_PATH | os.O_DIRECTORY), dest) for src, dest in keep]    # before /tmp is covered
    mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777" + (",size=%d" % size if size > 0 else ""))
    for outer, inner in INSIDE.items():
        if os.path.isdir(outer):
            os.mkdir(inner)
            os.chmod(inner, 0o1777)
            mount(inner, outer, None, MS_BIND)
    for fd, dest in kept:
        os.makedirs(dest)
        mount("/proc/self/fd/%d" % fd, dest, None, MS_BIND)
        os.close(fd)
    supervise(libc, mount, base, cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
