"""containedclaude command line."""
import argparse
import os
import sys

USAGE = """containedclaude: Claude Code in a sandbox that can only write to one project folder.

  containedclaude init DIR [--force] [--write PATH ...]
                                        set up DIR (creates DIR/.contained_claude.sh, .contained/, CLAUDE_environment.md);
                                        --write: extra writable folders (e.g. data shared by several users' projects)
  containedclaude add-write DIR PATH... make more folders writable for an existing project (same checks)
  containedclaude update [DIR] [--check] [--no-pull]
                                        git pull the clone, update the host's Claude Code and Codex (contained
                                        sessions run those), quick test + doctor if anything changed;
                                        --check: only compare with the latest; --no-pull: leave the clone
  containedclaude [run] [DIR] [-- claude args]
                                        start Claude in the sandbox for the project containing DIR (default: cwd)
  containedclaude server [DIR] [-- args]
                                        the Remote Control server in that sandbox (`claude remote-control`): it
                                        starts contained sessions from claude.ai in the project folder (--spawn
                                        same-dir; worktree mode is not supported), at most %d at once unless
                                        args give --capacity N (they share the sandbox's limits); a restart
                                        takes over the last run's first session; keep it in tmux
  containedclaude doctor [DIR] [--jobs] [--gpu] [--image SIF]
                                        verify the sandbox from the inside (interactive; --jobs: also as Slurm jobs)
  containedclaude test [DIR] [--jobs] [--build] [--gpu] [--image SIF] [--keep]
                                        functional test: check that everything that SHOULD work does
                                        (files, git, reading, internet, Claude, broker; --jobs: Slurm + containers)
  containedclaude admin [-- claude args]
                                        Claude with the shared contained config writable (/login, plugins, settings)
  containedclaude admin codex [args]    the same for Codex (its config, skills and plugins are read-only in projects)
  containedclaude shell [DIR] [-- cmd args]
                                        a shell (or command) in the same sandbox, with cc-* job commands working
  containedclaude env [DIR]             regenerate DIR's CLAUDE_environment.md (a notice for agents outside the
                                        sandbox; inside, .contained/run/CLAUDE_environment.md is shown in its place)
                                        from its config and add missing .gitignore entries (caches, executed
                                        notebooks, CLAUDE_environment.md)
"""
# sessions a Remote Control server runs at once: they share one sandbox's limits, and Claude Code's own default
# (32) would need more processes+threads together than session_tasks allows
SERVER_CAPACITY = 6
USAGE %= SERVER_CAPACITY


def server_args(args):
    """`claude remote-control` with args, plus `--spawn same-dir` (worktree mode is not supported: README) and
    `--capacity SERVER_CAPACITY` unless args give their own or ask for a mode in which Claude Code refuses them
    (--continue and --session-id refuse both, --spawn session refuses --capacity and is that one session)."""
    names = [a.split("=", 1)[0] for a in args]
    spawn = [a.partition("=")[2] or (args[i + 1] if i + 1 < len(args) else "")
             for i, a in enumerate(args) if names[i] == "--spawn"]
    if set(names) & {"-c", "--continue", "--session-id"}:
        return ["remote-control"] + args
    extra = [] if spawn else ["--spawn", "same-dir"]
    if "session" not in spawn and "--capacity" not in names:
        extra += ["--capacity", str(SERVER_CAPACITY)]
    return ["remote-control"] + extra + args


def _root_or_fail(path):
    """The project root for a folder argument, or None after printing why: unreadable (expired Kerberos ticket
    on the SDS), missing, or not set up."""
    from . import project
    problem = project.access_problem(path)
    root = None if problem else project.find_root(path)
    if not root:
        print("containedclaude: %s" % (problem or "no project found at %s (run `containedclaude init DIR` first)"
                                       % path), file=sys.stderr)
    return root


def _envdoc_kept(root):
    """For `test`: a sandbox of the project that runs while the project is started again keeps the sandbox
    description (it must not see the notice that is on disk). Returns its first line, or the problem."""
    import subprocess
    import tempfile
    from . import CLONE, policy
    print("containedclaude: test: starting the project twice at once (a few seconds)", file=sys.stderr)
    launcher = os.path.join(CLONE, "bin", "containedclaude")
    doc = os.path.join(root, policy.ENVDOC)
    with tempfile.TemporaryFile("w+") as err:
        try:
            p = subprocess.Popen([launcher, "shell", root, "--", "sh", "-c", 'echo ready; read x; head -n 1 "$1"',
                                  "sh", doc], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                 universal_newlines=True)
            if p.stdout.readline().strip() != "ready":
                p.kill()
                p.wait()
                err.seek(0)
                return "the first sandbox did not start: " + (err.read().strip().splitlines() or ["?"])[-1]
            second = subprocess.run([launcher, "shell", root, "--", "true"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300).returncode
            out, _ = p.communicate("\n", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "could not run: %s" % e
    if second:
        return "the second start failed (exit %d)" % second
    return out.strip() or "(nothing read)"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import compat, project, doctor
    # before anything else runs a program: PATH etc. without folders a sandbox can write to
    compat.harden_host_env()
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv.pop(0) if argv and argv[0] in ("init", "run", "doctor", "test", "admin", "env", "shell",
                                              "add-write", "update", "server") else "run"

    claude_args = []
    if "--" in argv:
        i = argv.index("--")
        argv, claude_args = argv[:i], argv[i + 1:]

    if cmd == "init":
        p = argparse.ArgumentParser(prog="containedclaude init")
        p.add_argument("dir")
        p.add_argument("--force", action="store_true", help="set up even if Claude files exist (backs them up)")
        p.add_argument("--write", action="append", default=[], metavar="PATH",
                       help="extra writable folder (write_paths in .contained/config.ini; repeatable), e.g. a "
                            "data folder shared by several users' projects")
        a = p.parse_args(argv)
        try:
            return project.init(a.dir, force=a.force, write_paths=a.write)
        except project.InitError as e:
            print("containedclaude: %s" % e, file=sys.stderr)
            return 1
    if cmd == "update":
        p = argparse.ArgumentParser(prog="containedclaude update",
                                    description="Update ContainedClaude (`git pull --ff-only` in the clone, if it "
                                                "is clean), then the host's Claude Code and Codex (`claude update`, "
                                                "`codex update`), which contained sessions run.")
        p.add_argument("--check", action="store_true", help="only compare installed and latest versions")
        p.add_argument("--no-pull", action="store_true", help="leave the clone as it is")
        p.add_argument("dir", nargs="?", default=os.getcwd(),
                       help="project for the quick self-check after a change (default: the current folder's "
                            "project, else a throw-away project below your home)")
        a = p.parse_args(argv)
        from . import updates
        return updates.update_all(check_only=a.check, project=a.dir, pull=not a.no_pull, argv=argv)
    if cmd == "add-write":
        p = argparse.ArgumentParser(prog="containedclaude add-write",
                                    description="Make more folders writable for an existing project (same checks "
                                                "as `init --write`); takes effect at the next start.")
        p.add_argument("dir", help="the project (or any folder inside it)")
        p.add_argument("paths", nargs="+", metavar="PATH")
        a = p.parse_args(argv)
        try:
            return project.add_write_paths(a.dir, a.paths)
        except project.InitError as e:
            print("containedclaude: %s" % e, file=sys.stderr)
            return 1
    if cmd == "doctor":
        p = argparse.ArgumentParser(prog="containedclaude doctor")
        p.add_argument("dir", nargs="?")
        p.add_argument("--jobs", action="store_true", help="also submit probe jobs and wait for them")
        p.add_argument("--gpu", action="store_true", help="also a GPU job (nvidia-smi)")
        p.add_argument("--image", help="image for the image probe (default: the project's default_image)")
        a = p.parse_args(argv)
        return doctor.doctor(a.dir, jobs=a.jobs, gpu=a.gpu, image=a.image)
    if cmd == "test":
        p = argparse.ArgumentParser(prog="containedclaude test",
                                    description="Check that everything that should work in the sandbox works.")
        p.add_argument("dir", nargs="?", default=os.getcwd())
        p.add_argument("--jobs", action="store_true", help="also jobs: command, array, dependency, cancel, log, "
                       "pull + run a container (a few minutes)")
        p.add_argument("--build", action="store_true", help="also build an image (implies --jobs, ~5 more min)")
        p.add_argument("--gpu", action="store_true", help="also a GPU job (implies --jobs)")
        p.add_argument("--image", help="also run a command and a notebook in this image (implies --jobs)")
        p.add_argument("--keep", action="store_true", help="keep the test folder .cc_selftest_*")
        a = p.parse_args(argv)
        root = _root_or_fail(a.dir)
        if not root:
            return 1
        from .config import Config
        from . import CLONE
        image = os.path.abspath(a.image) if a.image and "://" not in a.image else a.image
        args = {"jobs": a.jobs or a.build or a.gpu or bool(a.image), "build": a.build, "gpu": a.gpu,
                "image": image, "keep": a.keep, "automount": Config(root).automount_explicit()}
        import json
        import shutil
        from . import policy
        args["envdoc_kept"] = _envdoc_kept(root)
        # a folder no list knows (as a tool update adds them): the session must get the project's own
        unknown = os.path.join(policy.CLAUDE_CONFIG_DIR, policy.TEST_UNKNOWN_PREFIX + os.urandom(3).hex())
        try:
            os.mkdir(unknown, 0o700)
            args["unknown_folder"] = os.environ[policy.TEST_UNKNOWN_ENV] = os.path.basename(unknown)
        except OSError:
            unknown = None
        try:
            return project.run(root, [], command=["/usr/bin/python3", "-I",
                                                  os.path.join(CLONE, "lib", "containedclaude", "functest.py"),
                                                  json.dumps(args)])
        finally:
            if unknown:
                os.environ.pop(policy.TEST_UNKNOWN_ENV, None)
                shutil.rmtree(unknown, ignore_errors=True)
                shutil.rmtree(os.path.join(root, ".contained", "state", "claude", os.path.basename(unknown)),
                              ignore_errors=True)
    if cmd == "admin":
        if argv and argv[0] == "codex":     # Codex with its home writable: plugins, skills, config.toml
            return project.admin([], command=argv + claude_args)
        return project.admin(claude_args + argv)
    if cmd == "env":
        from . import policy
        from .config import Config
        root = _root_or_fail(argv[0] if argv else os.getcwd())
        if not root:
            return 1
        cfg = Config(root)
        problems = project.start_problems(root, cfg)
        if problems:
            print("containedclaude: %s" % "; ".join(problems), file=sys.stderr)
            return 1
        written = project.write_envdoc(cfg)
        for p, what in ((os.path.join(cfg.project, policy.ENVDOC), "the notice for agents outside the sandbox"),
                        (policy.envdoc_inside(cfg), "what agents see there inside")):
            print("%s %s (%s)" % ("wrote" if p in written else "up to date:", p, what))
        project.ensure_gitignore(cfg)
        return 0
    target = os.getcwd()
    # a folder argument: an existing folder, or something written as a path (a folder that cannot be read,
    # e.g. on the SDS without a Kerberos ticket, must still count as one, so that run() can say why)
    looks_like_dir = lambda a: not a.startswith("-") and (os.path.isdir(a) or os.sep in a or a in (".", ".."))
    if cmd == "shell":
        if argv and looks_like_dir(argv[0]):
            target = argv.pop(0)
        return project.run(target, [], command=(argv + claude_args) or ["bash", "--norc", "-i"])
    # run, server (the Remote Control server: `claude remote-control`, which project.run recognises)
    if argv and looks_like_dir(argv[0]):
        target = argv.pop(0)
    return project.run(target, server_args(argv + claude_args) if cmd == "server" else argv + claude_args)
