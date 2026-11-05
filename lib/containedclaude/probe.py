"""Runs INSIDE a sandbox (session or job) and reports what it can reach, as JSON. Used by `containedclaude doctor`.

Every probe is harmless: write probes create a uniquely named empty file and remove it again immediately.
argv[1]: JSON {"project", "mode", "outside": [dirs writable on the host], "protected": [...], "hidden": [...],
               "pinned": [folders that must not be renamable], "sockets": [...], "socket": broker socket or null}
"""
import errno
import fcntl
import json
import os
import socket
import subprocess
import sys
import termios


def try_write(d):
    p = os.path.join(d, ".containedclaude-probe-%s" % os.urandom(4).hex())
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.unlink(p)
        return True, "created"
    except OSError as e:
        return False, errno.errorcode.get(e.errno, str(e.errno))


def try_modify(p):
    """Append nothing (open for writing without truncating): tests write permission on an existing path."""
    if os.path.isdir(p):
        return try_write(p)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_APPEND)
        os.close(fd)
        return True, "opened for writing"
    except OSError as e:
        return False, errno.errorcode.get(e.errno, str(e.errno))


def visible(p):
    if not os.path.lexists(p):
        return False, "absent"
    if os.path.isdir(p):
        try:
            n = len(os.listdir(p))
        except OSError as e:
            return False, errno.errorcode.get(e.errno, "?")
        return n > 0, "%d entries" % n
    try:
        return os.path.getsize(p) > 0 or not os.path.isfile(p), "size %d" % os.path.getsize(p)
    except OSError:
        return True, "exists"


def run(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        return r.returncode, r.stdout.strip().splitlines()[-1:] or [""]
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, [str(e)]


def main():
    a = json.loads(sys.argv[1])
    res = []
    add = lambda name, ok_if, got, detail: res.append({"check": name, "expect": ok_if, "got": got,
                                                       "detail": detail})
    got, d = try_write(a["project"])
    add("write project folder", True, got, d)
    for o in a["outside"]:
        got, d = try_write(o)
        add("write outside: " + o, False, got, d)
    home = os.path.expanduser("~")
    for p in a["protected"]:
        if os.path.lexists(p):
            got, d = try_modify(p)
            label = next((os.path.relpath(p, b) for b in (a["project"], a.get("state", "/nonexistent"))
                          if p.startswith(b + os.sep)), "~" + p[len(home):] if p.startswith(home + os.sep) else p)
            add("modify protected: " + label, False, got, d)
    for d in a.get("pinned") or []:
        # a folder holding protected files must not be renamable
        name = "rename pinned folder " + os.path.relpath(d, a["project"])
        moved = d + ".cc-probe-%s" % os.urandom(4).hex()
        try:
            os.rename(d, moved)
            os.rename(moved, d)
            add(name, False, True, "renamed")
        except OSError as e:
            add(name, False, False, errno.errorcode.get(e.errno, str(e.errno)))
    for p in a["hidden"]:
        got, d = visible(p)
        add("hidden: " + p, False, got, d)
    for s in a["sockets"]:
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(3)
            c.connect(s)
            c.close()
            add("connect socket: " + s, False, True, "connected")
        except OSError as e:
            add("connect socket: " + s, False, False, errno.errorcode.get(e.errno, str(e)))
    with open("/proc/self/status") as fh:
        nnp = [l.split()[1] for l in fh if l.startswith("NoNewPrivs")]
    add("no new privileges (setuid disabled)", True, nnp == ["1"], "NoNewPrivs=%s" % (nnp or ["?"])[0])
    pids = [x for x in os.listdir("/proc") if x.isdigit()]
    add("own pid namespace (host processes invisible)", True, len(pids) < 50, "%d pids visible" % len(pids))
    m, s = os.openpty()
    try:
        fcntl.ioctl(s, termios.TIOCSTI, b"x")
        add("terminal injection (TIOCSTI) blocked", True, False, "ioctl succeeded")
    except OSError as e:
        add("terminal injection (TIOCSTI) blocked", True, e.errno == errno.EPERM, errno.errorcode.get(e.errno, "?"))
    finally:
        os.close(m)
        os.close(s)
    leaked = [k for k in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "KRB5CCNAME", "GPG_AGENT_INFO",
                          "CLAUDE_CODE_MESSAGING_SOCKET", "DISPLAY") if k in os.environ]
    add("host session variables removed", True, not leaked, " ".join(leaked) or "none")
    rc, out = run(["sbatch", "--version"])
    add("sbatch unavailable", True, rc != 0, "exit %d: %s" % (rc, out[0][:90]))
    if a.get("real_sbatch"):
        # the real binary (copied past the shim): must fail because Slurm's authentication (munge) is unreachable
        rc, out = run([a["real_sbatch"], "--test-only", "-t", "1", "--wrap", "true"])
        msg = out[0].lower()
        add("real sbatch binary cannot authenticate", True, rc != 0 and ("auth" in msg or "munge" in msg),
            "exit %d: %s" % (rc, out[0][:90]))
    if a.get("tmp_max"):
        st = os.statvfs("/tmp")
        size = st.f_blocks * st.f_frsize
        add("/tmp size limited", True, 0 < size <= a["tmp_max"] * 1.01, "%.1f GB (limit %.1f GB)" % (
            size / 2.0 ** 30, a["tmp_max"] / 2.0 ** 30))
    if a.get("mem_max") or a.get("tasks_max"):
        import glob
        unit = os.environ.get("CONTAINEDCLAUDE_SCOPE", "")
        hits = [h for depth in range(2, 8) for h in glob.glob("/sys/fs/cgroup/" + "*/" * depth + unit + ".scope")]
        for key, fname, label, scale in (("mem_max", "memory.max", "session memory limited (cgroup)", 2.0 ** 30),
                                         ("tasks_max", "pids.max", "session processes+threads limited (cgroup)", 1)):
            if not a.get(key):
                continue
            try:
                with open(os.path.join(hits[0], fname)) as fh:
                    got = int(fh.read().strip())
                add(label, True, got == a[key], ("%.1f GB (limit %.1f GB)" % (got / scale, a[key] / scale))
                    if scale > 1 else "%d (limit %d)" % (got, a[key]))
            except (IndexError, OSError, ValueError) as e:
                add(label, True, False, "scope %s not found: %s" % (unit, e))
    if a.get("cpus_max"):
        n = len(os.sched_getaffinity(0))
        add("session CPUs limited (affinity)", True, n <= a["cpus_max"], "%d of %d cores" % (n, os.cpu_count()))
        add("session at low priority (nice)", True, os.nice(0) >= a.get("nice", 0), "nice %d" % os.nice(0))
    rc, out = run(["apptainer", "--version"])
    if a["mode"] == "job":
        add("apptainer available in jobs", True, rc == 0, out[0][:90])
    else:
        add("apptainer unavailable on the login node", True, rc != 0, "exit %d" % rc)
    if a.get("socket"):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(10)
            c.connect(a["socket"])
            c.sendall(b'{"action": "ping"}\n')
            ok = b'"pong": true' in c.recv(4096)
            c.close()
            add("job broker reachable", True, ok, "ping")
        except OSError as e:
            add("job broker reachable", True, False, str(e))
        # the broker's refusals: answered before anything is submitted
        refusals = [("cpus above max_cpus", ["cc-run", "--cpus", "100000", "--", "true"], "cpus must be"),
                    ("time above max_time", ["cc-run", "--time", "999-00:00:00", "--", "true"], "exceeds max_time"),
                    ("array above max_array", ["cc-run", "--array", "1-100000000", "--", "true"], "max_array"),
                    ("--after a job of another project", ["cc-run", "--after", "1", "--", "true"],
                     "not a job of this project"),
                    ("cancel a job of another project", ["cc-cancel", "1"], "not jobs of this project"),
                    ("ssh to a node that is no GPU node", ["cc-gpu", "not-a-gpu-node"], "is not a GPU node")]
        refusals += [("--env " + k, ["cc-run", "--env", k + "=/x", "--", "true"], "is not allowed")
                     for k in ("PATH", "LD_PRELOAD", "APPTAINER_BIND")]
        for name, cmd, want in refusals:
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                   timeout=120)
                out = r.stdout.strip()
                refused = r.returncode != 0 and want in out and "submitted" not in out
                add("broker refuses: " + name, True, refused, (out.splitlines() or [""])[-1][:90])
            except (OSError, subprocess.TimeoutExpired) as e:
                add("broker refuses: " + name, True, False, str(e))
    print("CC_PROBE_RESULT " + json.dumps(res))


if __name__ == "__main__":
    main()
