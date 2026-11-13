"""Job broker: runs OUTSIDE the sandbox (in the launcher process) and is the only thing that talks to Slurm.

The sandbox cannot reach Slurm itself (the munge socket is hidden). Clients inside (sandbox/bin/cc) send one JSON
request per connection over a unix socket. The broker validates it, writes the job specification and the batch
script into <project>/.contained/run/ (read-only for the sandbox), and submits. Every job re-enters the same
sandbox on the compute node (jobwrap.py). Requests can never add sbatch options or shell code of their own:
the batch script is generated here from validated values, and commands are passed as argv lists.

Rule: the broker never writes into folders the sandbox can write to (symlink planting); only into .contained/.
"""
import hashlib
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time

from . import CLONE, __version__
from .config import ConfigError, mem_mb, time_min

MAX_REQUEST = 1 << 20
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# --env names refused even though they are only set inside the sandbox (second line of defence: they change how
# the loader, shells, apptainer or the sandbox tooling behave)
ENV_DENY_RE = re.compile(r"(LD_.*|PATH|BASH_ENV|ENV|IFS|PYTHONHOME|PYTHONSTARTUP|APPTAINER.*|SINGULARITY.*|"
                         r"CONTAINEDCLAUDE.*|SLURM_.*)", re.I)
ARRAY_RE = re.compile(r"(\d+(-\d+)?(:\d+)?)(,\d+(-\d+)?(:\d+)?)*(%\d+)?")
IMAGE_URI_RE = re.compile(r"(docker|oras|library)://[A-Za-z0-9._/:@+-]{1,300}")
JOBID_RE = re.compile(r"\d+")
KERNEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A notebook step without --kernel runs this in the job (in the image, where the kernels are): the notebook's
# kernel if installed there, else one of the same language (python3, ir first), else a refusal that lists them.
# A notebook saved on the host often names a kernel only the host has (e.g. one for a docker image).
# argv: the notebook, then the nbconvert command, which gets --ExecutePreprocessor.kernel_name after "nbconvert".
NB_KERNEL_PICK = r"""
import json, os, subprocess, sys
nb, cmd = sys.argv[1], sys.argv[2:]
try:
    meta = json.load(open(nb)).get("metadata") or {}
except (OSError, ValueError):
    os.execvp(cmd[0], cmd)
want = (meta.get("kernelspec") or {}).get("name") or ""
lang = ((meta.get("kernelspec") or {}).get("language") or (meta.get("language_info") or {}).get("name") or "").lower()
specs = json.loads(subprocess.run(["jupyter", "kernelspec", "list", "--json"], stdout=subprocess.PIPE,
                                  check=True).stdout)["kernelspecs"]
if not want or want in specs:
    os.execvp(cmd[0], cmd)
same = sorted(n for n, s in specs.items() if (s.get("spec") or {}).get("language", "").lower() == lang)
if not same:
    sys.exit("cc-run: the notebook's kernel %s (language %s) is not installed here; installed: %s. Choose one with "
             "cc-run --kernel NAME." % (want, lang or "not given", ", ".join(sorted(specs)) or "none"))
k = next((n for n in ("python3", "ir") if n in same), same[0])
print("cc-run: kernel %s is not installed here; using %s" % (want, k), flush=True)
os.execvp(cmd[0], cmd[:2] + ["--ExecutePreprocessor.kernel_name=" + k] + cmd[2:])
"""
SYS_PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


class RequestError(Exception):
    pass


def project_id(project):
    return hashlib.sha1(project.encode()).hexdigest()[:12]


def isfile_fresh(p):
    """os.path.isfile that survives NFS negative caching: a file just written by a job on another node can be
    "missing" on the login node for up to a minute. Listing the folder revalidates it."""
    if os.path.isfile(p):
        return True
    try:
        if os.path.basename(p) in os.listdir(os.path.dirname(p) or "."):
            return os.path.isfile(p) or True
    except OSError:
        pass
    return False


def array_size(spec):
    n = 0
    for part in spec.split("%")[0].split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?(?::(\d+))?", part)
        a, b, step = int(m.group(1)), int(m.group(2) or m.group(1)), int(m.group(3) or 1)
        if b < a or step < 1:
            raise RequestError("bad array range %r" % part)
        n += (b - a) // step + 1
    return n


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pid = project_id(cfg.project)
        self.tag = "containedclaude:" + self.pid
        c = cfg.contained_dir
        self.logs = os.path.join(c, "logs")
        self.run = os.path.join(c, "run", "jobs")
        self.state = os.path.join(c, "state")
        for d in (self.logs, self.run, self.state):
            os.makedirs(d, exist_ok=True)
        self.records = os.path.join(self.state, "jobs.tsv")
        self.lock = threading.Lock()
        self.submit_lock = threading.Lock()     # limit check + sbatch as one step (parallel requests)

    # ---- bookkeeping -----------------------------------------------------------------------------------
    def _records(self):
        """{jobid: array spec or ''} from state/jobs.tsv (the 6th column is missing in older records)."""
        recs = {}
        if os.path.exists(self.records):
            with open(self.records) as fh:
                for line in fh:
                    f = line.rstrip("\n").split("\t")
                    if f and JOBID_RE.fullmatch(f[0]):
                        recs[f[0]] = f[5] if len(f) > 5 else ""
        return recs

    def own_jobs(self):
        return set(self._records())

    def _record(self, jobid, name, gpu, spec_path, array=""):
        with self.lock, open(self.records, "a") as fh:
            fh.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (jobid, name, int(gpu), time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                   spec_path, array or ""))

    def queue(self):
        # -r: one row per array task (every task counts against the limits)
        out = subprocess.run(["squeue", "--me", "-h", "-r", "-o", "%F|%i|%j|%T|%M|%l|%C|%m|%b|%R|%k"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        if out.returncode != 0:
            raise RequestError("squeue failed: " + out.stderr.strip())
        own, rows = self.own_jobs(), []
        for line in out.stdout.splitlines():
            f = line.split("|")
            if len(f) < 11:
                continue
            if f[10] == self.tag or f[0] in own:
                rows.append(dict(zip(["array_id", "id", "name", "state", "elapsed", "limit", "cpus", "mem", "gres",
                                      "reason"], f[:10])))
        return rows

    # ---- validation ------------------------------------------------------------------------------------
    def _opts(self, o):
        cfg = self.cfg
        g = lambda k: cfg.get("jobs", k)
        name = str(o.get("name") or "job")
        if not NAME_RE.fullmatch(name):
            raise RequestError("job name must match [A-Za-z0-9._-]{1,64}")
        try:
            cpus = int(o.get("cpus") or g("cpus"))
            mem = str(o.get("mem") or g("mem")).strip().upper()
            tlim = str(o.get("time") or g("time")).strip()
            if not 1 <= cpus <= int(g("max_cpus")):
                raise RequestError("cpus must be 1..%s (max_cpus in .contained/config.ini)" % g("max_cpus"))
            if mem_mb(mem) > mem_mb(g("max_mem")) or mem_mb(mem) <= 0:
                raise RequestError("mem %s exceeds max_mem %s" % (mem, g("max_mem")))
            if time_min(tlim) > time_min(g("max_time")) or time_min(tlim) <= 0:
                raise RequestError("time %s exceeds max_time %s" % (tlim, g("max_time")))
        except ConfigError as e:
            raise RequestError(str(e))
        gpu = bool(o.get("gpu"))
        array = o.get("array")
        if array:
            array = str(array)
            if not ARRAY_RE.fullmatch(array):
                raise RequestError("bad --array (use e.g. 1-96 or 1-96%20)")
            if array_size(array) > int(g("max_array")):
                raise RequestError("array larger than max_array %s" % g("max_array"))
        deps = []
        own = self.own_jobs()
        for kind in ("after", "after_any"):
            for j in (p for v in o.get(kind) or [] for p in str(v).split(",")):     # repeated or 1,2
                j = j.strip().split("_")[0]
                if not JOBID_RE.fullmatch(j) or j not in own:
                    raise RequestError("--%s: %s is not a job of this project" % (kind.replace("_", "-"),
                                                                                  j or "an empty id"))
                deps.append(("afterok" if kind == "after" else "afterany") + ":" + j)
        env = {}
        for k, v in (o.get("env") or {}).items():
            if not ENV_KEY_RE.fullmatch(k) or len(str(v)) > 4096:
                raise RequestError("bad --env %r" % k)
            if ENV_DENY_RE.fullmatch(k):
                raise RequestError("--env %s is not allowed (it changes the loader, shell, apptainer or sandbox "
                                   "tooling); set it inside the job's command or script instead" % k)
            env[k] = str(v)
        return dict(name=name, cpus=cpus, mem=mem, time=tlim, gpu=gpu, array=array, deps=deps, env=env)

    def _image(self, image, cwd):
        """none, a registry URI, or a .sif path (relative paths: to the request's cwd; the config default: to the
        project)."""
        given = bool(image)
        image = str(image or self.cfg.get("jobs", "default_image") or "none")
        if image == "none" or IMAGE_URI_RE.fullmatch(image):
            return image
        base = cwd if given else self.cfg.project
        p = os.path.realpath(os.path.join(base, os.path.expanduser(image)))
        if not isfile_fresh(p):
            raise RequestError("image %s not found (use a .sif path, docker://..., or none)" % image)
        return p

    def _steps(self, req):
        cwd = os.path.realpath(str(req.get("cwd") or self.cfg.project))
        if req.get("argv"):
            argv = [str(x) for x in req["argv"]]
            return [{"argv": argv, "cwd": cwd}]
        files, args = req.get("files") or [], [str(x) for x in req.get("args") or []]
        if not files:
            raise RequestError("nothing to run (give files or a command after --)")
        if args and len(files) > 1:
            raise RequestError("arguments are only allowed with a single file")
        notebooks = [f for f in files if str(f).endswith(".ipynb")]
        kernel, exec_dir = req.get("kernel"), req.get("executed_dir")
        if (kernel or exec_dir) and not notebooks:
            raise RequestError("--kernel and --executed-dir are for notebooks (.ipynb files)")
        if kernel and not KERNEL_RE.fullmatch(str(kernel)):
            raise RequestError("bad --kernel %r (a kernel name: letters, digits, . _ -)" % kernel)
        if exec_dir:
            exec_dir = os.path.realpath(os.path.join(cwd, str(exec_dir)))
            if not any(exec_dir == w or exec_dir.startswith(w.rstrip(os.sep) + os.sep)
                       for w in map(os.path.realpath, self.cfg.write_paths())):
                raise RequestError("--executed-dir %s is not in the project or a write_paths folder" % exec_dir)
        else:
            exec_dir = os.path.join(self.cfg.project, self.cfg.get("jobs", "executed_dir"))
        steps = []
        for f in files:
            f = os.path.realpath(os.path.join(cwd, str(f)))
            if not isfile_fresh(f):
                raise RequestError("file not found: %s" % f)
            rel = os.path.relpath(f, self.cfg.project)
            out = rel.replace(os.sep, "__") if not rel.startswith("..") else os.path.basename(f)
            if f.endswith(".ipynb"):
                if os.path.join(exec_dir, out) == f:
                    raise RequestError("--executed-dir %s would overwrite %s itself" % (exec_dir, rel))
                argv = ["jupyter", "nbconvert", "--to", "notebook", "--execute", "--ExecutePreprocessor.timeout=-1",
                        "--output-dir", exec_dir, "--output", out, f]
                if kernel:
                    argv[2:2] = ["--ExecutePreprocessor.kernel_name=" + str(kernel)]
                else:
                    argv = ["python3", "-I", "-c", NB_KERNEL_PICK, f] + argv
            elif f.endswith(".py"):
                argv = ["python", f]
            elif f.endswith(".R"):
                argv = ["Rscript", f]
            elif f.endswith(".sh"):
                argv = ["bash", f]
            else:
                raise RequestError("unsupported file type %s (.ipynb .py .R .sh, or a command after --)" % f)
            steps.append({"argv": argv + args, "cwd": os.path.dirname(f), "label": rel})
        return steps

    # ---- limits ----------------------------------------------------------------------------------------
    @staticmethod
    def _throttle(array):
        return int(array.split("%")[1]) if array and "%" in array else 0

    def _footprint(self, q):
        """What this project's queued/running jobs can use at the same time: (gpu jobs, cpus, mem MB).
        Tasks of an array throttled with %N count N times at most."""
        recs, groups = self._records(), {}
        for r in q:
            groups.setdefault(r["array_id"], []).append(r)
        gpus, cpus, mem = 0, 0, 0.0
        for aid, rows in groups.items():
            k = self._throttle(recs.get(aid, ""))
            rows = sorted(rows, key=lambda r: r["state"] != "RUNNING")[:k] if k else rows
            for r in rows:
                if r["gres"] not in ("", "N/A", "(null)"):
                    gpus += 1
                try:
                    cpus += int(r["cpus"])
                except ValueError:
                    pass
                try:
                    mem += mem_mb(r["mem"].upper())
                except ConfigError:
                    pass
        return gpus, cpus, mem

    def _check_limits(self, o):
        g = lambda k: self.cfg.get("jobs", k)
        q = self.queue()
        n = array_size(o["array"]) if o["array"] else 1
        conc = min(n, self._throttle(o["array"]) or n)
        if len(q) + n > int(g("max_jobs")):
            raise RequestError("project has %d queued/running jobs (array tasks count singly); %d more would exceed "
                               "max_jobs %s" % (len(q), n, g("max_jobs")))
        gpus, cpus, mem = self._footprint(q)
        if o["gpu"] and int(g("max_gpu_jobs")) <= 0:
            raise RequestError("GPU jobs are not enabled for this project (max_gpu_jobs is 0; the user can raise it "
                               "in .contained/config.ini)")
        if o["gpu"] and gpus + conc > int(g("max_gpu_jobs")):
            raise RequestError("project has %d GPU jobs; %d more at a time would exceed max_gpu_jobs %s (wait, or "
                               "throttle the array with %%N)" % (gpus, conc, g("max_gpu_jobs")))
        if g("max_total_cpus") and cpus + conc * o["cpus"] > int(g("max_total_cpus")):
            raise RequestError("project jobs use %d CPUs; %d more at a time would exceed max_total_cpus %s (wait, or "
                               "throttle the array with %%N)" % (cpus, conc * o["cpus"], g("max_total_cpus")))
        if g("max_total_mem") and mem + conc * mem_mb(o["mem"]) > mem_mb(g("max_total_mem")):
            raise RequestError("project jobs use %dG memory; %dG more at a time would exceed max_total_mem %s (wait, "
                               "or throttle the array with %%N)" % (mem / 1024, conc * mem_mb(o["mem"]) / 1024,
                                                                    g("max_total_mem")))

    # ---- actions ---------------------------------------------------------------------------------------
    def submit(self, req):
        with self.submit_lock:
            return self._submit(req)

    def _submit(self, req):
        o = self._opts(req.get("opts") or {})
        image = self._image(req.get("image"), os.path.realpath(str(req.get("cwd") or self.cfg.project)))
        steps = self._steps(req)
        self._check_limits(o)
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        spec_path = os.path.join(self.run, "%s_%s.json" % (stamp, o["name"]))
        script_path = spec_path[:-5] + ".sh"
        spec = {"version": __version__, "project": self.cfg.project, "image": image, "gpu": o["gpu"],
                "steps": steps, "env": o["env"], "kind": str(req.get("kind") or "run")}
        with open(spec_path, "w") as fh:
            json.dump(spec, fh, indent=1)
        log = os.path.join(self.logs, "%x_%A_%a.out" if o["array"] else "%x_%j.out")
        lines = ["#!/bin/bash",
                 "#SBATCH -J " + o["name"],
                 "#SBATCH -o " + log,
                 "#SBATCH -t " + o["time"],
                 "#SBATCH -c %d" % o["cpus"],
                 "#SBATCH --mem=" + o["mem"],
                 "#SBATCH --comment=" + self.tag]
        part = self.cfg.get("jobs", "partition")
        if part:
            lines.append("#SBATCH -p " + part)
        if o["gpu"]:
            lines.append("#SBATCH --gres=" + self.cfg.get("jobs", "gpu_gres"))
        if o["array"]:
            lines.append("#SBATCH --array=" + o["array"])
        if o["deps"]:
            lines.append("#SBATCH --dependency=" + ",".join(o["deps"]))
            lines.append("#SBATCH --kill-on-invalid-dep=yes")    # a failed parent cancels it (not pending forever)
        for extra in self.cfg.lines("jobs", "extra_sbatch"):
            lines.append("#SBATCH " + extra)
        lines.append("exec %s -I %s %s" % (SYS_PYTHON, os.path.join(CLONE, "lib", "containedclaude", "jobwrap.py"),
                                            spec_path))
        with open(script_path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        out = subprocess.run(["sbatch", "--parsable", script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=120)
        if out.returncode != 0:
            raise RequestError("sbatch failed: " + out.stderr.strip())
        jobid = out.stdout.strip().split(";")[0]
        self._record(jobid, o["name"], o["gpu"], spec_path, o["array"])
        logfile = os.path.join(self.logs, ("%s_%s_*.out" if o["array"] else "%s_%s.out") % (o["name"], jobid))
        return {"jobid": jobid, "log": logfile, "image": image, "steps": len(steps)}

    def cancel(self, ids):
        own = self.own_jobs()
        bad = [i for i in ids if str(i).split("_")[0] not in own or not re.fullmatch(r"\d+(_\d+)?", str(i))]
        if bad:
            raise RequestError("not jobs of this project: %s" % " ".join(map(str, bad)))
        out = subprocess.run(["scancel"] + [str(i) for i in ids], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=60)
        return {"output": out.stdout.strip(), "returncode": out.returncode}

    def usage(self, ids):
        """What this project's jobs used (cgroup accounting, 30 s samples): per job (array tasks separately)
        state, elapsed, allocated CPUs, CPU time used and efficiency, peak memory, requested memory and memory
        efficiency. sacct's allocation line + batch step for finished jobs, sstat while a job runs."""
        own = self.own_jobs()
        ids = [str(i) for i in ids]
        foreign = [i for i in ids if i.split("_")[0] not in own]
        if foreign:
            raise RequestError("not this project's job(s): " + " ".join(foreign))
        if not ids:
            ids = sorted(own, key=int)[-50:]
        if not ids:
            return {"rows": []}
        out = subprocess.run(["sacct", "-n", "-P", "-j", ",".join(ids), "-o",
                              "JobID,JobName,State,ElapsedRaw,AllocCPUS,TotalCPU,ReqMem,MaxRSS,ExitCode,NodeList"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        if out.returncode != 0:
            raise RequestError("sacct failed: " + out.stderr.strip())
        jobs, batch = [], {}
        for line in out.stdout.splitlines():
            f = line.split("|")
            if len(f) < 10:
                continue
            if "." in f[0]:
                base, step = f[0].split(".", 1)
                if step == "batch":
                    batch[base] = f
            else:
                jobs.append(f)
        rows = []
        for f in jobs:
            jid, state, cpus = f[0], f[2].split()[0], int(f[4] or 0)
            elapsed = float(f[3] or 0)
            b = batch.get(jid)
            cpu_used = _secs(b[5]) if b else _secs(f[5])
            peak = _mb(b[7], cpus) if b else None
            if state == "RUNNING":
                st = subprocess.run(["sstat", "-n", "-P", "-j", jid + ".batch", "-o", "AveCPU,MaxRSS"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                                    timeout=60).stdout.strip().split("|")
                if len(st) == 2:
                    cpu_used, peak = _secs(st[0]), _mb(st[1], cpus)
            req = _mb(f[6], cpus)
            rows.append({"id": jid, "name": f[1], "state": state, "elapsed_s": elapsed, "cpus": cpus,
                         "cpu_used_s": cpu_used,
                         "cpu_eff": round(100.0 * cpu_used / (elapsed * cpus), 1) if elapsed and cpus else None,
                         "peak_mb": peak, "req_mb": req,
                         "mem_eff": round(100.0 * peak / req, 1) if peak is not None and req else None,
                         "exit": f[8], "nodes": f[9]})
        return {"rows": rows}

    # ---- cluster view: what sinfo/squeue show every user (Slurm's allocations, not measured load) ---------
    def nodes(self):
        out = subprocess.run(["sinfo", "-N", "-h", "-O", "NodeList:40,Partition:40,StateCompact:20,CPUsState:30,"
                              "Memory:20,AllocMem:20,Gres:200,GresUsed:200"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=60).stdout
        rows = []
        for line in out.splitlines():
            f = line.split()
            if len(f) < 8:
                continue
            cpus = f[3].split("/")          # allocated/idle/other/total
            rows.append({"node": f[0], "partition": f[1], "state": f[2], "cpus_alloc": cpus[0], "cpus": cpus[-1],
                         "mem_mb": f[4], "mem_alloc_mb": f[5], "gres": f[6], "gres_used": f[7]})
        return rows

    def cluster(self):
        out = subprocess.run(["squeue", "-h", "-r", "-o", "%i|%u|%P|%T|%M|%l|%C|%m|%b|%R|%k"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        if out.returncode != 0:
            raise RequestError("squeue failed: " + out.stderr.strip())
        keys = ["id", "user", "partition", "state", "elapsed", "limit", "cpus", "mem", "gres", "reason"]
        jobs = []
        for line in out.stdout.splitlines():
            f = line.split("|")
            if len(f) < 11:
                continue
            d = dict(zip(keys, f[:10]))
            d["mine"] = f[10] == self.tag
            jobs.append(d)
        return {"nodes": self.nodes(), "jobs": jobs}

    def gpu(self, node):
        """`nvidia-smi` on a GPU node over ssh (runs here, outside the sandbox, with the user's key). The node
        must be one Slurm lists with a gpu GRES (default: the first); the remote command is fixed."""
        gpu_nodes = sorted(n["node"] for n in self.nodes() if re.search(r"(^|,)gpu:", n["gres"]))
        if not gpu_nodes:
            raise RequestError("Slurm lists no node with a GPU")
        node = str(node or gpu_nodes[0])
        if node not in gpu_nodes:
            raise RequestError("%s is not a GPU node (GPU nodes: %s)" % (node, " ".join(gpu_nodes)))
        try:
            out = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                  "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR",
                                  "--", node, "nvidia-smi"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                 timeout=40)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RequestError("ssh %s failed: %s" % (node, e))
        if out.returncode != 0:
            raise RequestError("nvidia-smi on %s failed (exit %d): %s" %
                               (node, out.returncode, (out.stderr.strip() or out.stdout.strip())[:300]))
        return {"node": node, "output": out.stdout}

    def info(self):
        g = lambda k: self.cfg.get("jobs", k)
        return {"project": self.cfg.project, "project_id": self.pid, "version": __version__,
                "logs": self.logs, "executed_dir": os.path.join(self.cfg.project, g("executed_dir")),
                "defaults": {k: g(k) for k in ("cpus", "mem", "time", "default_image", "partition", "gpu_gres")},
                "limits": {k: g(k) for k in ("max_cpus", "max_mem", "max_time", "max_jobs", "max_gpu_jobs",
                                             "max_array", "max_total_cpus", "max_total_mem")},
                "write_paths": self.cfg.write_paths(), "image_binds": self.cfg.image_binds(),
                "nodes": self.nodes()}

    def handle(self, req):
        action = req.get("action")
        if action == "submit":
            return self.submit(req)
        if action == "queue":
            return {"rows": self.queue()}
        if action == "cancel":
            return self.cancel(req.get("ids") or [])
        if action == "usage":
            return self.usage(req.get("ids") or [])
        if action == "info":
            return self.info()
        if action == "cluster":
            return self.cluster()
        if action == "gpu":
            return self.gpu(req.get("node"))
        if action == "ping":
            return {"pong": True, "version": __version__}
        raise RequestError("unknown action %r" % action)


def _secs(t):
    """Seconds from Slurm's [D-]HH:MM:SS[.mmm] (also MM:SS); 0 for empty."""
    t = (t or "").strip()
    if not t:
        return 0.0
    days, rest = (t.split("-", 1) + [""])[:2] if "-" in t else ("0", t)
    parts = [float(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return int(days) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _mb(s, cpus=1):
    """Megabytes from Slurm's size strings ("1573708K", "32G", "16000M"; a trailing n = per node, c = per CPU);
    None for empty."""
    s = (s or "").strip()
    if not s:
        return None
    per_cpu = s.endswith("c")
    s = s.rstrip("nc")
    unit = {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2}.get(s[-1:].upper())
    try:
        val = float(s[:-1]) * unit if unit else float(s) / 1024.0      # bare numbers are KB (MaxRSS)
    except ValueError:
        return None
    return round(val * (cpus if per_cpu else 1), 1)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            data = self.rfile.readline(MAX_REQUEST + 1)
            if len(data) > MAX_REQUEST:
                raise RequestError("request too large")
            req = json.loads(data.decode("utf-8"))
            if not isinstance(req, dict):
                raise RequestError("request must be a JSON object")
            res = {"ok": True, "result": self.server.broker.handle(req)}
        except (RequestError, ValueError) as e:
            res = {"ok": False, "error": str(e)}
        except subprocess.TimeoutExpired as e:
            res = {"ok": False, "error": "timeout: %s" % e}
        except Exception as e:      # never let a bad request kill the broker
            res = {"ok": False, "error": "internal error: %s: %s" % (type(e).__name__, e)}
        try:
            self.wfile.write((json.dumps(res) + "\n").encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(cfg, sock_path):
    """Start the broker in a background thread; returns the server (call .shutdown())."""
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    old = os.umask(0o077)
    try:
        srv = _Server(sock_path, _Handler)
    finally:
        os.umask(old)
    srv.broker = Broker(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def request(sock_path, payload, timeout=180):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    s.sendall((json.dumps(payload) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())
