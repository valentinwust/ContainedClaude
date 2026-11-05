"""`containedclaude doctor`: check the host and verify the sandbox from the inside (session and jobs)."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import CLONE, broker, compat, policy
from .config import CLAUDE_CONFIG_DIR, HOME, STATE, UID, Config, with_xdg

PROBE = os.path.join(CLONE, "lib", "containedclaude", "probe.py")


def host_writable_dirs(cfg):
    """Directories the user can write to on the host, outside the sandbox's writable paths: must be read-only
    inside."""
    cands = [HOME, os.path.dirname(cfg.project), os.path.join(HOME, ".local", "bin")] + cfg.automount() + \
        cfg.image_binds()
    for c in list(cands):
        if os.path.isdir(c):
            try:
                cands += [os.path.join(c, x) for x in sorted(os.listdir(c))[:30]]
            except OSError:
                pass
    out, writable = [], cfg.write_paths() + [CLAUDE_CONFIG_DIR]
    for c in cands:
        c = os.path.realpath(c)
        if not os.path.isdir(c) or c in out or any(c == w or c.startswith(w + os.sep) for w in writable):
            continue
        if c == "/tmp" or c.startswith("/tmp/"):      # /tmp is private to the sandbox by design
            continue
        if os.access(c, os.W_OK | os.X_OK):
            out.append(c)
    return out[:25]


def real_sbatch_copy(cfg):
    """Copy the real sbatch binary to .contained/run/ (read-only inside), so the probe can run it past the shim:
    it must fail with an authentication error, which shows that Slurm's munge socket is really unreachable."""
    exe = compat.tool("sbatch")
    if not exe:
        return None
    dst = os.path.join(cfg.contained_dir, "run", "doctor-sbatch")
    try:
        shutil.copy2(os.path.realpath(exe), dst)
        os.chmod(dst, 0o755)
        return dst
    except OSError:
        return None


def probe_args(cfg, mode, sock=None, real_sbatch=None, scope_active=False):
    hidden = [os.path.expanduser(x) for x in with_xdg(["~/.ssh", "~/.gnupg", "~/.claude", "~/.config/gh",
                                                        "~/.codex/auth.json", "~/.local/share/jupyter/runtime"])]
    hidden += ["/run/user/%d" % UID, "/run/munge", os.path.join(STATE, "write_paths.txt")]
    hidden = [h for h in hidden if os.path.lexists(h)]
    sockets = [s for s in ["/run/munge/munge.socket.2", "/run/user/%d/bus" % UID, "/run/dbus/system_bus_socket",
                           "/run/user/%d/systemd/private" % UID, os.path.join(HOME, ".gnupg", "S.gpg-agent")]
               + glob.glob("/run/user/%d/cc-socks/*.sock" % UID) if os.path.exists(s)]
    protected = policy.effective_protected(cfg)        # symlinks resolved, as bound
    # renaming is only tried on the doctor's own fixture (never on the user's .git, even briefly)
    fixture = getattr(cfg, "doctor_fixture", None)
    pinned = [d for d in policy.pinned_dirs(protected, cfg.write_paths())
              if fixture and (d == fixture or d.startswith(fixture + os.sep))]
    if mode == "session":           # samples of the shared config folders' read-only entries (policy.config_ro)
        protected += [p for p in policy.config_ro(cfg, mode) if os.path.basename(p) in
                      ("settings.json", "config.toml", "hooks.json", "packages")]
    outside = host_writable_dirs(cfg) + [m for m in policy.ro_inner_mounts(cfg) if os.access(m, os.W_OK)]
    if sock:                        # the broker's socket folder (host /tmp): connect only, no writes
        outside.append(os.path.dirname(sock))
    return {"project": cfg.project, "state": STATE, "mode": mode, "outside": outside, "protected": protected,
            "pinned": pinned, "hidden": hidden, "sockets": sockets, "socket": sock, "real_sbatch": real_sbatch,
            "tmp_max": policy.tmp_size(cfg) if mode == "session" else 0,
            "mem_max": policy.session_mem(cfg) if mode == "session" and scope_active else 0,
            "tasks_max": policy.session_int(cfg, "session_tasks") if mode == "session" and scope_active else 0,
            "cpus_max": policy.session_int(cfg, "session_cpus") if mode == "session" and
            policy.session_int(cfg, "session_cpus") < (os.cpu_count() or 1) else 0, "nice": policy.SESSION_NICE}


def make_fixture(project):
    """A nested repository with a submodule git dir, so the probe can check that folders holding protected files
    cannot be renamed and that submodule configs are read-only (without touching the user's own repos)."""
    f = tempfile.mkdtemp(prefix=".cc-doctor-", dir=project)
    sub = os.path.join(f, ".git", "modules", "lib")
    for d in (os.path.join(f, ".git", "hooks"), os.path.join(sub, "hooks")):
        os.makedirs(d)
    for g in (os.path.join(f, ".git"), sub):
        for name, text in (("HEAD", "ref: refs/heads/main\n"), ("config", "[core]\n\tbare = false\n")):
            with open(os.path.join(g, name), "w") as fh:
                fh.write(text)
    # protected paths that are symlinks (relative) or lie behind a symlinked folder (absolute link): the
    # targets must be read-only
    os.makedirs(os.path.join(f, "claude.real"))
    for p, text in ((os.path.join(f, "claude.real", "settings.json"), "{}\n"), (os.path.join(f, "envrc.real"), "")):
        with open(p, "w") as fh:
            fh.write(text)
    os.symlink(os.path.join(f, "claude.real"), os.path.join(f, ".claude"))
    os.symlink("envrc.real", os.path.join(f, ".envrc"))
    return f


def parse(output):
    for line in output.splitlines():
        if line.startswith("CC_PROBE_RESULT "):
            return json.loads(line[len("CC_PROBE_RESULT "):])
    return None


def report(title, results, out):
    out.write("\n== %s\n" % title)
    if results is None:
        out.write("  FAIL  probe produced no result\n")
        return False
    ok_all = True
    for r in results:
        if r.get("note"):           # information only, never a failure
            out.write("  note  %-62s %s\n" % (r["check"][:62], r["detail"]))
            continue
        ok = r["got"] == r["expect"]
        ok_all &= ok
        out.write("  %s  %-62s %s\n" % ("pass" if ok else "FAIL", r["check"][:62], r["detail"]))
    return ok_all


def shared_config_check(out):
    """Entries of the shared Claude config and Codex home that are in none of policy's groups (read-only, per
    project, per runtime, shared): a tool update added them. Notes, not failures: unknown folders are per
    project from the next session start on, unknown files stay shared, until classified in policy.py
    (DEVELOPMENT §5.7). Runs after every Claude/Codex change (updates.check_after_change)."""
    results = []
    for name, found in (("claude-config", policy.claude_config_unclassified()),
                        ("codex-home", policy.codex_home_unclassified())):
        folders = policy.unknown_folders(found)
        files = [n for n, _ in found if n not in folders]
        if not found:
            results.append({"check": "%s: all entries classified" % name, "got": True, "expect": True,
                            "detail": "all entries known"})
        if folders:
            results.append({"check": "%s: new folders, kept per project" % name, "note": True,
                            "detail": "classify in policy.py: " + " ".join(folders)})
        if files:
            results.append({"check": "%s: new files, shared between projects" % name, "note": True,
                            "detail": "classify in policy.py: " + " ".join(files)})
    return report("shared config folders", results, out)


def late_automount_check(cfg):
    """An autofs path NOT mounted at start must stay invisible (it would otherwise arrive writable)."""
    mounted = set()
    with open("/proc/self/mountinfo") as fh:
        for l in fh:
            mounted.add(l.split()[4])
    try:
        homes = subprocess.run(["getent", "passwd"], stdout=subprocess.PIPE, universal_newlines=True,
                               timeout=30).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    for l in homes[:2000]:
        f = l.split(":")
        if len(f) > 5 and f[5].startswith("/home/") and f[5] not in mounted and f[5] != HOME:
            return f[5]
    return None


def session_probe(cfg, out, real_sbatch=None):
    sock_dir = tempfile.mkdtemp(prefix="containedclaude-doctor-", dir="/tmp")

    scope, scope_env = policy.session_scope(cfg)
    env = policy.sandbox_env(cfg, "session")
    env.update(scope_env)

    runtime = policy.project_runtime(cfg.project)
    store = policy.project_store(cfg, runtime)

    def prefix(**kw):           # the same command the launcher uses (incl. the size-limited /tmp, memory scope)
        return policy.session_command(cfg, "session", sock_dir, runtime, store, scope=scope, **kw)
    srv = broker.serve(cfg, os.path.join(sock_dir, "broker.sock"))
    sec = policy.seccomp_fd()
    late = late_automount_check(cfg)
    try:
        policy.automount(cfg.automount())
        args = probe_args(cfg, "session", os.path.join(policy.SOCK_IN_SANDBOX, "broker.sock"), real_sbatch,
                          scope_active=bool(scope))
        cmd = prefix(seccomp=sec) + ["--", sys.executable, PROBE, json.dumps(args)]
        r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                           timeout=300, pass_fds=(sec,) if sec is not None else ())
        results = parse(r.stdout)
        if results is not None and late:
            cmd2 = prefix() + ["--", "sh", "-c", "ls %s >/dev/null 2>&1 && grep -c ' %s ' /proc/self/mountinfo || echo 0" %
                 (late, late)]
            r2 = subprocess.run(cmd2, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, timeout=120)
            n = (r2.stdout.strip().splitlines() or ["0"])[-1]
            results.append({"check": "share mounted after start stays invisible (%s)" % late, "expect": True,
                            "got": n == "0", "detail": "mounted inside: %s" % ("no" if n == "0" else "YES")})
        if results is None:
            out.write(r.stdout[-3000:])
        return report("interactive sandbox (login node)", results, out)
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(sock_dir, ignore_errors=True)
        if sec is not None:
            os.close(sec)


def job_probes(cfg, out, gpu=False, image=None, real_sbatch=None):
    b = broker.Broker(cfg)
    pyexe = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    jobs = []
    args = probe_args(cfg, "job", real_sbatch=real_sbatch)
    jobs.append(("job, no image", b.submit({"argv": [pyexe, PROBE, json.dumps(args)], "cwd": cfg.project,
                                            "image": "none", "opts": {"name": "cc_doctor", "cpus": 1, "mem": "2G",
                                                                      "time": "00:20:00"}})))
    image = image or (cfg.get("jobs", "default_image") if cfg.get("jobs", "default_image") != "none" else None)
    if image:
        outside = " ".join("'%s'" % d for d in args["outside"][:8])
        script = ("p=%s; f=$p/.cc-img-probe-$$; touch $f && rm $f && echo 'pass  image: write project' || "
                  "echo 'FAIL  image: write project'; for d in %s; do touch \"$d/.cc-img-probe-$$\" 2>/dev/null && "
                  "{ rm -f \"$d/.cc-img-probe-$$\"; echo \"FAIL  image: wrote outside $d\"; } || "
                  "echo \"pass  image: outside read-only $d\"; done; ls %s/.ssh 2>/dev/null | grep -q . && "
                  "echo 'FAIL  image: ~/.ssh visible' || echo 'pass  image: ~/.ssh hidden'" %
                  (cfg.project, outside, HOME))
        jobs.append(("job in image %s" % image, b.submit({"argv": ["sh", "-c", script], "cwd": cfg.project,
                                                          "image": image, "opts": {"name": "cc_doctor_img",
                                                                                   "cpus": 1, "mem": "4G",
                                                                                   "time": "00:20:00"}})))
    if gpu:
        jobs.append(("GPU job in image %s" % (image or "none"),
                     b.submit({"argv": ["nvidia-smi", "-L"], "cwd": cfg.project, "image": image or "none",
                               "opts": {"name": "cc_doctor_gpu", "cpus": 1, "mem": "4G", "time": "00:20:00",
                                        "gpu": True}})))
    out.write("\nsubmitted: %s\nwaiting for the jobs (Ctrl-C to stop waiting; check later with the logs)...\n" %
              ", ".join("%s=%s" % (t, j["jobid"]) for t, j in jobs))
    ids = [j["jobid"] for _, j in jobs]
    while any(r["array_id"] in ids for r in b.queue()):
        time.sleep(15)
    ok_all = True
    for title, j in jobs:
        log = j["log"]
        text = open(log).read() if os.path.exists(log) else ""
        if "CC_PROBE_RESULT" in text:
            ok_all &= report(title + " (job %s)" % j["jobid"], parse(text), out)
        else:
            out.write("\n== %s (job %s)\n" % (title, j["jobid"]))
            lines = [l for l in text.splitlines() if l.startswith(("pass", "FAIL"))]
            if lines:
                out.write("".join("  %s\n" % l for l in lines))
                ok_all &= not any(l.startswith("FAIL") for l in lines)
            else:
                out.write("  (log %s)\n%s\n" % (log, "\n".join("  " + l for l in text.splitlines()[-15:])))
                ok_all &= ("status=0" in text)
    return ok_all


def doctor(project, jobs=False, gpu=False, image=None, out=sys.stdout):
    from .project import access_problem, find_root, start_problems
    out.write("containedclaude doctor (clone %s)\n" % CLONE)
    problem = access_problem(project or os.getcwd())
    if problem:
        out.write("FAIL %s\n" % problem)
        return 1
    problems = policy.check_host()
    for t in ("bwrap", "unshare", "claude", "sbatch", "apptainer"):
        out.write("  %-10s %s\n" % (t, compat.tool(t) or "MISSING"))
    if problems:
        out.write("FAIL host: %s\n" % "; ".join(problems))
        return 1
    missing = ["%s %s" % (t, f) for t, f, have in compat.optional_report() if not have]
    out.write("optional flags: %s\n" % ("all available" if not missing else "MISSING " + ", ".join(missing)))
    root = find_root(project or os.getcwd())
    if not root:
        out.write("no project found (run `containedclaude init DIR` first, or pass DIR)\n")
        return 1
    cfg = Config(root)
    out.write("project %s\nconfig  %s\n" % (root, ", ".join(cfg.sources) or "(defaults)"))
    problems = start_problems(root, cfg)
    if problems:
        out.write("FAIL %s\n" % "; ".join(problems))
        return 1
    compat.harden_host_env(cfg.write_paths())
    sb = real_sbatch_copy(cfg)
    cfg.doctor_fixture = make_fixture(cfg.project)     # before the first protect_paths() (cached scan)
    try:
        ok = session_probe(cfg, out, sb)
        ok &= shared_config_check(out)
        if jobs or gpu:
            ok &= job_probes(cfg, out, gpu=gpu, image=image, real_sbatch=sb)
    finally:
        shutil.rmtree(cfg.doctor_fixture, ignore_errors=True)
        if sb:
            try:
                os.unlink(sb)
            except OSError:
                pass
    out.write("\n%s\n" % ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED (see FAIL lines above)"))
    return 0 if ok else 1
