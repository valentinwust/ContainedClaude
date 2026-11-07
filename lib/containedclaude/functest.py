"""Functional self-test: checks that everything an agent SHOULD be able to do actually works.

Counterpart of doctor.py (which checks that the ways out are blocked). Runs INSIDE a normal session sandbox,
started by `containedclaude test` through project.run, so it uses exactly the path a real session uses.
Everything happens in a temporary folder <project>/.cc_selftest_<stamp>/ that is removed at the end
(unless --keep). argv[1]: JSON {"jobs", "image", "gpu", "build", "keep", "automount": [explicit shares]}.

Output is meant for humans: sections, one line per check (ok / FAIL / skip), timings, and a summary.
Exit status 0 if nothing failed.
"""
import glob
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ARGS = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
PROJECT = os.environ.get("CONTAINEDCLAUDE_PROJECT", os.getcwd())
ENVDOC_HEADING = "# Environment: ContainedClaude sandbox"     # first line of templates/CLAUDE_environment.md
CLONE = os.environ.get("CONTAINEDCLAUDE_CLONE", "")
PULL_URI = "docker://quay.io/biocontainers/seqtk:1.4--he4a0461_2"   # small, glibc-based

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if COLOR else s

counts = {"ok": 0, "FAIL": 0, "skip": 0}
failed = []
T0 = time.time()


def section(title):
    print("\n" + c("1", title))


def line(status, name, detail="", secs=None):
    counts[status] += 1
    if status == "FAIL":
        failed.append(name)
    mark = {"ok": c("32", "  ok  "), "FAIL": c("31;1", " FAIL "), "skip": c("33", " skip ")}[status]
    t = c("2", " %5.1fs" % secs) if secs is not None and secs >= 0.5 else ""
    d = c("2", "  " + detail) if detail else ""
    print("%s %s%s%s" % (mark, name, d, t))
    sys.stdout.flush()


def check(name, fn):
    """fn() returns detail string (success) or raises; returning None/False counts as failure."""
    t = time.time()
    try:
        r = fn()
        if r is False or r is None:
            line("FAIL", name, "", time.time() - t)
        elif isinstance(r, tuple) and r[0] == "skip":
            line("skip", name, r[1])
        else:
            line("ok", name, "" if r is True else str(r), time.time() - t)
    except Exception as e:     # noqa: BLE001  (any error is a failed check, shown with its message)
        msg = str(e).strip().splitlines()
        line("FAIL", name, (msg[-1] if msg else type(e).__name__)[:110], time.time() - t)


# run in an image job: what it sees of the node's name resolution, time zone and the job's variables
IMAGE_ENV_PROBE = ('{ grep -c "^nameserver" /etc/resolv.conf; grep -c localhost /etc/hosts; '
                   'echo "$USER|$OMP_NUM_THREADS|$XDG_CACHE_HOME|$(date +%Z)"; } > img_env 2>&1')


def image_env(td):
    assert wait_file(os.path.join(td, "img_env")), "no output"
    ns, hosts, env = (read(os.path.join(td, "img_env")).splitlines() + ["", "", ""])[:3]
    assert ns.isdigit() and int(ns) > 0, "no nameserver in /etc/resolv.conf (DNS fails)"
    assert hosts.isdigit() and int(hosts) > 0, "no localhost in /etc/hosts"
    user, threads, cache, tz = (env.split("|") + ["", "", "", ""])[:4]
    want = os.path.join(os.environ["CONTAINEDCLAUDE_PROJECT"], ".cache")
    assert user == os.environ.get("USER"), "USER is %r" % user
    assert threads.isdigit(), "OMP_NUM_THREADS is %r" % threads
    assert cache == want, "XDG_CACHE_HOME is %r" % cache
    assert tz == time.strftime("%Z"), "time zone %r instead of %r" % (tz, time.strftime("%Z"))
    return "%s nameserver, USER, %s threads, caches in the project, %s" % (ns, threads, tz)


def checksum_ok(image):
    import hashlib
    with open(image + ".sha256") as fh:
        digest, name = fh.read().split()
    h = hashlib.sha256()
    with open(image, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    assert name == os.path.basename(image), "names %s" % name
    assert digest == h.hexdigest(), "checksum does not match the image"
    return "%s.sha256 matches" % os.path.basename(image)


def sh(cmd, timeout=600, cwd=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                       timeout=timeout, cwd=cwd, shell=isinstance(cmd, str))
    return r.returncode, r.stdout


def must(cmd, timeout=600, cwd=None):
    rc, out = sh(cmd, timeout, cwd)
    if rc != 0:
        raise RuntimeError("exit %d: %s" % (rc, out.strip()[-200:]))
    return out


def guard_run(shell, cmd, cwd, path, inside=False, answer=None):
    """`cmd` in a clean bash or zsh with lib/shell-guard.sh sourced: (stdout, stderr or what the terminal
    showed). answer: the reply typed into a terminal (stdin and stderr a pty); None: no terminal."""
    env = dict(os.environ, PATH=path)
    env.pop("CONTAINEDCLAUDE", None)
    if inside:
        env["CONTAINEDCLAUDE"] = "1"
    script = '. "%s"; %s' % (os.path.join(CLONE, "lib", "shell-guard.sh"), cmd)
    argv = ["bash", "--norc", "--noprofile", "-c", script] if shell == "bash" else ["zsh", "-f", "-c", script]
    if answer is None:
        r = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        return r.stdout.strip(), r.stderr.strip()
    master, slave = os.openpty()
    try:
        os.write(master, (answer + "\n").encode())
        r = subprocess.run(argv, cwd=cwd, env=env, stdin=slave, stderr=slave, stdout=subprocess.PIPE,
                           universal_newlines=True, timeout=30)
        os.close(slave)
        slave, seen = None, b""
        while select.select([master], [], [], 0.2)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:         # EIO: the terminal's other side is closed and everything read
                break
            if not chunk:
                break
            seen += chunk
        return r.stdout.strip(), seen.decode("utf-8", "replace")
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)


def jobid(out):
    for l in out.splitlines():
        if l.startswith("submitted "):
            return l.split()[1]
    raise RuntimeError("no job id in: %s" % out.strip()[-200:])


def wait_file(p, secs=60):
    """Files written by jobs on other nodes can be invisible here for a while (NFS caching)."""
    end = time.time() + secs
    while True:
        try:
            os.listdir(os.path.dirname(p))      # revalidates the directory's cached entries
            if os.path.isfile(p):
                return True
        except OSError:
            pass
        if time.time() > end:
            return False
        time.sleep(2)


def read(p):
    with open(p) as fh:
        return fh.read().strip()


# ----------------------------------------------------------------------------------------------- session
def session_checks(td):
    section("Project folder (writable)")
    def files():
        p = os.path.join(td, "a.txt")
        with open(p, "w") as fh:
            fh.write("hello")
        os.rename(p, p + ".moved")
        assert read(p + ".moved") == "hello"
        os.makedirs(os.path.join(td, "sub", "deeper"))
        os.symlink("a.txt.moved", os.path.join(td, "link"))
        assert read(os.path.join(td, "link")) == "hello"
        shutil.rmtree(os.path.join(td, "sub"))
        return "create, rename, mkdir, symlink, delete"
    check("files and folders", files)
    def script():
        p = os.path.join(td, "s.sh")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\necho runs\n")
        os.chmod(p, 0o755)
        return must([p]).strip() == "runs" and "chmod +x and execute"
    check("executable scripts", script)
    def git():
        g = os.path.join(td, "repo")
        os.mkdir(g)
        with open(os.path.join(g, "f"), "w") as fh:
            fh.write("x\n")
        idn = ["-c", "user.name=selftest", "-c", "user.email=selftest@localhost"]
        must(["git", "init", "-q"], cwd=g)
        must(["git", "add", "f"], cwd=g)
        must(["git"] + idn + ["commit", "-qm", "test"], cwd=g)
        must(["git", "checkout", "-qb", "branch"], cwd=g)
        return "init, commit, branch"
    check("git", git if shutil.which("git") else lambda: ("skip", "git not installed"))
    if os.path.isdir(os.path.join(PROJECT, ".git")):
        check("git status of the project", lambda: must(["git", "status", "--short"], cwd=PROJECT) is not None
              and "works")

    section("Reading outside the project")
    home = os.path.expanduser("~")
    def homefile():
        for n in (".bashrc", ".bash_profile", ".profile"):
            p = os.path.join(home, n)
            if os.path.isfile(p):
                with open(p) as fh:
                    fh.read(100)
                return "read ~/%s" % n
        return "home listing: %d entries" % len(os.listdir(home))
    check("home directory", homefile)
    parent = os.path.dirname(PROJECT)
    check("next to the project", lambda: "%s: %d entries" % (parent, len(os.listdir(parent))))
    shares = [s for s in ARGS.get("automount", []) if os.path.isdir(s) and s != home]
    for s in shares[:6]:
        check("share %s" % s, lambda s=s: "%d entries" % len(os.listdir(s)))
    if not shares:
        line("skip", "network shares", "none configured besides ~ (sandbox.automount)")

    section("Temporary space")
    def tmp():
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x"), "wb") as fh:
            fh.write(os.urandom(1 << 20))
        shutil.rmtree(d)
        return "TMPDIR=%s (private, in memory)" % os.environ.get("TMPDIR", "/tmp")
    check("/tmp writable", tmp)

    section("Software")
    check("python", lambda: must([sys.executable, "-c", "import sys; print(sys.version.split()[0])"]).strip())
    for py in ("python3", "R", "Rscript"):
        p = shutil.which(py)
        if p and py != "R":
            check("%s on PATH" % py, lambda py=py: must([py, "--version"]).strip().splitlines()[0][:60])
    common = ["bash", "grep", "sed", "awk", "find", "sort", "tar", "gzip", "curl", "wget", "make", "git"]
    miss = [x for x in common if not shutil.which(x)]
    line("ok" if not miss else "skip", "standard tools", "all present" if not miss else "missing: " + " ".join(miss))

    section("Internet")
    check("DNS", lambda: "pypi.org -> %s" % socket.getaddrinfo("pypi.org", 443)[0][4][0])
    def https():
        with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=20) as r:
            return "HTTPS GET pypi.org: %d" % r.status
    check("HTTPS download", https)

    section("Claude Code / Codex")
    claude = shutil.which("claude")
    check("claude runs", (lambda: must([claude, "--version"], timeout=60).strip().splitlines()[-1][:60])
          if claude else lambda: ("skip", "claude not on PATH"))
    cfgdir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    def cfgw():
        p = os.path.join(cfgdir, ".selftest-%s" % os.urandom(3).hex())
        open(p, "w").close()
        os.unlink(p)
        return cfgdir
    check("contained Claude config writable (login, history)", cfgw if cfgdir else lambda: False)
    state = os.path.join(PROJECT, ".contained", "state")
    def with_project(pairs):        # the same files as the project's .contained/state/ entries (bound over)
        def fn():
            for rel, p in pairs:
                src = os.path.join(state, *rel.split("/"))
                assert os.path.samefile(p, src), "%s is not the project's .contained/state/%s" % (p, rel)
            return ", ".join(rel for rel, _ in pairs) + " are the project's"
        return fn
    check("transcripts, memory and prompt history stay with the project",
          with_project([("claude/projects", os.path.join(cfgdir, "projects")),
                        ("claude/history.jsonl", os.path.join(cfgdir, "history.jsonl"))]) if cfgdir else lambda: False)
    def own_mounts():
        # every per-project entry of the shared config (policy's lists) is its own mount inside: nothing of
        # another project behind it, nothing of this project visible to others
        sys.path.insert(0, os.path.join(CLONE, "lib"))
        from containedclaude import policy
        with open("/proc/self/mountinfo") as fh:
            mounted = {l.split()[4] for l in fh}
        names = policy.CLAUDE_CONFIG_PER_PROJECT + policy.CLAUDE_CONFIG_PER_PROJECT_FILES + policy.CLAUDE_CONFIG_PER_RUNTIME
        missing = [n for n in names if os.path.join(cfgdir, n) not in mounted]
        assert not missing, "shared, not per project: %s" % " ".join(missing)
        return "%d entries (incl. sessions, shell snapshots, hook env files) are own mounts" % len(names)
    check("per-project entries of the shared config are own mounts", own_mounts if cfgdir else lambda: False)
    def launcher_settings():
        # every Claude process here reads them from the config's settings.json (also the sessions `claude
        # remote-control` starts, which get no launcher flags); the user's own settings stay
        sys.path.insert(0, os.path.join(CLONE, "lib"))
        from containedclaude import policy, project
        from containedclaude.config import Config
        cfg = Config(PROJECT)
        st = json.loads(read(os.path.join(cfgdir, "settings.json")))
        assert os.path.samefile(os.path.join(cfgdir, "settings.json"), policy.settings_inside(cfg)), \
            "the config's settings.json is not the launcher's copy"
        hooks = json.dumps(st.get("hooks"))
        missing = [h for h in ("write_guard.py", "peer_guard.py", "oom_notice.py", "context_notice.py") if h not in hooks]
        assert not missing and hooks.count("peer_guard.py") == 2, "hooks missing: %s" % " ".join(missing)
        assert "RemoteTrigger" in st.get("permissions", {}).get("deny", []), "RemoteTrigger is not denied"
        no_mcp = not cfg.bool("sandbox", "mcp_from_user_config")
        assert (st.get("allowedMcpServers") == []) == no_mcp, "allowedMcpServers: %r" % st.get("allowedMcpServers")
        assert st.get("disableClaudeAiConnectors") is True or not no_mcp, "claude.ai connectors are loaded"
        mine = {"model": "m", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
                "permissions": {"allow": ["Read"], "deny": ["WebFetch", "RemoteTrigger"]}}
        got = project.session_settings(cfg, mine)
        assert got["model"] == "m" and got["permissions"] == mine["permissions"] \
            and got["hooks"]["PreToolUse"][0] == mine["hooks"]["PreToolUse"][0] \
            and len(got["hooks"]["PreToolUse"]) == 3, "the user's own settings were not kept: %r" % got
        for bad in ([], {"hooks": []}, {"hooks": {"Stop": {}}}, {"permissions": {"deny": "Bash"}}):
            try:
                project.session_settings(cfg, bad)
            except ValueError:
                continue
            raise AssertionError("settings Claude Code does not read were accepted: %r" % bad)
        return "hooks, RemoteTrigger denied, %s; the user's own settings kept" % (
            "no MCP servers" if no_mcp else "MCP servers from the config")
    check("the launcher's settings reach every Claude process", launcher_settings if cfgdir else lambda: False)
    if ARGS.get("unknown_folder") and cfgdir:     # made by `containedclaude test` on the host before the start
        n = ARGS["unknown_folder"]
        check("a new folder of the shared config (tool update) is per project",
              with_project([("claude/" + n, os.path.join(cfgdir, n))]))
    def envdoc():
        text = read(os.path.join(PROJECT, "CLAUDE_environment.md"))
        assert text.startswith(ENVDOC_HEADING), "it is not the sandbox description: " + text.splitlines()[0][:70]
        assert text == read(os.path.join(PROJECT, ".contained", "run", "CLAUDE_environment.md")), \
            "differs from .contained/run/CLAUDE_environment.md"
        assert "@CLAUDE_environment.md" in read(os.path.join(PROJECT, "CLAUDE.md")), "CLAUDE.md lacks the import"
        return "the sandbox description in place of the notice, imported by CLAUDE.md"
    check("agents get the environment description", envdoc)
    def envdoc_kept():          # read by a first sandbox while `test` started the project a second time
        got = ARGS.get("envdoc_kept")
        assert got == ENVDOC_HEADING, got
        return "a running sandbox kept it while the project was started again"
    check("the description stays while the project starts again",
          envdoc_kept if "envdoc_kept" in ARGS else lambda: ("skip", "not run by `containedclaude test`"))
    codex = shutil.which("codex")
    def codex_sandbox():
        # Codex's own sandbox (an inner layer: its bwrap + seccomp) must start inside ours and, in workspace-write
        # mode, write the project and reach the broker socket; no model call needed. Breaks when a Codex update
        # changes how its sandbox resolves /tmp or sockets (see DEVELOPMENT.md §5 step 2).
        rc, out = sh([codex, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "sh", "-c",
                      "echo ok > codex_sandbox_ok && cc-info >/dev/null && echo CC_OK"], timeout=120, cwd=td)
        if rc != 0 or "CC_OK" not in out:
            raise RuntimeError("exit %d: %s" % (rc, out.strip()[-200:]))
        return "writes the project and reaches the broker under its own sandbox"
    if not codex:                                   # Codex is an optional add-on: one skip line, no failure
        line("skip", "codex", "not installed (optional)")
    elif not os.environ.get("CODEX_HOME"):
        line("skip", "codex", "codex = no in the project config")
    else:
        check("codex runs", lambda: must([codex, "--version"], timeout=60).strip().splitlines()[-1][:60])
        check("codex sessions stay with the project",
              with_project([("codex/sessions", os.path.join(os.environ["CODEX_HOME"], "sessions"))]))
        def codex_mounts():
            sys.path.insert(0, os.path.join(CLONE, "lib"))
            from containedclaude import policy
            with open("/proc/self/mountinfo") as fh:
                mounted = {l.split()[4] for l in fh}
            names = policy.CODEX_HOME_PER_PROJECT + policy.CODEX_HOME_PER_PROJECT_FILES + policy.CODEX_HOME_PER_RUNTIME
            missing = [n for n in names if os.path.join(os.environ["CODEX_HOME"], n) not in mounted]
            assert not missing, "shared, not per project: %s" % " ".join(missing)
            return "%d entries (incl. shell snapshots, helper programs, server files) are own mounts" % len(names)
        check("per-project entries of the Codex home are own mounts", codex_mounts)
        check("codex's own sandbox works inside (workspace-write)", codex_sandbox)
    hook = os.path.join(CLONE, "sandbox", "hooks", "write_guard.py")
    def hook_ok():
        def rc(path):
            inp = json.dumps({"tool_name": "Write", "tool_input": {"file_path": path}})
            return subprocess.run([sys.executable, hook], input=inp, universal_newlines=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT).returncode
        assert rc(os.path.join(td, "x.txt")) == 0, "blocks a project file"
        assert rc(os.path.join(os.path.expanduser("~"), "x.txt")) == 2, "lets a home file through"
        assert rc(os.path.join(PROJECT, ".contained", "config.ini")) == 2, "lets a protected file through"
        return "lets project writes through, explains home and protected files"
    check("write hook", hook_ok)
    def peer_guard():
        # stand-in session records: one running (a listening socket), one gone; a transcript with an own agent
        d = tempfile.mkdtemp(dir="/tmp")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock, gone = os.path.join(d, "1.sock"), os.path.join(d, "2.sock")
            srv.bind(sock)
            srv.listen(4)
            os.makedirs(os.path.join(d, "sessions"))
            for n, name, path in (("1", "proj-peer", sock), ("2", "gone-peer", gone)):
                with open(os.path.join(d, "sessions", n + ".json"), "w") as fh:
                    json.dump({"name": name, "sessionId": "sid-" + n, "messagingSocketPath": path}, fh)
            transcript = os.path.join(d, "t.jsonl")
            with open(transcript, "w") as fh:
                fh.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Agent",
                                                               "input": {"name": "helper"}}]}}) + "\n")
            env = dict(os.environ, CLAUDE_CONFIG_DIR=d)
            guard = os.path.join(CLONE, "sandbox", "hooks", "peer_guard.py")
            def verdict(payload):
                out = subprocess.run([sys.executable, "-I", guard], input=json.dumps(payload), env=env,
                                     universal_newlines=True, stdout=subprocess.PIPE, timeout=30).stdout
                r = json.loads(out) if out.strip() else {}
                return ("blocked" if r.get("decision") == "block" or
                        (r.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny" else "passed")
            msg = '<cross-session-message from="%s" from-name="x" from-mode="prompting">\nhi\n</cross-session-message>'
            prompts = [("hello", "passed"), (msg % ("uds:" + sock), "passed"), (msg % "bridge:session_x", "blocked"),
                       (msg % "did:x", "blocked"), ("about " + msg % "bridge:session_x", "passed")]
            ref = hashlib.sha256(("session:" + sock).encode()).hexdigest()       # as ListAgents shows it
            sends = [("main", "main", "passed"), ("team-lead", "name", "passed"),
                     ("a1b2c3d4e5f6a7b8", "agent", "passed"),
                     ("proj-peer", "name", "passed"), ("proj-peer [%s]" % ref[:6], "name", "passed"),
                     (ref[:8], "name", "passed"), ("helper", "name", "passed"), ("uds:" + sock, "session", "passed"),
                     ("gone-peer", "name", "blocked"), ("someone-else", "name", "blocked"),
                     ("someone-else [484611]", "name", "blocked"), ("bridge:session_x", "session", "blocked"),
                     ("local_0123456789ab", "session", "blocked")]
            wrong = ["prompt %r: %s" % (p[:40], got) for p, want in prompts
                     for got in [verdict({"hook_event_name": "UserPromptSubmit", "prompt": p})] if got != want]
            wrong += ["to %r: %s" % (to, got) for to, kind, want in sends
                      for got in [verdict({"hook_event_name": "PreToolUse", "tool_name": "SendMessage",
                                           "transcript_path": transcript,
                                           "tool_input": {"to": to, "recipient_kind": kind, "message": "hi"}})]
                      if got != want]
            assert not wrong, "; ".join(wrong)
        finally:
            srv.close()
            shutil.rmtree(d, ignore_errors=True)
        return "%d messages in, %d sends out as expected" % (len(prompts), len(sends))
    check("messages only within the project (peer_guard)", peer_guard)
    def context_notice():
        # sample transcripts: the session on a model with modelSettings (100k), a subagent with the 200k fallback
        d = tempfile.mkdtemp(dir="/tmp")
        sid = "cn-test-%d" % os.getpid()
        try:
            def usage(path, tokens, model):
                with open(path, "a") as fh:
                    fh.write(json.dumps({"type": "assistant", "message": {"model": model, "usage": {
                        "input_tokens": 2, "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": tokens - 1002}}}) + "\n")
                    fh.write(json.dumps({"type": "user", "message": {"content": "x" * 70000}}) + "\n")
            with open(os.path.join(d, "settings.json"), "w") as fh:
                json.dump({"modelSettings": {"m-small": {"autoCompactWindow": 100000}}}, fh)
            main_t = os.path.join(d, sid + ".jsonl")
            os.makedirs(os.path.join(d, sid, "subagents"))
            sub_t = os.path.join(d, sid, "subagents", "agent-a1.jsonl")
            env = dict(os.environ, CLAUDE_CONFIG_DIR=d, CLAUDE_PROJECT_DIR=d)
            env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
            hook = os.path.join(CLONE, "sandbox", "hooks", "context_notice.py")
            def said(agent=None, result=""):
                payload = {"hook_event_name": "PostToolUse", "tool_name": "Read", "session_id": sid,
                           "transcript_path": main_t, "cwd": d, "tool_response": result}
                if agent:
                    payload.update(agent_id=agent, agent_type="general-purpose")
                out = subprocess.run([sys.executable, "-I", hook], input=json.dumps(payload), env=env,
                                     universal_newlines=True, stdout=subprocess.PIPE, timeout=30).stdout
                return ((json.loads(out) if out.strip() else {}).get("hookSpecificOutput") or {}).get(
                    "additionalContext", "")
            # compaction at window - 33k, the notice 25k before: 42k of the 100k window, 142k of the 200k one
            steps = [(main_t, 40000, "m-small", None, 0, False), (main_t, 43000, "m-small", None, 0, True),
                     (main_t, 45000, "m-small", None, 0, False),               # once per crossing
                     (sub_t, 140000, "m-big", "a1", 0, False), (sub_t, 143000, "m-big", "a1", 0, True),
                     (main_t, 20000, "m-small", None, 0, False),               # after a compaction ...
                     (main_t, 35000, "m-small", None, 30000, True),            # ... again, with a big result
                     (sub_t, 400000, "m-big", "a1", 0, False)]                 # past 200k: a 1M window
            wrong = []
            for i, (path, tokens, model, agent, result, want) in enumerate(steps):
                usage(path, tokens, model)
                got = said(agent, "x" * result)
                if bool(got) != want or (got and "starts at about %dk" % (67 if model == "m-small" else 167)
                                         not in got):
                    wrong.append("step %d: %r" % (i + 1, got[:60] or "silent"))
            assert not wrong, "; ".join(wrong)
        finally:
            shutil.rmtree(d, ignore_errors=True)
            for f in glob.glob("/tmp/.containedclaude-context/%s.*" % sid):
                os.unlink(f)
        return "warns once 25k before compaction, again after a drop (%d steps)" % len(steps)
    check("notice close to auto-compaction (context_notice)", context_notice)
    def shell_guard():
        fake = os.path.join(td, "guardbin")
        os.makedirs(fake)
        for n in ("claude", "codex", "containedclaude"):     # stand-ins: print their name and arguments
            with open(os.path.join(fake, n), "w") as fh:
                fh.write('#!/bin/sh\nprintf %%s %s; printf "|%%s" "$@"; echo\n' % n)
            os.chmod(os.path.join(fake, n), 0o755)
        path = fake + os.pathsep + os.environ.get("PATH", "")
        sub = os.path.join(td, "guardsub")                   # a sub-folder of this project
        os.makedirs(sub)
        warn, ask = "warning: running uncontained", "is a ContainedClaude project"
        cases = [   # cwd, command, inside a sandbox, answer typed (None: no terminal), stdout, in stderr
            ("/", "claude a 'b c'", False, None, "claude|a|b c", ""),
            (sub, "claude a 'b c'", False, None, "claude|a|b c", warn),
            (sub, "claude a", True, None, "claude|a", ""),
            (sub, "claude update", False, None, "claude|update", ""),
            (sub, "codex login", False, None, "codex|login", ""),
            (sub, "command claude a", False, None, "claude|a", ""),
            (sub, "claude a 'b c'", False, "", "containedclaude|run|--|a|b c", ask),
            (sub, "claude a", False, "u", "claude|a", ask),
            (sub, "codex x", False, "", "containedclaude|shell|--|codex|x", ask),     # (`codex a`: apply)
            ("/", "cclaude a", False, None, "containedclaude|run|--|a", ""),
            ("/", "cclaude server --name 'x y'", False, None, "containedclaude|server|--|--name|x y", ""),
            ("/", "cclaude server", True, None, "claude|remote-control", ""),
            ("/", "ccodex x", True, None, "codex|x", ""),
        ]
        shells = ["bash"] + (["zsh"] if shutil.which("zsh") else [])
        for shell in shells:
            for cwd, cmd, inside, answer, want, note in cases:
                got, err = guard_run(shell, cmd, cwd, path, inside, answer)
                where = "%s, %s%s%s" % (shell, "project" if cwd == sub else cwd, ", inside" if inside else "",
                                        ", typed %r" % answer if answer is not None else "")
                assert got == want, "%s: `%s` ran %r, not %r" % (where, cmd, got, want)
                assert (note in err) if note else not err, "%s: `%s` printed %r" % (where, cmd, err[-80:])
        return "%s: %d cases each (outside/inside, no terminal, Enter/u, subcommands, cclaude, ccodex, server)" % (
            ", ".join(shells), len(cases))
    check("shell guard (lib/shell-guard.sh)", shell_guard)
    def server_args():
        sys.path.insert(0, os.path.join(CLONE, "lib"))
        from containedclaude.cli import server_args, SERVER_CAPACITY
        cap, same = ["--capacity", str(SERVER_CAPACITY)], ["--spawn", "same-dir"]
        no = ["--no-create-session-in-dir"]
        cases = [([], same + cap + no), (["--name", "x"], same + cap + no), (["--spawn", "worktree"], cap + no),
                 (["--spawn=same-dir"], cap + no), (["--capacity", "2"], same + no), (["--capacity=2"], same + no),
                 (["--create-session-in-dir"], same + cap), (no, same + cap), (["-c"], []), (["--continue"], []),
                 (["--session-id", "s"], []), (["--spawn", "session"], []), (["--spawn=session"], [])]
        for args, extra in cases:
            want = ["remote-control"] + extra + args
            assert server_args(args) == want, "%s: %s, not %s" % (args, server_args(args), want)
        return "%d argument cases (--spawn same-dir, --capacity %d, --no-create-session-in-dir unless given " \
               "or refused)" % (len(cases), SERVER_CAPACITY)
    check("containedclaude server arguments", server_args)

    section("Job system (login node)")
    def info():
        d = json.loads(must(["cc-info"]))
        df = d.get("defaults", {})
        return "defaults: %s cpus, %s, %s, partition %s" % (df.get("cpus"), df.get("mem"), df.get("time"),
                                                            df.get("partition") or "(default)")
    check("cc-info", info)
    check("cc-jobs", lambda: must(["cc-jobs"]) is not None and "works")
    def cluster():
        out = must(["cc-cluster"]) or ""
        table = out.split("\n\n")[0].splitlines()      # the node table ends at the first blank line
        return out.startswith("NODE") and "%d nodes listed" % (len(table) - 1)
    check("cc-cluster", cluster)
    def gpu():
        rc, out = sh(["cc-gpu"])
        if "lists no node with a GPU" in out:
            return ("skip", "Slurm lists no GPU node")
        return rc == 0 and "NVIDIA-SMI" in out and out.splitlines()[0]
    check("cc-gpu (ssh to a GPU node)", gpu)


# ----------------------------------------------------------------------------------------------- jobs
def job_checks(td):
    section("Jobs (submitted to Slurm, run in the same sandbox on a compute node)")
    print(c("2", "       submitting test jobs; this takes a few minutes depending on the queue"))
    rel = os.path.relpath(td, PROJECT)
    probe = ("set -e; hostname > node; head -c 10 ~/.bashrc >/dev/null 2>&1 || ls ~ >/dev/null; echo home > home_ok; "
             "d=$(mktemp -d); dd if=/dev/zero of=$d/x bs=1M count=64 status=none; rm -r $d; "
             "df -Pk /tmp | awk 'NR==2{print int($4/1048576)}' > scratch_gb; "
             "command -v apptainer > apptainer_path; echo \"$CC_TEST_VAR\" > env_ok; "
             "head -n 1 \"$CONTAINEDCLAUDE_PROJECT/CLAUDE_environment.md\" > envdoc_head; echo marker-from-job")
    t = time.time()
    ids = {}
    try:
        ids["cmd"] = jobid(must(["cc-run", "--name", "cc_test_cmd", "--cwd", td, "--env", "CC_TEST_VAR=inside_ok",
                                 "--", "sh", "-c", probe]))
        ids["arr"] = jobid(must(["cc-run", "--name", "cc_test_arr", "--cwd", td, "--array", "1-3", "--",
                                 "sh", "-c", "echo $SLURM_ARRAY_TASK_ID > arr_$SLURM_ARRAY_TASK_ID"]))
        ids["after"] = jobid(must(["cc-run", "--name", "cc_test_after", "--cwd", td, "--after", ids["cmd"], "--",
                                   "sh", "-c", "test -f node && touch after_ok"]))
        ids["after2"] = jobid(must(["cc-run", "--name", "cc_test_after2", "--cwd", td, "--after",
                                    ids["cmd"] + "," + ids["arr"], "--",
                                    "sh", "-c", "test -f node && test -f arr_3 && touch after2_ok"]))
        ids["fail"] = jobid(must(["cc-run", "--name", "cc_test_fail", "--cwd", td, "--", "false"]))
        ids["dead"] = jobid(must(["cc-run", "--name", "cc_test_dead", "--cwd", td, "--after", ids["fail"], "--",
                                  "touch", "dead_ran"]))
        ids["sleep"] = jobid(must(["cc-run", "--name", "cc_test_cancel", "--cwd", td, "--", "sleep", "600"]))
        ids["waits"] = jobid(must(["cc-run", "--name", "cc_test_waits", "--cwd", td, "--after", ids["sleep"], "--",
                                   "true"]))       # pending until cc-cancel below, then cancelled with it
        if ARGS.get("gpu"):
            ids["gpu"] = jobid(must(["cc-run", "--name", "cc_test_gpu", "--gpu", "--cwd", td, "--",
                                     "sh", "-c", "nvidia-smi -L > gpu_ok"]))
        line("ok", "submit (command, array, dependencies%s)" % (", GPU" if "gpu" in ids else ""),
             " ".join(ids.values()), time.time() - t)
    except Exception as e:     # noqa: BLE001
        line("FAIL", "submit", str(e)[:110])
        return

    check("cc-jobs lists them", lambda: all(i in must(["cc-jobs"]) for i in ids.values()) and "all listed")
    def waiting():
        out = sh(["cc-log", ids["waits"]])[1]
        assert "is PENDING (Dependency)" in out, out.strip()[-110:]
        return "says it is waiting"
    check("cc-log of a job not started yet", waiting)
    check("cc-cancel", lambda: must(["cc-cancel", ids["sleep"]]) is not None and "cancelled " + ids["sleep"])

    t = time.time()
    rc, out = sh(["cc-wait", "--interval", "10", "--max", "30"] + [ids[k] for k in ids if k != "sleep"], 2000)
    line("ok" if rc in (0, 1) else "FAIL", "cc-wait", "exit %d (0 = all succeeded)" % rc, time.time() - t)
    def usage():
        rc, out = sh(["cc-usage", ids["cmd"]])
        rows = [l for l in out.splitlines() if l.startswith(ids["cmd"])]
        return rc == 0 and "PEAK-MEM" in out and rows and rows[0][:100]
    check("cc-usage (peak memory, CPU efficiency)", usage)

    check("job ran and wrote into the project",
          lambda: wait_file(os.path.join(td, "node")) and "on node " + read(os.path.join(td, "node")))
    check("job reads the home directory", lambda: wait_file(os.path.join(td, "home_ok")) and "yes")
    check("node-local /tmp scratch", lambda: wait_file(os.path.join(td, "scratch_gb"))
          and "%s GB free" % read(os.path.join(td, "scratch_gb")))
    check("apptainer available in jobs", lambda: wait_file(os.path.join(td, "apptainer_path"))
          and read(os.path.join(td, "apptainer_path")))
    check("job sees the sandbox description", lambda: wait_file(os.path.join(td, "envdoc_head"))
          and read(os.path.join(td, "envdoc_head")) == ENVDOC_HEADING and "CLAUDE_environment.md")
    check("--env reaches the job", lambda: wait_file(os.path.join(td, "env_ok"))
          and read(os.path.join(td, "env_ok")) == "inside_ok" and "CC_TEST_VAR set")
    check("array job (3 tasks)", lambda: all(wait_file(os.path.join(td, "arr_%d" % i)) for i in (1, 2, 3)) and "1-3")
    check("dependency (--after)", lambda: wait_file(os.path.join(td, "after_ok")) and "ran after its parent")
    check("dependency on two jobs (--after A,B)",
          lambda: wait_file(os.path.join(td, "after2_ok")) and "ran after both, one an array")

    def dead():
        rows = [l.split() for l in sh(["cc-usage", ids["dead"]])[1].splitlines()]
        state = [r[2] for r in rows if len(r) > 2 and r[0] == ids["dead"]]
        assert not os.path.exists(os.path.join(td, "dead_ran")), "it ran although its parent failed"
        assert state and state[0] == "CANCELLED", "state %s" % (state[0] if state else "unknown")
        out = sh(["cc-log", ids["dead"]])[1]
        assert "never started: CANCELLED" in out, "cc-log: " + out.strip()[-100:]
        return "cancelled, not left pending; cc-log says it never started"
    check("dependency on a failed job", dead)
    check("cc-log", lambda: "marker-from-job" in must(["cc-log", ids["cmd"]]) and "shows the job output")
    if "gpu" in ids:
        check("GPU job", lambda: wait_file(os.path.join(td, "gpu_ok")) and read(os.path.join(td, "gpu_ok"))[:70])

    section("Containers (in jobs)")
    t = time.time()
    rc, out = sh(["cc-pull", "--wait", PULL_URI, os.path.join(rel, "seqtk.sif")], 2400)
    sif = os.path.join(td, "seqtk.sif")
    if rc == 0 and wait_file(sif):
        line("ok", "cc-pull docker image", PULL_URI.split("/")[-1], time.time() - t)
        script = "seqtk 2>&1 | grep Version > img_ok; " + IMAGE_ENV_PROBE
        check("run in pulled image", lambda: must(["cc-run", "--wait", "--name", "cc_test_img", "--cwd", td,
                                                   "--image", sif, "--", "sh", "-c", script], 2400) is not None
              and wait_file(os.path.join(td, "img_ok")) and read(os.path.join(td, "img_ok")))
        check("image job: name resolution and variables as without an image", lambda: image_env(td))
        check("cc-pull writes the checksum", lambda: checksum_ok(sif))
    else:
        line("FAIL", "cc-pull docker image", out.strip()[-110:], time.time() - t)
    if ARGS.get("build"):
        os.makedirs(os.path.join(td, "img"))
        with open(os.path.join(td, "img", "img.def"), "w") as fh:
            fh.write("Bootstrap: docker\nFrom: debian:bookworm-slim\n%post\n"
                     "    apt-get update -qq && apt-get install -y -qq --no-install-recommends samtools >/dev/null\n")
        t = time.time()
        rc, out = sh(["cc-build", "--wait", os.path.join(rel, "img", "img.def")], 4000)
        built = os.path.join(td, "img", "img.sif")
        if rc == 0 and wait_file(built):
            line("ok", "cc-build (debian + samtools)", "img/img.sif", time.time() - t)
            check("cc-build writes the checksum", lambda: checksum_ok(built))
            check("run in built image", lambda: must(["cc-run", "--wait", "--name", "cc_test_built", "--cwd", td,
                                                      "--image", built, "--", "sh", "-c",
                                                      "samtools --version | head -1 > built_ok"], 2400) is not None
                  and wait_file(os.path.join(td, "built_ok")) and read(os.path.join(td, "built_ok")))
        else:
            line("FAIL", "cc-build (debian + samtools)", out.strip()[-110:], time.time() - t)
    else:
        line("skip", "cc-build", "add --build (pulls debian, ~5 min)")
    img = ARGS.get("image")
    if img:
        check("command in %s" % os.path.basename(img),
              lambda: must(["cc-run", "--wait", "--name", "cc_test_userimg", "--cwd", td, "--image", img, "--",
                            "sh", "-c", "python3 --version > userimg_ok 2>&1 || echo ran > userimg_ok"], 2400)
              is not None and wait_file(os.path.join(td, "userimg_ok")) and read(os.path.join(td, "userimg_ok")))
        nb = {"cells": [{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                         "source": ["open('nb_ok', 'w').write('notebook ran')"]}],
              "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
              "nbformat": 4, "nbformat_minor": 5}
        with open(os.path.join(td, "cc_selftest_nb.ipynb"), "w") as fh:
            json.dump(nb, fh)
        def notebook():
            must(["cc-run", "--wait", "--name", "cc_test_nb", "--image", img,
                  os.path.join(rel, "cc_selftest_nb.ipynb")], 2400)
            assert wait_file(os.path.join(td, "nb_ok")), "notebook did not run (does the image have jupyter?)"
            return "executed copy in the jobs' executed folder"
        check("notebook in %s" % os.path.basename(img), notebook)
    else:
        line("skip", "your own image / notebooks", "add --image SIF (e.g. the project's analysis image)")


def cleanup_executed():
    try:
        d = json.loads(must(["cc-info"])).get("executed_dir")
    except Exception:          # noqa: BLE001
        d = None
    for base in filter(None, [d, os.path.join(PROJECT, "jobs", "executed")]):
        if os.path.isdir(base):
            for f in os.listdir(base):
                if "cc_selftest_nb" in f:
                    try:
                        os.unlink(os.path.join(base, f))
                    except OSError:
                        pass


def main():
    print(c("1", "ContainedClaude functional test") + c("2", "  (what SHOULD work; `containedclaude doctor` "
                                                           "checks what must be blocked)"))
    print(c("2", "project %s" % PROJECT))
    td = os.path.join(PROJECT, ".cc_selftest_%s" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(td)
    try:
        session_checks(td)
        if ARGS.get("jobs"):
            job_checks(td)
        else:
            section("Jobs and containers")
            line("skip", "jobs, arrays, dependencies, containers", "add --jobs (a few minutes)")
    finally:
        if ARGS.get("keep"):
            print(c("2", "\nkept %s" % td))
        else:
            shutil.rmtree(td, ignore_errors=True)
            cleanup_executed()
    n = counts
    print("\n%s  %d ok, %d failed, %d skipped  (%.0fs)" % (
        c("32;1", "EVERYTHING WORKS") if not n["FAIL"] else c("31;1", "PROBLEMS FOUND"),
        n["ok"], n["FAIL"], n["skip"], time.time() - T0))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
