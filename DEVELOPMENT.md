# ContainedClaude: developer / maintenance notes

Written for whoever maintains this, most likely a future Claude session debugging something that broke. Read this
first, then the code: everything is small, Python standard library only, Python 3.6+ (the system python on EL9
compute nodes is 3.9; the user's PATH python is miniconda 3.12, so both must work). The launcher itself runs
with `#!/usr/bin/python3 -I` (never a python from PATH: see §5.8).

User-facing docs: README.md (usage), EXPLANATIONS.md (what is allowed/blocked and why).

---------------------------------------------------------------------------------------------------------------
## 1. Goal and threat model

An AI agent (Claude Code, its sub-agents, Codex, and every program they start) should be able to read everything
the user can read, write only inside ONE project folder, run Slurm jobs and apptainer images, and nothing else
with lasting effect. The adversary is a confused or prompt-injected agent, not a kernel exploit. We aim to remove
every *easy* route out (verified on curry, see §8), keep the rules identical for jobs, and keep hooks as
helpful messages rather than enforcement.

Explicitly NOT covered (documented to the user in EXPLANATIONS "Not covered"): network (open: Claude needs the
API), services that accept connections without a credential, code from the project the user later runs
unsandboxed, kernel bugs, resource use inside limits.

**This is not perfect containment, and the docs must keep saying so.** Reviews keep finding ways out. README and
EXPLANATIONS tell users not to rely on the sandbox alone; don't remove that note or make stronger claims anywhere.

**This repository is public: never write attack details into it** (docs, code comments, commit messages,
tests). No recipes, no "verified attack" stories, no list of known open weaknesses. Describe what a protection
does, not how to get around it or what it doesn't cover. The maintainer keeps such details privately, outside
the repository.

---------------------------------------------------------------------------------------------------------------
## 2. Layout

```
bin/containedclaude            entry point; puts lib/ on sys.path, calls cli.main
lib/containedclaude/
  __init__.py                  __version__, CLONE (= this repo, resolved via __file__)
  cli.py                       subcommands: init | add-write | run (default) | server | shell | doctor | test |
                               admin | env | update
  config.py                    defaults + site.ini + <project>/.contained/config.ini; hide/protect/expose lists;
                               mem/time parsers
  policy.py                    THE sandbox: bwrap argument list for modes session/admin/job, outer unshare,
                               seccomp BPF, automount, environment cleaning, shims
  project.py                   init (+--force, backups), safe_write, CLAUDE_environment.md rendering, the
                               interactive launcher run(), admin(), post-session change report
  broker.py                    unix-socket server (thread in the launcher, OUTSIDE the sandbox); validates job
                               requests, writes spec+batch script, calls sbatch/squeue/scancel/sacct/sinfo
  jobwrap.py                   runs on the compute node as the batch step; re-enters the sandbox (mode job) and
                               runs the steps, with apptainer if an image is given
  probe.py                     runs INSIDE a sandbox; reports what is reachable (JSON line CC_PROBE_RESULT ...)
  doctor.py                    runs probe.py in a session sandbox and as jobs; prints pass/FAIL table
                               (= what must be BLOCKED is blocked); creates a throw-away .cc-doctor-* nested
                               repo with a submodule git dir for the "rename pinned folder" checks
  functest.py                  `containedclaude test`: runs INSIDE a normal session (via project.run) and
                               checks what SHOULD work (files, git, reading, internet, claude/codex, broker;
                               --jobs/--build/--gpu/--image: jobs and containers). Human-readable output;
                               works in <project>/.cc_selftest_<stamp>/, removed afterwards
  nsinit.py                    outer layer of SESSIONS (instead of util-linux unshare): user+mount namespace via
                               ctypes, private propagation, the session's size-limited tmpfs (tmp_size) mounted
                               ON /tmp with /var/tmp and /dev/shm as sub-folders of it (bwrap 0.6 has no
                               --size), the broker socket and the project's runtime folder kept below it
                               (--keep), a pid namespace whose pids start at a random base (--pid-base: it
                               stays as init inside and as waiter outside; bwrap runs without --unshare-pid)
  compat.py                    trusted host environment (harden_host_env, tool(): §5.8) and
                               feature detection from each tool's --help (bwrap, unshare, apptainer, claude):
                               required flags -> refuse to start, optional flags -> left out + warning
  updates.py                   update notice (clone, claude, codex), `containedclaude update`, self-test after
                               a change (§3 step 4)
lib/shell-guard.sh             optional (install.sh --shell-guard), sourced by ~/.bashrc / ~/.zshrc: the shell
                               functions claude, codex (ask in a contained project), cclaude (`cclaude server`
                               = `containedclaude server`), ccodex; checked
                               by functest ("shell guard") with stand-ins, in bash and zsh, with and without a
                               terminal
sandbox/bin/cc (+ cc-run cc-wait cc-jobs cc-cancel cc-log cc-build cc-pull cc-info cc-cluster cc-gpu cc-usage
                               symlinks)
                               client used INSIDE the sandbox; talks JSON to the broker socket
sandbox/shim                   stand-in bound over sbatch/ssh/apptainer/... inside the sandbox (prints advice, exit 126)
sandbox/hooks/write_guard.py   Claude PreToolUse hook for Write/Edit/...: path-based explanation only
sandbox/hooks/oom_notice.py    Claude PostToolUse hook for Bash: tells the agent when the session's memory
                               scope killed a process (reads the scope's memory.events)
sandbox/hooks/peer_guard.py    Claude UserPromptSubmit + PreToolUse SendMessage hook: messages between sessions
                               only within the project (§5.10)
templates/                     config.ini, contained_claude.sh (project stub), CLAUDE_environment.md (the
                               sandbox description), CLAUDE_environment_outside.md (the notice on disk)
site.curry.ini                 committed curry defaults; copied to site.ini by install.sh / init if missing
site.example.ini / site.ini    generic template / the active local copy (gitignored)
install.sh [--shell-guard] [TEMPLATE]
                               symlink bin/containedclaude into ~/.local/bin, create site.ini (default from
                               site.curry.ini); --shell-guard: one marked line in the rc file of $SHELL (bash,
                               zsh) that sources lib/shell-guard.sh (replaces an older one, keeps a symlinked rc;
                               without the flag, one line on how to add it while that line is missing)
uninstall.sh [--state]         remove that symlink (only if it points into this clone) and the marked shell guard
                               line from ~/.bashrc / ~/.zshrc; --state: also the known
                               entries of STATE (claude-config, codex-home, registry, caches; other files and
                               the folder stay), refused while sessions run. Clone and projects untouched.
LICENSE                        MIT
```

Per project (created by `init`):
```
DIR/.contained_claude.sh       stub: exec <clone>/bin/containedclaude run <its own dir> "$@"
DIR/.contained/config.ini      project config (read-only inside sandbox)
DIR/.contained/{logs,run,state,VERSION,.gitignore,backup-*}
   logs/<name>_<jobid>.out     Slurm output (written by slurmd OUTSIDE the sandbox -> must be in a ro folder)
   run/user-settings.json      the contained config's settings.json plus the launcher's (§3, 4), read-only over
                               it inside the project's sandbox
   run/mcp.json                empty MCP config (--mcp-config); run/settings.json: admin's --settings file
   run/CLAUDE_environment.md   the sandbox description, bound read-only over DIR/CLAUDE_environment.md inside
   run/jobs/<stamp>_<name>.{json,sh}  job spec + generated batch script
   state/jobs.tsv              jobid, name, gpu, time, spec: ownership record for cancel/after/usage
   state/owner                 the user who ran init (the only owner check; state/ is gitignored)
   state/changes-<date>.txt    the change report of each session (§3 step 9)
   state/marked_write_paths    the project's root, then the write paths whose marker has its line (§3 step 1)
   state/{claude,codex}/       the project's entries of the shared config folders (§5.7)
DIR/CLAUDE_environment.md      rendered at every start: on disk a notice that the agent is NOT contained;
                               inside, run/CLAUDE_environment.md in its place (read-only)
DIR/CLAUDE.md                  gets "@CLAUDE_environment.md" as its FIRST line (Claude's import syntax; put
                               back at every start)
DIR/AGENTS.md                  gets a pointer line (Codex reads AGENTS.md; no import syntax there)
W/.contained_writable          in every extra write path W: one "user project-root" line per project writing
                               there (read-only inside; §3 step 1)
```
Shared state: `~/.local/share/containedclaude/` (STATE; `$XDG_DATA_HOME/containedclaude` when set):
claude-config and codex-home (CLAUDE_CONFIG_DIR / CODEX_HOME for contained sessions), admin (the fixed working
folder of `containedclaude admin`), write_paths.txt (registry of every project's write paths, §5.8),
update-check.json and tool-identity.json (§3 step 4). This whole folder is hidden inside sandboxes except the
re-bound claude-config/codex-home (sessions only; jobs see neither) and, in admin mode, the admin folder
itself. Both config folders are shared by all contained projects, but their per-project entries (transcripts,
memory, history, session records, shell snapshots, Codex rollouts, ...) are bound over from the project
(`<project>/.contained/state/{claude,codex}/`) or its runtime folder: §5.7.

---------------------------------------------------------------------------------------------------------------
## 3. How a session starts (project.run)

1. find_root: walk up from the given dir to a folder with .contained/config.ini. Then nesting_problem (also in
   init, doctor, env): refuse if another .contained/config.ini exists further up, a WRITE_MARKER
   (.contained_writable) exists in an ancestor (at init also in the folder itself; any user's), or the root
   lies strictly inside a registered write_path (compat.registered()) or one of its own write_paths: a
   project's config and starter must not be writable from any sandbox, and a config further up would be the
   one the walk-up finds. init also refuses a folder containing a project (projects_inside: the nested scan).
   After start_problems, project.mark_write_paths adds the project's line to the marker of each extra write
   path (safe_write; a folder's first marker only after projects_inside finds nothing; cannot create -> start
   refused; another user's marker we cannot rewrite is enough), forget_old_marks removes the line from paths
   no longer configured (or all, after a move) using state/marked_write_paths. The markers are in
   Config.protect_paths (read-only inside, sessions and jobs) and left out of the change snapshot.
2. cli.main: compat.harden_host_env() (PATH etc. without project folders, §5.8); after Config:
   compat.register(write_paths) + harden again with them;
   check_host (bwrap, unshare, userns enabled), find `claude` with compat.tool; project.start_problems (also
   doctor, env; init does the same for the new folder): check_paths refuses if the project or a write_path
   is/contains ~, ~/.config, ~/.local/bin, the clone, the contained state or a hidden path, or lies inside one
   of those (config.unsafe_write_paths; also in jobwrap); config.placement_problems refuses paths not strictly
   below a site.ini allowed_roots entry (site-only key, read with config.site_config so a project config can't
   widen it) or on another mount than that root (user FUSE mounts: fusermount is setuid here;
   /proc/self/mountinfo; jobwrap checks the location only, mounts differ per node). Mounts INSIDE a writable
   path are not refused: policy.ro_inner_mounts binds them read-only after the writable bind (§5 step 5), on
   the login node and on each compute node from its own mountinfo; doctor lists them as "write outside".
   Before any of this, project.access_problem(DIR) (run, cli._root_or_fail for doctor/test/env, doctor): a
   folder that does not exist or cannot be read is reported as such, with a Kerberos hint when it lies on a
   sec=krb5* mount (config.krb5_shares) and `klist -s` finds no valid ticket (project.kerberos_missing; the
   SDS then answers "Permission denied", sometimes after a wait; without the check the launcher concluded
   "not a project"). kerberos_problem does the same inside start_problems for the write paths. The cli treats
   an argument as the folder when it is a directory OR written as a path (contains /), since isdir() is false
   for an unreadable folder. Phases that can block print a line after 2 s (project.Slow: "... is taking a
   while", then "done after N s"): the project/file-system checks and the protected-file snapshot;
   policy.automount reports shares that have not answered after 2 s (report callback); updates.notice
   announces the daily check before its network calls. The doctor after a tool change was announced already.
   owner_problem: the record init writes to .contained/state/owner must be the current user; another name
   or no record (both messages say what to do) refuses start, doctor, env and add-write. init itself refuses
   another user's record and (re)claims a folder without one (--force). state/ is gitignored, so a clone of
   a project starts unowned. File owners are never consulted: curry can't map the owner names the SDS
   reports, every file there is nobody. Same cause: sandbox_env lists every write path and PATH/* in git's
   safe.directory through GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/VALUE_n (protected config, so git honours it),
   otherwise git refuses every repository on the SDS ("dubious ownership").
3. ensure_state (shared config dirs; seed claude-config/settings.json from ~/.claude/settings.json, allowlisted
   keys SEED_KEYS only, never hooks/statusLine/apiKeyHelper; create the CLAUDE_CONFIG_PRECREATE_* and
   CODEX_HOME_PRECREATE_* entries (CLAUDE.md, settings.local.json, hooks/ skills/ agents/ ...; Codex: AGENTS.md,
   hooks.json "{}", rules/ prompts/ skills/ plugins/ packages/) empty if missing, so they can be bound
   read-only, and the mount points of the per-project entries; seed Codex's config.toml with
   `[sandbox_workspace_write] network_access = true` and `[features] daemon_auto_start = false` once).
   codex_trust: config.toml is read-only in sessions, so the launcher writes `[projects."<path>"] trust_level =
   "trusted"` itself (TOML basic-string escaping) for the project, its git top folder (_git_top: Codex trusts
   the repository root inside a repo) and every nested repository (cfg.git_dirs), each only if its header is
   missing; skipped if config.toml has an inline `projects =` table.
4. write_session_settings: run/user-settings.json (0600) = the contained config's settings.json (a JSON object
   with hooks and permissions in a form Claude Code reads, else the start is refused) plus claude_settings
   (PreToolUse hook -> write_guard.py, PreToolUse SendMessage + UserPromptSubmit -> peer_guard.py, PostToolUse
   Bash hook -> oom_notice.py, permissions.deny RemoteTrigger) and `allowedMcpServers: []` plus
   `disableClaudeAiConnectors: true` unless mcp_from_user_config (without the second, every session `claude
   remote-control` starts warns about the blocked connectors on stderr, and the server shows that warning in
   place of the session's real error); the user's own entries stay, written only when it changed. bwrap
   binds it read-only over claude-config/settings.json, so every Claude process in the sandbox has these
   settings, the sessions `claude remote-control` starts included (they get no launcher flags). The doctor
   writes it too.
   write_envdoc (the description to run/CLAUDE_environment.md, the outside notice to DIR/; each written only
   when it changed, so the project's running sessions keep their description at another start), refresh_stub
   (rewrites .contained_claude.sh if it differs from the template, so fixes to it reach old projects),
   ensure_import_line (`@CLAUDE_environment.md` back as the FIRST line of CLAUDE.md, which
   agents rewrite), tracked_protected_note (one line if a git-tracked protected file differs between HEAD and
   another local branch: git can't rewrite it inside). Admin instead: run/settings.json (claude_settings).
   Both: run/mcp.json (empty).
   updates.notice: once a day (STATE/update-check.json) the clone's HEAD vs its upstream (git fetch, 15 s,
   GIT_TERMINAL_PROMPT=0, only if .git is writable) and installed vs npm-latest of claude/codex, WARNING if
   behind; `containedclaude update` = updates.update_all: updates.pull_clone (git pull --ff-only; skipped
   with reason if not writable, no upstream, tracked files modified, or not fast-forwardable), after a
   change re-exec of the launcher with --no-pull and CONTAINEDCLAUDE_UPDATE_PULLED=<old HEAD> so the rest
   runs in the pulled code; then claude update / codex update on the host, each only if npm-latest is newer
   than the installed version (updates._is_current; `codex update` reinstalls unconditionally), then updates.self_test: quick
   test + session doctor on the given/current project or a throw-away one below ~, unregistered afterwards.
   updates.check_after_change (real sessions only): the identity (binaries: realpath, size, mtime; clone:
   HEAD read from .git/ without running git; STATE/tool-identity.json) changed since the last start ->
   session doctor on this project first (~6 s), WARNING + continue on FAIL.
5. automount(cfg.automount()): touch each share in parallel threads, one overall deadline (90 s), so they are
   mounted BEFORE the private namespace exists (see §5.2).
6. broker.serve on /tmp/containedclaude-<uid>-XXXX/broker.sock (host /tmp, mode 700); nsinit keeps that folder
   reachable at /tmp/.containedclaude below the session's tmpfs (--keep: opened before /tmp is covered), and
   bwrap binds it read-only onto itself (connect works on a read-only mount; the folder is on the login
   node's root file system, outside tmp_size).
   policy.project_runtime: the project's runtime folder on the host, <XDG_RUNTIME_DIR or /tmp/containedclaude-
   <uid>>/containedclaude/<project_id>/ (must be ours, 0700, no symlink, else refused), with xdg/ kept at
   /tmp/.xdg-runtime (= XDG_RUNTIME_DIR inside: Claude's cc-socks/<pid>.sock). policy.project_store: the
   per-project entries of the two config folders (§5.7), bound over their places inside: from
   <project>/.contained/state/claude/ (projects/ = transcripts + memory, history.jsonl, plans, file-history,
   ...) and <runtime>/claude/ (sessions/ = Claude's <pid>.json record + <pid>.<hash>.key, shell-snapshots,
   session-env, teams, tasks, daemon), the same for Codex (state/codex/sessions, archived_sessions,
   history.jsonl; <runtime>/codex/shell_snapshots, tmp). So the sessions of one project find each other
   (ListAgents/SendMessage/teammates; verified with two interactive sessions in tmux and a headless one
   asking), other projects, users and unsandboxed sessions not. Claude names those files after its
   pid and every sandbox's first process would be pid 2, hence nsinit's pid namespace with a random base
   (random.randrange(100000, 3000000) per session; pid_max 4194304): pid 1 inside writes
   /proc/sys/kernel/ns_last_pid (allowed: CAP_SYS_ADMIN in our user namespace), remounts /proc for the new
   namespace (bwrap looks itself up under /proc/<pid>), forks bwrap and stays as init (reaps, forwards
   SIGTERM/SIGHUP, SIGINT ignored like the launcher, PDEATHSIG from the waiter); the waiter outside returns the
   status. Discovery does not need pid liveness: Claude records a pidDomain and falls back to the socket.
7. sensitive_snapshot (content hashes of protected files, nested and submodule ones included (re-walked), the
   contents of every git dir's hooks/, and Config.watch_paths: commondir + config.worktree of every git dir and
   linked worktree, existing or not).
8. exec (policy.session_command): `systemd-run --user --scope --quiet --unit containedclaude-<pid>-<x>
   -p MemoryMax=<session_mem> -p MemorySwapMax=0 -p OOMPolicy=continue -- /usr/bin/python3 -I nsinit.py
   --tmp-size <tmp_size> --pid-base <random> --keep <runtime>/xdg /tmp/.xdg-runtime --keep
   /tmp/containedclaude-<uid>-XXXX /tmp/.containedclaude -- /usr/bin/bwrap <policy, /tmp /var/tmp /dev/shm
   re-bound rw, /tmp/.containedclaude ro, the project's entries of claude-config and codex-home bound over
   (project_store), run/user-settings.json over claude-config/settings.json, no --unshare-pid> -- claude
   --mcp-config run/mcp.json --strict-mcp-config <user args>` with a cleaned env (plus the host's
   XDG_RUNTIME_DIR, which systemd-run needs to reach the user manager; bwrap --setenv puts the sandbox value
   back inside; see §5.9 for the scope), SIGINT ignored
   in the launcher (Claude handles Ctrl-C itself; the launcher must survive to stop the broker). SIGHUP/SIGTERM
   (ssh connection dropped, kill) are forwarded to bwrap, so the cleanup below still runs.
9. After exit: stop broker, remove socket dir, report_changes (warn about created/changed protected files; also
   saved to .contained/state/changes-<date>.txt, since after a hang-up there is no terminal).

`containedclaude shell` = the same with bash (or a given command) instead of claude. `admin` = same, with the
fixed folder STATE/admin as the project (config.ini rewritten at each start, write_paths = the contained
claude-config, no start_problems checks) and the config's ro sub-binds dropped (mode "admin") -> /login, plugin
installs, settings changes; `admin codex` runs Codex there instead of Claude (its config.toml, skills,
plugins, ... writable; bundled skills are only refreshed there). project.trust_folder writes projects[ADMIN_DIR].hasTrustDialogAccepted = true into
the contained .claude.json first (if it exists and parses), so the trust dialog never comes up there. Admin's
settings.json is the real one, so it gets the launcher's settings as a flag: `--settings F --mcp-config F
--strict-mcp-config`; project sessions get only the MCP flags (their settings come from settings.json, step 4,
so hooks don't run twice). --mcp-config is variadic and the boolean after it ends the list, so a subcommand in
the user's args (`containedclaude admin -- plugin list`) works. `containedclaude server` (= `containedclaude
-- remote-control`: the server for sessions started from claude.ai, `remote-control` as the first argument)
gets none of these MCP flags: Claude Code refuses to start it behind --mcp-config/--strict-mcp-config, and
the sessions it starts get no flags anyway; they have the settings.json rules, `allowedMcpServers: []`
included. They share the server's sandbox and so its scope
(session_mem, session_tasks, session_cpus): `server` adds `--capacity 6` (cli.SERVER_CAPACITY), because the
server's own default of 32 sessions would need more processes+threads than session_tasks, and `--spawn
same-dir`, which also skips Claude Code's question for the mode. Worktree mode is not supported (README): its
checkouts hold only tracked files, and on the SDS Claude Code's own git checks run without the GIT_CONFIG_*
variables that carry safe.directory, so git refuses the repository. `--no-create-session-in-dir`: with a
`bridge-pointer.json` written in the last 4 h (the server rewrites it at every start) Claude Code takes over
the previous run's first session, and that start fails once the session was archived on claude.ai. Each flag
is left out when the args give their own or a mode in which Claude Code refuses it (`--continue`/`-c` and
`--session-id` refuse all three, `--spawn session` refuses `--capacity` and is its one session).

---------------------------------------------------------------------------------------------------------------
## 4. How a job runs

Inside: `cc-run [opts] files | -- cmd` -> JSON {"action":"submit", files/argv, opts, image, cwd} to the socket.
Broker (outside, broker.Broker.submit):
- validates: name regex (first char alphanumeric: `#SBATCH -J -x`), cpus/mem/time against max_* (config), array
  syntax + size, --after ids (repeated or comma list) must be in state/jobs.tsv, env keys regex + ENV_DENY_RE
  (PATH, LD_*, BASH_ENV, PYTHONHOME, APPTAINER*, SLURM_*, CONTAINEDCLAUDE*), image = none | docker/oras/library
  URI | existing file (relative to request cwd). With a dependency: `--kill-on-invalid-dep=yes` (curry's
  default leaves a job whose parent failed pending forever; then cc-wait ends with it CANCELLED).
- limits (Broker._check_limits, under submit_lock so parallel requests cannot both pass): queue from
  `squeue -r` (one row per array task), rows
  with our comment tag or in jobs.tsv. max_jobs: rows + new tasks. max_gpu_jobs / max_total_cpus /
  max_total_mem: concurrent footprint; an array throttled with %N (throttle recorded in jobs.tsv column 6)
  counts at most N tasks.
- steps: .ipynb -> jupyter nbconvert --execute (output to executed_dir, name = relative path with / -> __),
  .py -> python, .R -> Rscript, .sh -> bash; cwd = the file's folder. Command mode: argv as given, cwd = client cwd.
- writes run/jobs/<stamp>_<name>.json (spec) and .sh (batch script with #SBATCH lines built only from validated
  values + `--comment=containedclaude:<sha1(project)[:12]>`), runs `sbatch --parsable`, appends state/jobs.tsv.
Batch script: `exec /usr/bin/python3 -I <clone>/lib/containedclaude/jobwrap.py <spec.json>`.
jobwrap (on the node, outside any sandbox; harden_host_env first, the environment comes from the launcher via
sbatch): mkdtemp scratch in $TMPDIR or /tmp (node-local), automount (config
shares + image_binds + project + step cwds + image path), then per step:
`unshare ... -- bwrap <policy mode=job, scratch bound at /tmp, /dev/fuse, GPU /dev/nvidia* if gpu,
 --setenv K V for the request's --env> -- <argv>`
or with an image:
`... -- apptainer exec --userns --containall --workdir /tmp/.apptainer-work --home /tmp/home --pwd <cwd> [--nv]
 --bind <image_binds + write_paths> --env K=V... <image> <argv>`.
The --env list: policy.IMAGE_ENV (USER, LOGNAME, MPLBACKEND, HDF5_USE_FILE_LOCKING, thread and cache variables, as
a job without an image has them), Slurm's, then the request's. --contain skips the site's `bind path` entries
except /etc/hosts and /etc/localtime (so /mnt only through image_binds and write_paths) and keeps apptainer's
resolv.conf: images get the node's name resolution and time zone. Not --no-mount bind-paths: it drops exactly
those, leaving an image's empty placeholders (DNS fails) and UTC (tested).
The request's --env variables are NEVER in the environment of unshare/bwrap (those run outside the sandbox): only
bwrap --setenv and apptainer --env. First failing step stops the job; scratch removed at the end (chmod first: apptainer leaves ro dirs).
SIGTERM/SIGINT/SIGHUP (scancel, time limit; Slurm sends KILL after KillWait = 45 s on curry) are caught (jobwrap.Stop):
forwarded to the step, step killed after 20 s, then the scratch is removed. curry has no per-job tmp container
(TmpFS=/tmp), so without this every cancelled job left its scratch (incl. image builds) on the node.
Logs: .contained/logs/%x_%j.out (arrays %x_%A_%a.out).
cc-cluster = broker action `cluster`: sinfo -N -O (CPUsState, AllocMem, Gres, GresUsed) and squeue of all users
(`mine` from the comment tag); cc-info's nodes carry the same fields. Slurm's allocations, no measured load.
cc-gpu [NODE] = broker action `gpu`: `ssh -n -o BatchMode=yes NODE nvidia-smi` run by the broker (outside, the
user's key); NODE must equal one of the nodes sinfo lists with a gpu GRES (default: the first), the remote
command is a constant: nothing from the sandbox reaches a shell.
cc-usage [JOBID ...] = broker action `usage` (replaced `history`; own jobs only, default the last 50): sacct
allocation line (State, ElapsedRaw, AllocCPUS, TotalCPU, ReqMem, ExitCode, NodeList) + the .batch step's
MaxRSS/TotalCPU; a RUNNING job gets AveCPU/MaxRSS from sstat on its .batch step instead (sacct has nothing
until a step ends). Rows carry cpu_eff = cpu_used / (elapsed * cpus) and mem_eff = peak / requested; _secs
and _mb parse Slurm's time and size strings (ReqMem suffix n = per node, c = per CPU). cc-wait prints the
usage_table when the jobs have finished, so the agent sees peak memory and CPU efficiency once per job;
cc-jobs (the "still running?" lookup) stays compact, also with --all, to keep the agent's context small.
Sampling is JobAcctGatherFrequency (30 s on curry): spikes shorter than that are missed.
cc-wait polls the broker's queue every 30 s (one blocking call for the agent), then prints the usage table + log
tails; exit 0 all COMPLETED, 1 otherwise, 2 on --max timeout.
Slurm writes a log when the job starts. For a job without one, cc-log and cc-wait say why (cc no_log, existing
broker actions only): its queue state and reason (PENDING (Dependency)), or sacct's state if it never started
(NodeList "None assigned"; CANCELLED there = a failed --after parent or a cancel while it waited).
cc-build = command job `apptainer build --fakeroot --force out.sif def` (image none, 8 cpu/16G/2h default);
cc-pull = `apptainer pull --force out uri`. Both run INSIDE the job sandbox, so %setup/%files cannot touch
anything outside the project -> no definition-file validation needed. Both write out.sif.sha256 next to the
image (`sha256sum -c` in its folder; cc with_checksum, one bash step; an old one is removed first).

---------------------------------------------------------------------------------------------------------------
## 5. The policy in detail (policy.bwrap_args) and WHY each piece exists

Order matters: later mounts cover earlier ones.
1. `--ro-bind / /`: recursive read-only view (bwrap remounts every submount ro; verified incl. NFS/autofs).
2. `--dev /dev`, `--proc /proc` (new procfs of the new pid ns); session: /tmp, /dev/shm, /var/tmp re-bound rw
   from nsinit's tmpfs on /tmp (one tmp_size limit for all three, /var/tmp and /dev/shm are /tmp/.var-tmp and
   /tmp/.shm; unlimited if tmp_size is 0). The tmpfs must sit ON /tmp (mount root "/"): Codex's own sandbox
   derives canonical paths from the mount table and failed with the earlier sub-folder bind of a tmpfs
   mounted elsewhere. Job: scratch on /tmp, `--tmpfs /var/tmp`, `--dev-bind /dev/fuse` (apptainer userns needs
   squashfuse), GPU: /dev/nvidia*, /dev/dri.
3. hide: DEFAULT_HIDE + hide_paths: dir -> `--tmpfs`, file/socket -> `--ro-bind /dev/null`; symlinks resolved.
   Hidden dirs get `--remount-ro` at the very end (after child binds like claude-config were placed inside them).
4. expose: DEFAULT_EXPOSE (~/.codex/packages: the codex binary lives there) ro again.
5. write_paths: project + config write_paths, `--bind` (recursive), then `--ro-bind` for every mount point
   inside them (config.inner_mounts): a file system mounted inside a project stays read-only.
6. session/admin: rw bind of claude-config (+ codex-home), then ro binds of CLAUDE_CONFIG_RO and CODEX_HOME_RO
   entries (policy.config_ro; session only), rw plugins/data again, the broker socket folder ro; then the project's entries of both folders over their places
   (policy.project_store, §5.7): persistent ones from <project>/.contained/state/{claude,codex}/,
   session-lifetime ones from the project's runtime folder (§3 step 6).
7. protect: first `--bind d d` for policy.pinned_dirs (every folder between the write root and an existing
   protected path: .git, .claude, sub, sub/.git, .git/modules/x, ...; outer ones first). These become mount
   points, so they can't be renamed or removed (EBUSY); doctor checks this on a throw-away fixture. Then
   `--ro-bind p p` for policy.effective_protected: the EXISTING protected paths (DEFAULT_PROTECT + protect_paths +
   nested/submodule ones; see §7.3) with symlinks resolved on the host side and only those inside a writable
   area. bwrap binds by path: a relative link in the path would protect the link instead of its target, and an
   absolute one made bwrap fail to start (fixture in doctor.make_fixture covers both). DIR/CLAUDE_environment.md
   gets policy.envdoc_inside (run/CLAUDE_environment.md) as its source instead, when that exists; without it
   the outside notice shows, the safe side. test checks the description inside (session and job, and in a
   sandbox that runs while the project is started again: cli._envdoc_kept), doctor the notice on disk.
8. shims: `--ro-bind sandbox/shim <realpath of each command found on the user's original PATH>`.
9. `--dir /tmp/jupyter-runtime` in sessions (/tmp/.xdg-runtime is the project's runtime folder there, kept by
   nsinit; jobs get an empty `--dir /tmp/.xdg-runtime`), remount-ro hidden dirs, `--unshare-pid` in jobs only
   (sessions run in nsinit's pid namespace), `--unshare-ipc --unshare-cgroup-try --die-with-parent`,
   `--seccomp FD`, `--setenv` (jobs: request variables; sessions: XDG_RUNTIME_DIR back to the sandbox value),
   `--chdir`.
Env (sandbox_env): drop DROP_ENV_PREFIXES (SSH_*, DBUS_*, KRB5*, CLAUDE_CODE_* incl. the host session's messaging
socket/token, CODEX_*, SLURM_*, DISPLAY...), PATH = sandbox/bin + the user's ORIGINAL PATH
(CONTAINEDCLAUDE_INNER_PATH; LD_LIBRARY_PATH/LD_PRELOAD originals via bwrap --setenv, never in the env of bwrap), CONTAINEDCLAUDE_* (used by write_guard and
cc), XDG_RUNTIME_DIR, TMPDIR, APPTAINER_USERNS=1, APPTAINER_CACHEDIR=<project>/.cache/apptainer; session:
CLAUDE_CONFIG_DIR, CODEX_HOME, DISABLE_AUTOUPDATER=1, CONTAINEDCLAUDE_SOCKET, CONTAINEDCLAUDE_CONFIG_RO (the
CLAUDE_CONFIG_RO paths for write_guard; empty in admin), XDG_CACHE_HOME=<project>/.cache (+ PIP_CACHE_DIR,
CONDA_PKGS_DIRS, MPLCONFIGDIR, npm_config_cache, UV_PYTHON_INSTALL_DIR below it, in sessions AND jobs: the real
home is ro, /tmp is RAM or deleted after the job, and the project is shared, so models/packages downloaded once
are reused); session: JUPYTER_RUNTIME_DIR=/tmp/jupyter-runtime (the real runtime dir is hidden),
CONTAINEDCLAUDE_SCOPE / CONTAINEDCLAUDE_SESSION_MEM (for oom_notice.py and the probe); job: HOME=/tmp/home,
JUPYTER_PATH=<real home>/.local/share/jupyter (user-installed kernelspecs are found although HOME moved).

### 5.1 Why not just apptainer --containall for jobs?
`apptainer --bind /mnt:/mnt:ro` is NOT recursive: submount /mnt/sds-hd stayed writable (verified). Read-all
would need one ro bind per mount point. Running apptainer *inside* bwrap gives the recursive ro for free and the
exact same rules as the session. Inside bwrap apptainer cannot use its setuid starter (no_new_privs) -> user
namespace mode (`--userns`, APPTAINER_USERNS=1). Plain `apptainer exec` also falls back on the login node, but on
curry58 it errored "starter-suid doesn't have setuid bit set" until --userns was explicit.

### 5.2 Why the outer private mount namespace (nsinit.py in sessions, `unshare --propagation private` in jobs)?
bwrap's mounts are slaves of the host (`master:` in mountinfo): a share the automounter mounts AFTER the sandbox
started propagates in, and arrives READ-WRITE (verified with /home/<unmounted user>). With a private outer mount
namespace nothing propagates; an unmounted share is simply invisible (listing fails quickly, no hang). Hence
automount before start, and the `automount` config list. Symptom if a share is missing: "operation not
permitted"/ENOENT on lstat inside -> add it to automount (or it was slow: deadline 90 s in policy.automount).

### 5.3 Why seccomp?
dev.tty.legacy_tiocsti=1 on curry: a sandboxed process could push characters into the user's terminal input
(executed by the user's shell after the session). policy.seccomp_program: hand-assembled classic BPF, x86_64
only (returns None elsewhere -> no filter, doctor shows FAIL on TIOCSTI): deny ioctl TIOCSTI/TIOCLINUX with
EPERM, deny non-native ABIs (i386/x32) to stop bypasses, allow everything else. Layout of seccomp_data: nr@0,
arch@4, ip@8, args@16 (args[1] low 32 bits @24, little endian).

### 5.4 Hidden things and why (DEFAULT_HIDE in config.py)
munge socket (= Slurm submission; THE important one), /run/user/<uid> (systemd user bus: systemd-run --user
would start processes outside; pipewire; Claude Code's cc-socks session sockets), ~/.ssh, ~/.gnupg (gpg-agent
also offers ssh-agent), ~/.claude + ~/.claude.json (user's real, unsandboxed Claude config; daemon control.key;
transcripts of all projects), ~/.codex, ~/.copilot, gh/git/cloud
credentials, jupyter runtime (server tokens), keyrings, krb5 caches, VNC,
X authority, shell histories, ~/.vscode-server (remote CLI sockets/tokens), the state folder (STATE),
/run/dbus (system bus: logind/systemd control; nothing in a sandbox needs it). Entries below ~/.local/share,
~/.config and ~/.cache are hidden where XDG_DATA_HOME / XDG_CONFIG_HOME / XDG_CACHE_HOME point too
(config.with_xdg), so add them under their default place only.
Add new ones in DEFAULT_HIDE (global) or hide_paths (project). `containedclaude doctor` lists user-owned sockets
it tested.

### 5.5 Protected paths and why (DEFAULT_PROTECT)
Files in the writable project that the USER later executes or that tools load automatically outside the sandbox:
.git/hooks, .git/config, .git/info, .claude/settings*.json, .claude/hooks, .mcp.json, .envrc (direnv), .vscode,
.idea, .codex, plus our own .contained/, .contained_claude.sh (the user runs it), CLAUDE_environment.md, and the
.contained_writable marker of every extra write path. NESTED_PROTECT:
the same in sub-folders (nested repos), plus a nested project's .contained/ and .contained_claude.sh, found by
Config._scan (depth <= 3, skips NESTED_SKIP + executed_dir, 3 s budget shared with the submodule search), bound
ro too. Submodule git dirs live in <gitdir>/modules/ (the submodule's own .git is a file): _scan searches there
for the project's and every nested .git (a folder with HEAD = a git dir; names may contain "/", nested
submodules; SUBMODULE_DEPTH 8) and protects GITDIR_PROTECT (config, hooks, info).
GITDIR_WATCH (commondir, config.worktree; every git dir and <gitdir>/worktrees/*) is only compared in the change
snapshot: git needs these absent or valid, so they can't be bound. A protected path that is a symlink protects its
target (effective_protected), and the snapshot hashes the target as well as the link text.
Pinned parents: see §5 step 7. Side effect: those folders can't be renamed or deleted inside a session.

### 5.6 The broker never writes into agent-writable folders
Slurm writes logs as the user outside the sandbox, so logs/specs/scripts/state live in .contained/ (ro inside,
and its mount point cannot be renamed while bound), never in a folder the agent can prepare. project.safe_write refuses symlinks and writes via
tempfile+rename (rename replaces a symlink, never its target).

### 5.7 Claude config and Codex home: shared, but per project where it matters
CLAUDE_CONFIG_DIR and CODEX_HOME are shared by all contained projects (one login, one set of settings and
plugins). Every entry of the two folders is in one of four groups (lists at the top of policy.py; doctor's
"shared config folders" section reports entries in none of them, which a tool update may add, so they are
noticed after the automatic doctor run that follows a Claude/Codex update):
- read-only inside (CLAUDE_CONFIG_RO: settings.json, settings.local.json, CLAUDE.md, hooks, plugins except
  plugins/data, skills, agents, commands, output-styles, rules, keybindings.json, statusline.sh, dev-mods,
  cowork_plugins, workflows, themes: they define behaviour; the missing ones are pre-created by ensure_state,
  since bwrap can only make existing paths ro; CODEX_HOME_RO: config.toml, hooks.json, AGENTS.md, rules,
  prompts, skills, plugins, packages; the launcher writes Codex's folder trust (§3 step 3), and Codex's
  bundled skills (skills/.system) are only refreshed when Codex runs in `admin codex`);
- per project, persistent (CLAUDE_CONFIG_PER_PROJECT + _FILES: projects/ = transcripts + auto-memory,
  history.jsonl = prompt history (appended in place, so a file bind works), plans, file-history (rewind),
  todos, paste-cache, jobs, agent-memory, memory, remote-control, shares, uploads, chrome, ccr; Codex:
  sessions, archived_sessions, tui-thread-reference-capabilities, history.jsonl, session_index.jsonl (+ .lock;
  appended in place)): what a session records and reads back later, in
  <project>/.contained/state/{claude,codex}/ (gitignored; ro at that path inside, rw at the config path);
- per project, runtime (CLAUDE_CONFIG_PER_RUNTIME: sessions, shell-snapshots, session-env, teams, tasks,
  daemon; Codex: shell_snapshots, tmp, .tmp, app-server-control, app-server-daemon): alive for a session and sourced, executed or connected to (the
  shell snapshot and the SessionStart hook env file go into every Bash command; Codex puts helper binaries
  like apply_patch into tmp/arg0-linux/ and runs them), in the project's runtime folder (tmpfs, §3 step 6);
- shared and writable (CLAUDE_CONFIG_SHARED, CODEX_HOME_SHARED + *.sqlite): root files Claude replaces by
  rename (.claude.json, .credentials.json, ...: a file bind would break them), caches, diagnostics; Codex's
  login, its memory and thread index (state_*.sqlite lists every project's threads by cwd, title and first
  prompt; `codex resume` filters by cwd), logs.
Entries on no list (a tool update added them): in sessions, real folders among them are per project too
(policy.unknown_folders -> project_store, from <project>/.contained/state/{claude,codex}/<name>); files stay
shared (a file bind would break a rename). Admin keeps both shared. doctor prints them as `note` lines (no
FAIL), updates.quick_check repeats the notes; classify them in policy.py. `containedclaude test` creates a
throw-away unknown folder (.selftest-unknown-*) and checks it is the project's; only the test's own session
counts it (policy.TEST_UNKNOWN_ENV), other sessions and the doctor skip it.
Consequences: transcripts and memory move and go with the project, `--resume`/`codex resume` show only the
project's sessions, and no session can read or edit another project's records or the files another
project's session executes. What stays shared is documented as the residual in the limitations (§7.7-8).
.claude.json must stay writable (Claude rewrites it constantly) and can contain MCP servers -> launcher passes
--strict-mcp-config + an empty --mcp-config, and the session's settings.json has `allowedMcpServers: []` and
`disableClaudeAiConnectors: true` (the rules the sessions `claude remote-control` starts have, §3), so no MCP
server is used, claude.ai connectors
included (config `mcp_from_user_config = yes` to opt out). The user's `/login` happens once (first session or
`containedclaude admin`). Plugins (e.g. codex) must be installed via `containedclaude admin` (plugins/ is ro
in normal sessions). Admin mode gets the same per-project binds for its fixed folder (its own records), so it
does not see other projects' either.

### 5.8 The host side trusts no program a sandbox could have written
Launcher, broker, jobwrap and the starter script run outside the sandbox and start programs (unshare, bwrap,
sbatch/squeue/..., `claude --help`, git). They only use programs from places no sandbox can write to, also when
a project environment is on the user's PATH:
- bin/containedclaude: `#!/usr/bin/python3 -I` (no PYTHONPATH, no user site).
- compat.harden_host_env(extra): saves PATH/LD_LIBRARY_PATH/LD_PRELOAD in CONTAINEDCLAUDE_INNER_*, then removes
  entries that compat.untrusted() (inside a folder with .contained/config.ini above it, or under a path in
  ~/.local/share/containedclaude/write_paths.txt = compat.register(), or `extra`), appends SYSTEM_PATH. Called in
  cli.main, again in project.run / doctor with the project's write_paths, and in jobwrap (sbatch passes the
  hardened launcher env + INNER_* on to the job).
- compat.tool(name): absolute path from the hardened PATH, refused if its realpath is untrusted. unshare and bwrap
  are always started by absolute path, because Popen(env=sandbox env) and unshare's execvp look them up in the
  env's PATH, which is the user's original one (needed inside).
- LD_PRELOAD / LD_LIBRARY_PATH set in the user's own shell already affect the launcher's python itself; that
  cannot be fixed from inside python (and affects every program the user runs) -> documented, not handled.
- The starter .contained_claude.sh runs before the launcher: `#!/bin/bash -p` (no
  BASH_ENV/ENV, no functions or CDPATH from the environment), builtins only, /usr/bin/readlink by absolute path
  only when the script itself is a symlink. project.refresh_stub rewrites outdated stubs at every start.

### 5.9 Session memory limit (policy.session_scope, sandbox/hooks/oom_notice.py)
The login node's user slice has no memory cap here, and tmp_size only caps files. So the launcher wraps the
whole sandbox in a transient systemd user scope: `systemd-run --user --scope -p MemoryMax=<session_mem>
-p MemorySwapMax=0 -p OOMPolicy=continue`. The cgroup charges every process of the session AND the tmpfs
pages of /tmp. `OOMPolicy=continue` matters: with the default (stop) systemd ends the whole scope after the
first OOM kill, i.e. the session dies instead of one process. The launcher and broker stay outside the scope.
compat.scope_error runs a /bin/true scope with the same properties once; if that fails (no systemd user
session, old systemd without OOMPolicy=, no systemd-run) the session starts without a limit and warns.
Inside, the scope's memory.events is readable via /sys/fs/cgroup (unit name in CONTAINEDCLAUDE_SCOPE);
oom_notice.py (PostToolUse on Bash) reports new oom_kill counts to the agent with exit 2, once each, so a
`Killed`/137 is explained. `doctor` checks memory.max of the scope from inside.
The same scope carries TasksMax=session_tasks (pids controller: hard). CPU: the user manager on curry only
delegates memory and pids (compat.user_controllers reads user@UID.service/cgroup.subtree_control), so
CPUQuota is only added when 'cpu' is delegated; otherwise nsinit.py applies nice 10 (not undoable without
privileges) and sched_setaffinity to session_cpus random cores (policy.session_cores; inherited, a process
could widen it: courtesy cap), and sandbox_env sets OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS (jobs: from
SLURM_CPUS_PER_TASK). nsinit is always the outer layer of sessions (--tmp-size 0 = unlimited tmpfs). Bandwidth:
not cappable unprivileged; CLAUDE_environment.md sends large transfers to jobs. doctor checks pids.max,
affinity count and niceness.

### 5.10 Messages between sessions (sandbox/hooks/peer_guard.py)
Without Remote Control a contained session finds only the sessions of its project (the per-project runtime
folder, §3). With Remote Control on (`--remote-control`, `/remote-control`), Claude Code also lists the
account's Remote Control sessions (ListAgents type remote_control) and messages them over Remote Control.
peer_guard.py (in the session's settings.json, §3, 4) keeps messages within the project:
- UserPromptSubmit: a prompt that starts with `<cross-session-message ...>` is blocked ({"decision":
  "block"}) unless the from= of every such header is a local socket (`uds:`, `/....sock`) or `sid:` with a
  session id of this project. The user sees "blocked by hook" with the reason; the model gets nothing (the
  transcript has only the queue entry and that notice). Local messages pass.
- PreToolUse SendMessage: allowed are recipient_kind main and agent, the reserved names main, team-lead, user,
  system, local socket addresses, the names of this project's running sessions (records in
  $CLAUDE_CONFIG_DIR/sessions whose socket accepts a connection: killed sessions leave their records behind),
  their refs (6 to 12 hex digits of sha256("session:<socket>"), or of "session:sid:<session id>"), and this
  session's agents (Agent/Task calls' input.name and agentId in the transcript) and teammates
  (teams/*/config.json members). Anything else is denied with a text that says what to do. A name shared by a
  local and a remote session is refused by Claude Code itself ("2 agents are named ..."); the ref decides then.
- permissions.deny RemoteTrigger: the tool is gone from the session (routines run in the cloud with the
  account's connectors).
Like the other hooks this is not a boundary (§1); a separate claude.ai account for the contained config
separates contained sessions from the user's others completely. The sessions `claude remote-control` starts
read the same settings.json, so all of this applies there too. Locally they are listed under a derived name
(`t1-55` in a folder t1); `--name` names only the server's Remote Control entry.

---------------------------------------------------------------------------------------------------------------
## 6. Version dependencies (what can break when tools update) and where to fix

| dependency | used for | if it changes |
|---|---|---|
| bwrap >= 0.4 (curry 0.6.3) | --ro-bind --tmpfs --remount-ro --dir --seccomp --die-with-parent --unshare-cgroup-try | compat.py: BWRAP_REQUIRED missing -> refuse to start; BWRAP_OPTIONAL missing -> left out with a warning (--dir falls back to --tmpfs; no --seccomp = no TIOCSTI filter; no --remount-ro = hidden dirs writable in RAM). doctor prints `optional flags: ...` |
| kernel namespaces via ctypes (nsinit.py) | unshare(CLONE_NEWUSER\|CLONE_NEWNS), uid_map/gid_map/setgroups, mount MS_REC\|MS_PRIVATE, tmpfs on /tmp (size=), MS_BIND of /tmp/.var-tmp, /tmp/.shm and /proc/self/fd/N (the kept folders); unshare(CLONE_NEWPID) + /proc/sys/kernel/ns_last_pid + a procfs remount, prctl(PR_SET_PDEATHSIG) | syscall ABI, stable; a failed mount or unshare stops the start with the errno (no fallback: a session without its private /tmp or pid namespace must not run). doctor: "own pid namespace" |
| Claude Code session messaging | $XDG_RUNTIME_DIR/cc-socks/<pid>.sock, <config>/sessions/<pid>.json + <pid>.<hash>.key, record field pidDomain (pid namespace id; liveness falls back to the socket) | if the layout moves, sessions of one project stop seeing each other (ListAgents empty), nothing else; policy.project_runtime + "sessions" in CLAUDE_CONFIG_PER_RUNTIME are the two places |
| Claude Code cross-session messages (2.1.319) | peer_guard.py: inbound prompt `<cross-session-message from="uds:...\|bridge:...\|did:..." from-name= from-mode=>` (UserPromptSubmit; its `source` field absent), SendMessage tool_input `to` + `recipient_kind` (main\|agent\|session\|name), "name [ref]", ref = sha256("session:<id>")[:6..12], session record fields name, sessionId, messagingSocketPath; reserved names main, team-lead, user, system | peer_guard stops recognising messages or recipients; test's check uses sample payloads and can't see that, the occasional manual check in §8 does |
| Claude Code config folder layout (2.1.318) | projects/<cwd, non-alnum -> '-'>/<session>.jsonl + memory/, history.jsonl appended in place, shell-snapshots/, session-env/<session>/sessionstart-hook-N.sh, file-history/, plans/, ... (the lists in policy.py, taken from the binary's `(Ue(),"name")` joins); .claude.json and .credentials.json replaced by rename | a renamed or new entry is shared between projects until classified: doctor's "shared config folders" section names it (runs after every Claude update); `containedclaude test` checks that every per-project entry is its own mount inside |
| Codex home layout (0.166.0) | sessions/YYYY/MM/DD/rollout-*.jsonl, archived_sessions/, history.jsonl and session_index.jsonl appended in place, shell_snapshots/<thread>.<ts>.sh, tmp/arg0-linux/codex-arg0*/{apply_patch,applypatch,codex-linux-sandbox}, state_N.sqlite thread index (cwd, rollout_path, title, first_user_message), skills/.system (bundled skills, marker file), app-server-control/ + app-server-daemon/ (background server; off) | same doctor section (patterns *.sqlite); `codex resume` of another project's thread fails (rollout not visible), the picker filters by cwd; a read-only skills/ only means the bundled skills update in `admin codex` |
| Codex CLI (0.166.0) | its own sandbox = a bwrap + seccomp inner layer; needs /tmp to be a real mount (mount root "/") and, for sockets, `[sandbox_workspace_write] network_access = true` in CODEX_HOME/config.toml (seeded by ensure_state); `[features] daemon_auto_start = false` (seeded: otherwise the TUI copies its binary into packages/ and runs a background server); folder trust `[projects."<path>"] trust_level` (written by codex_trust; Codex can't write config.toml in sessions) | `containedclaude test` runs a command under `codex sandbox` (writes the project, reaches the broker); failure shows there and as "bwrap: Can't mkdir parents ..." or "broker not reachable (Operation not permitted)" in Codex output |
| util-linux unshare (2.37.4), jobs | --user --map-current-user --mount --propagation private | compat.outer_unshare: falls back to --map-user=UID --map-group=GID (never -r: that maps to root) |
| apptainer 1.5.5 | exec --userns --containall --workdir --home --pwd --nv --bind --env; build --fakeroot; pull | jobwrap: --userns/--containall/--workdir only if `apptainer exec --help` lists them (compat.likely: if --help is unreadable, pass everything as before); APPTAINER_USERNS=1 forces userns anyway. Security does not depend on these: the surrounding bwrap sandbox decides ro/rw |
| Claude Code (2.1.319) | CLAUDE_CONFIG_DIR/settings.json read by every Claude process (user settings), also by the sessions `claude remote-control` starts (they inherit env and mounts, not flags; the server refuses to start behind --mcp-config/--strict-mcp-config); `allowedMcpServers: []` there hides every MCP server incl. claude.ai connectors, `disableClaudeAiConnectors: true` keeps the connectors from loading (else each session the server starts warns about them on stderr); the managed-settings folder is fixed at /etc/claude-code (no override in the public build); --settings (admin only), --strict-mcp-config, --mcp-config (not for the server; passed only if `claude --help` lists them, else warns), DISABLE_AUTOUPDATER, hook JSON (tool_name, tool_input.file_path/notebook_path, exit 2 = block + stderr), @import in CLAUDE.md | check `claude --help`; config dir layout names in policy.CLAUDE_CONFIG_RO (missing names are skipped) |
| Slurm 26.05 | sbatch --parsable --comment; squeue -r -o "%F|%i|%j|%T|%M|%l|%C|%m|%b|%R|%k" (also for all users: cc-cluster); sacct -P (JobID, JobName, State, ElapsedRaw, AllocCPUS, TotalCPU, ReqMem, MaxRSS, ExitCode, NodeList; the .batch step for MaxRSS); sstat -P AveCPU,MaxRSS on <job>.batch; sinfo -N -O NodeList,Partition,StateCompact,CPUsState,Memory,AllocMem,Gres,GresUsed | format letters stable for decades; if %k (comment) breaks, ownership still works via state/jobs.tsv; MaxRSS needs jobacct_gather (cgroup here, 30 s samples); GresUsed strings like `shard:a40:8(8/32)` are parsed by cc's gpu_text |
| claude / codex subcommands (lib/shell-guard.sh) | the shell guard's lists of subcommands that start no agent (`_ccg_claude`, `_ccg_codex`, from `--help`) | a new subcommand that starts no agent asks needlessly until listed; check the lists against `--help` after updates |
| ssh to compute nodes (cc-gpu) | `ssh -n -o BatchMode=yes NODE nvidia-smi` from the broker with the user's key | if the site adds pam_slurm_adopt (ssh only with a job on the node) cc-gpu fails with the ssh error; nothing else depends on it |
| munge auth | hidden socket path /run/munge | if Slurm moves to auth/slurm (sackd): socket /run/slurm/sack.socket — already in DEFAULT_HIDE; doctor "real sbatch binary cannot authenticate" copies the real binary to .contained/run/doctor-sbatch (past the shim) and expects an auth error, in the session and in jobs |
| Python 3.6+ | stdlib only | keep f-strings out? (3.6 has them; avoid walrus/match/`X | Y`) |
| x86_64 | seccomp filter | other arch: no filter (warn) |

Rule for changes: prefer feature detection over version numbers; fail closed for security-relevant pieces
(no munge hiding = do not start), degrade with a warning for convenience pieces.

---------------------------------------------------------------------------------------------------------------
## 7. Known limitations / sharp edges

1. Fakeroot builds need glibc base images (no /etc/subuid entry -> apptainer's userns fakeroot injects a glibc
   fakeroot binary; alpine/musl fails "exec /.singularity.d/libs/fakeroot failed").
2. /tmp in sessions is RAM (tmpfs), limited by tmp_size (32G) together with /var/tmp and /dev/shm; the login
   node's user.slice also throttles at memory.high, so big writes there get slow. Jobs use node-local disk.
3. Protected paths are bound only if they exist at start; new ones are reported at the end (report_changes).
   `git init` inside the sandbox -> .git/config unprotected until next start.
4. Shares not mounted at start are invisible (by design).
5. Pipelines cannot submit Slurm jobs (no broker inside jobs). Local executor inside one job works.
6. Inside jobs, HOME=/tmp/home (empty, deleted). Real home readable at its path.
7. The contained Claude config is shared by all contained projects except its per-project entries (§5.7).
   What remains shared and writable: .claude.json (per-project allowedTools / trust can be pre-set for another
   project; MCP servers there are neutralised), the login, root files like loop.md and daemon.json (so the
   background-session daemon is not usable per project), caches and diagnostics. A session can still READ
   another project's .contained/state/ through the file system, like any other file of that project
   (everything of the user's is readable by design); it cannot write there. An entry a Claude update adds
   lands in the shared group until classified in policy.py; doctor reports it.
8. Codex home (CODEX_HOME) is shared between contained projects; its behaviour files are read-only in sessions
   and its per-project entries are the project's (§5.7): its thread index lists every project's threads (cwd,
   title, first prompt), its memory database, logs and login are shared. `codex exec` outside a git
   repository needs `--skip-git-repo-check` (Codex's own rule). Codex's own sandbox is an inner layer: it works nested (§5 step 2),
   but in its read-only mode (default of `codex exec` and the plugin's reviews) Codex itself blocks every
   socket, so the cc-* commands fail there with "broker not reachable (Operation not permitted)";
   workspace-write has network_access = true (seeded) and works.
9. NFS attribute caching: never compare mtimes across a session (report_changes hashes contents).
10. NFS negative dentry caching: a file written by a job on another node can look MISSING on the login node
    for a while (seen: pulled image 'not found' right after cc-pull). broker.isfile_fresh() lists the parent
    directory first (revalidates) and retries briefly; use it for every existence check of job outputs.
    functest.wait_file does the same inside the sandbox.
11. Protected files that git tracks (.vscode/, .envrc, .contained/, .contained_claude.sh, ...) can't be
    rewritten by git inside: a checkout/merge/reset that changes one of them updates everything else and
    leaves that file as it is (shown as modified; `unable to unlink`). `git checkout -f BRANCH` and `git reset
    --hard` then give a clean state (verified). CLAUDE_environment.md tells the agent; the launcher prints a
    note at start when such a file differs between local branches (tracked_protected_note).
12. The change snapshot ignores hooks/*.sample (git never runs them) and records absent paths as None, so a
    fresh `git init` reports its config/hooks/info only.
13. The session memory scope needs a systemd user session on the login node (XDG_RUNTIME_DIR) and systemd >=
    243 (OOMPolicy=); otherwise no limit, with a warning at start.

---------------------------------------------------------------------------------------------------------------
## 8. Verification (re-run after every change)

- `containedclaude test DIR [--jobs] [--build] [--gpu] [--image SIF]`: what SHOULD work. Must print
  EVERYTHING WORKS (skips are fine). The user-facing check; also the first thing to ask a user to run.
  --jobs covers command, array, dependency, cancel, log, usage, --env, pull + run an image.
- `containedclaude doctor DIR`: interactive sandbox probe, including the broker's refusals (limits, other
  projects' jobs, non-GPU nodes, the --env blocklist). Must print ALL CHECKS PASSED.
- `containedclaude doctor DIR --jobs --gpu --image <sif>`: + job without image, job in image, GPU job.
  (DIR must be on a filesystem the compute nodes see, e.g. under ~; the Claude scratchpad /tmp is node-local.)
- By hand after changes to start-up checks: init/start refusals (nested project, a marked write path,
  another user's project), a Codex session in a project (no trust question, answers, writes).
Last full round (2026-11-05, curry0, Claude Code 2.1.318, Codex 0.166.0): `test --jobs --gpu` 43 ok and
`doctor --jobs --gpu` ALL CHECKS PASSED on a home project and on an SDS project (jobs on curry58/59, GPU on
the A40 of curry58); a job with `extra_sbatch = --exclude=curry58,curry59` got ExcNodeList curry[58-59];
Codex in a git project (TUI, workspace-write) and a non-git one (`codex exec`) answered without a trust
question; marker refusals checked by hand with a simulated second user.
After the XDG, broker-regex and test-folder changes (same evening): `test --jobs --gpu` 43 ok and `doctor --jobs
--gpu` ALL CHECKS PASSED on a home project while sessions of another project kept starting (no stand-in folder
left anywhere); a new install with XDG_DATA_HOME and XDG_CONFIG_HOME elsewhere: `test` 27 ok, `doctor` passed;
the post-update self-test left no project, registry entry or runtime folder behind.
Image jobs without --no-mount bind-paths and with IMAGE_ENV, checksums from cc-build/cc-pull: `test --jobs
--build` 47 ok (name resolution, time zone, variables and both checksums checked), `doctor --jobs --image <sif>`
ALL CHECKS PASSED; in an image job `getent hosts pypi.org` resolves, /etc/hosts and the time zone are the node's.
--after as a comma list and with --kill-on-invalid-dep, leftovers removed (under_nsinit, nsinit without
--pid-base, image variables now all in IMAGE_ENV): `test --jobs --build` 49 ok (a job after two parents, one
an array; a job after a failed one ends CANCELLED without running), `doctor --jobs --image <sif>` ALL CHECKS
PASSED; `--after 1,abc` and `--after N,` are refused.
cc-log/cc-wait for a job without a log: `test --jobs` 47 ok (cc-log of a waiting job says PENDING (Dependency),
of one cancelled by a failed parent "never started: CANCELLED"); cc-wait prints the same line; an id of
another project or a typo is refused by the broker.
Shell guard (2026-11-06, Claude Code 2.1.319): `test` 28 ok (bash and zsh, 11 cases each); through a real
terminal, Enter started a contained `claude -p` (the launcher's self-check ran) and `claude --version` passed
through; `install.sh --shell-guard` / `uninstall.sh` with a stand-in HOME: an older line replaced, a symlinked
.bashrc kept, zsh's .zshrc created, an unsupported shell refused before anything is installed; without the flag
the hint shows only while the line is missing (bash and zsh; none for other shells).
Description kept at another start (2026-11-06): `test` 29 ok; the new check reads the notice with the old
write_envdoc and the description with the new one; `env` says "up to date" for unchanged files, recreates a
deleted notice, and a config change reaches new sandboxes while a running one keeps its description.
Messages within the project (2026-11-06, Claude Code 2.1.319): `test` 30 ok; three sessions in tmux (A and C
with --remote-control, in two projects; B local in A's project): A -> B and B's reply delivered; A -> C by name
and as "name [ref]" refused with the hook's text; a message from an unsandboxed Remote Control session to A
not delivered (A's transcript: only the queue entry and the UI notice); RemoteTrigger missing from A's tools;
records left by killed sessions don't count (their sockets don't answer).
Server sessions (2026-11-06, Claude Code 2.1.319): `test` 31 ok, doctor passes; `containedclaude --
remote-control` in a throwaway project (before: refused by Claude Code because of the launcher's MCP flags): a
message from an unsandboxed session started a session there and was not delivered (blocked by hook); that
session has no RemoteTrigger and no mcp__ tools; a local session B in the project: `/mcp` empty, messages to
and from the server's session delivered, a send outside the project refused. With `mcp_from_user_config =
yes` `claude mcp list` shows the account's claude.ai connectors, with `no` none. A normal session runs `claude
--mcp-config F --strict-mcp-config <args>`, and its `/hooks` lists the launcher's hooks as user hooks.
Connectors off (2026-11-06): a server-style session (`--print --resume=<id>` without a transcript) printed
`No conversation found with session ID` followed by "claude.ai MCP servers blocked by enterprise policy",
which the server showed as the session's failure; with `disableClaudeAiConnectors: true` only the real error.
Server command (2026-11-06): `test` 32 ok (shell guard 13 cases each, server arguments 13 cases);
`containedclaude server DIR -- --help` and `cclaude server --help` (outside, and inside a sandbox) print the
help of `claude remote-control`. By hand: servers started with `--no-create-session-in-dir` open no session
at start and then serve sessions from claude.ai; a restart within about a minute of stopping is refused
("already served"), later it works.
Occasional manual check of messages (by hand, e.g. after a larger Claude Code update; `test` and `update` stay
short and don't run it): two throwaway projects P and Q; in tmux `containedclaude P -- --name t-A
--remote-control`, `containedclaude Q -- --name t-C --remote-control`, `containedclaude P -- --name t-B`. Ask
t-A to call ListAgents, then SendMessage to t-B, to t-C, and to t-C as "t-C [ref]": t-B gets its message,
both sends to t-C are refused with peer_guard's text. From an unsandboxed session send one message to t-A: t-A
shows "blocked by hook" and doesn't react. Server mode: `containedclaude server P` (answer the trust
question); one message to it from an unsandboxed session starts a session that shows "blocked by hook";
t-B finds that session in ListAgents (under a name derived from P's folder) and can message it, and it
reports no RemoteTrigger and no mcp__ tools. Afterwards remove the projects' lines from compat.REGISTRY and
their trust entries from CODEX_HOME/config.toml, then the folders.

---------------------------------------------------------------------------------------------------------------
## 9. How to change common things

- New hidden path for everyone: config.DEFAULT_HIDE. Per project: hide_paths in .contained/config.ini.
- Extra writable folder: write_paths (project config; applies to sessions AND jobs).
- New job option: cc (job_opts/opts_dict) -> broker._opts (validate!) -> broker.submit (#SBATCH line).
  Never pass raw strings into the batch script without a regex check: fullmatch (not ^...$), and write the
  value exactly as checked.
- New functional check: functest.py (session_checks / job_checks; use check(name, fn), fn returns a detail
  string, ('skip', why), or raises). New blocking check: probe.py + doctor.probe_args.
- Shell guard: lib/shell-guard.sh (the subcommand lists at the top); its test is functest's "shell guard".
- Messages between sessions: sandbox/hooks/peer_guard.py (formats in §6); its test is functest's "messages
  only within the project", plus the occasional manual check of messages in §8.
- Launcher settings for Claude (hooks, denials, MCP rule): project.claude_settings / session_settings; they
  reach sessions through the bound settings.json (§3, 4), admin through --settings; functest's "the
  launcher's settings reach every Claude process" checks them.
- New subcommand on the host: cli.py. New action for the sandbox: broker.Broker.handle + a cc-* entry.
- Session memory limit: session_mem in site.ini / .contained/config.ini (0 = none); the scope properties are
  compat.SCOPE_PROPS.
- Where projects may live: allowed_roots in site.ini only (empty = anywhere); config.placement_problems.
- Shared data between users' projects: `init --write PATH` (project.check_extra_write_paths validates like a
  project folder and refuses folders that are or contain a project; the folder gets a .contained_writable
  marker, §3 step 1).
- Different cluster: site.ini (partition, gpu_gres, automount, image_binds, limits, tmp_size, session_mem); curry's committed
  defaults are site.curry.ini (change those in git, then copy to site.ini or edit site.ini locally).
- Debug a job: .contained/run/jobs/<stamp>_<name>.{json,sh} + .contained/logs/; rerun by hand outside:
  `/usr/bin/python3 -I lib/containedclaude/jobwrap.py <spec.json>` (on a compute node, e.g. via an interactive
  allocation by the user).
- Debug the sandbox interactively: `containedclaude shell DIR`; print the bwrap command: python -c
  "import sys; sys.path.insert(0,'lib'); from containedclaude import policy, config;
  print(policy.bwrap_args(config.Config('DIR'), 'session'))".
