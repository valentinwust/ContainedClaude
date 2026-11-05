"""Runs on the compute node as the Slurm batch step: re-enters the project's sandbox and executes the job.

Called by the batch script that broker.py generated:  /usr/bin/python3 -I jobwrap.py <spec.json>
The spec lives in <project>/.contained/run/ (read-only for the sandbox). Steps run one after another inside
the sandbox (same policy as the interactive session, plus apptainer + /dev/fuse + GPU devices); with an image,
each step runs in `apptainer exec --containall` inside that sandbox, with the variables and name resolution
a job without an image has. The first failing step ends the job.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from containedclaude import compat, config, policy  # noqa: E402
from containedclaude.config import Config  # noqa: E402


def apptainer_argv(cfg, spec, step, job_env):
    a = ["apptainer", "exec"]
    # --userns: also forced by APPTAINER_USERNS=1; --containall: a clean environment (IMAGE_ENV below) and of
    # the site's binds only the node's /etc/hosts and /etc/localtime (plus its resolv.conf)
    for f, val in (("--userns", None), ("--containall", None), ("--workdir", "/tmp/.apptainer-work")):
        if compat.likely("apptainer", f):
            a += [f] + ([val] if val else [])
        else:
            compat.warn_once("apptainer exec lacks %s; continuing without it" % f)
    a += ["--home", "/tmp/home", "--pwd", step["cwd"]]
    if spec.get("gpu"):
        a.append("--nv")
    binds = []
    for p in cfg.image_binds() + cfg.write_paths():
        if p not in binds:
            binds.append(p)
    for p in binds:
        a += ["--bind", p]           # read-only or not is decided by the surrounding sandbox
    env = {"CONTAINEDCLAUDE": "1", "CONTAINEDCLAUDE_PROJECT": cfg.project}
    env.update({k: job_env[k] for k in policy.IMAGE_ENV if k in job_env})    # as without an image
    for k in ("SLURM_JOB_ID", "SLURM_ARRAY_TASK_ID", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_COUNT",
              "SLURM_ARRAY_TASK_MIN", "SLURM_ARRAY_TASK_MAX", "SLURM_CPUS_PER_TASK", "SLURM_MEM_PER_NODE",
              "SLURM_JOB_NAME", "CUDA_VISIBLE_DEVICES"):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(spec.get("env") or {})
    for k, v in sorted(env.items()):
        a += ["--env", "%s=%s" % (k, v)]
    return a + [spec["image"]] + step["argv"]


class Stop:
    """SIGTERM/SIGINT/SIGHUP (Slurm time limit, scancel: TERM, then KILL after KillWait): pass the signal on to
    the running step, and let main() clean up the scratch folder instead of dying on the spot (without a per-job
    tmp container the folder would stay on the node's /tmp)."""
    def __init__(self):
        self.signum, self.since, self.proc = None, None, None
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, self.handler)

    def handler(self, signum, frame):
        if self.signum is None:
            self.signum, self.since = signum, time.time()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.send_signal(signum)
            except OSError:
                pass

    def wait(self, proc, grace=20.0):
        """Wait for the step; after a signal, give it `grace` seconds, then kill it."""
        self.proc = proc
        while True:
            try:
                return proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if self.signum is not None and time.time() - self.since > grace:
                    proc.kill()


def main():
    stop = Stop()
    compat.harden_host_env()
    spec_path = sys.argv[1]
    with open(spec_path) as fh:
        spec = json.load(fh)
    cfg = Config(spec["project"])
    compat.harden_host_env(cfg.write_paths())
    print("containedclaude job  host=%s  start=%s  image=%s  gpu=%s" % (
        os.uname().nodename, time.strftime("%Y-%m-%d %H:%M:%S"), spec["image"], spec.get("gpu")), flush=True)

    problems = policy.check_host() + ["writable path %s is not allowed: %s" % b
                                      for b in config.unsafe_write_paths(cfg.write_paths())]
    # location only: mounts differ per node (a user mount on the login node is just absent here)
    problems += ["writable path %s: %s" % b for b in config.placement_problems(cfg.write_paths(), check_mounts=False)]
    if problems:
        print("FATAL: " + "; ".join(problems), flush=True)
        return 2
    base = os.environ.get("TMPDIR") if os.path.isdir(os.environ.get("TMPDIR", "")) else "/tmp"
    scratch = tempfile.mkdtemp(prefix="containedclaude-job-", dir=base)
    for d in ("home", ".apptainer-work"):
        os.makedirs(os.path.join(scratch, d), exist_ok=True)
    needed = [cfg.project] + [s["cwd"] for s in spec["steps"]] + \
        ([spec["image"]] if spec["image"].startswith("/") else [])
    missing = policy.automount(cfg.automount() + cfg.image_binds() + needed)
    report = [p for p in missing if p in needed or p in cfg.automount_explicit()]
    if report:
        print("WARNING: not reachable, invisible inside the job: " + " ".join(report), flush=True)

    # keep Slurm's per-job variables, but drop everything the policy drops for sessions (SLURM_* included).
    # The request's --env variables are NOT put here: this environment is the one of unshare/bwrap, which run
    # outside the sandbox. They are set inside it with bwrap --setenv (and apptainer --env).
    slurm_env = {k: v for k, v in os.environ.items() if k.startswith("SLURM_") or k == "CUDA_VISIBLE_DEVICES"}
    env = policy.sandbox_env(cfg, "job", slurm_env)
    if os.environ.get("SLURM_CPUS_PER_TASK", "").isdigit():      # BLAS/OpenMP: the allocated CPUs, not the node's
        for k in policy.THREAD_VARS:
            env.setdefault(k, os.environ["SLURM_CPUS_PER_TASK"])
    req_env = policy.inner_setenv()
    req_env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    env["APPTAINER_TMPDIR"] = "/tmp/.apptainer-work"
    status = 0
    try:
        for i, step in enumerate(spec["steps"], 1):
            if stop.signum is not None:
                break
            print("=== step %d/%d: %s" % (i, len(spec["steps"]), step.get("label") or " ".join(step["argv"])),
                  flush=True)
            sec = policy.seccomp_fd()
            inner = step["argv"] if spec["image"] == "none" else apptainer_argv(cfg, spec, step, env)
            cmd = policy.outer_unshare() + policy.bwrap_args(cfg, "job", scratch=scratch, gpu=spec.get("gpu"),
                                                             chdir=step["cwd"], seccomp=sec, setenv=req_env) + \
                ["--"] + inner
            try:
                proc = subprocess.Popen(cmd, env=env, pass_fds=(sec,) if sec is not None else ())
            finally:
                if sec is not None:
                    os.close(sec)
            rc = stop.wait(proc)
            print("=== step %d exit status %d" % (i, rc), flush=True)
            if rc != 0:
                status = rc
                break
        if stop.signum is not None:
            status = 128 + stop.signum
            print("=== stopped by signal %d (time limit or cancelled); cleaning up" % stop.signum, flush=True)
    finally:
        # apptainer leaves read-only directories behind; make them removable
        for root, dirs, _ in os.walk(scratch):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o700)
                except OSError:
                    pass
        shutil.rmtree(scratch, ignore_errors=True)
    print("end=%s  status=%d" % (time.strftime("%Y-%m-%d %H:%M:%S"), status), flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
