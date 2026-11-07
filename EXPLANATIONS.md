# What ContainedClaude allows, what it blocks, and why

This explains the rules and the design choices behind them. For usage see README.md; for implementation
details and debugging see DEVELOPMENT.md.

> **Not perfect containment.** What follows is a best effort, not a guarantee, and you shouldn't rely on the
> sandbox alone. Reviews keep finding gaps. Don't give a contained agent anything that must not leak. Review
> what it wrote before you run any of it outside the sandbox, and keep backups.

## The idea in one paragraph
An agent should be able to do real work (read your data, write results, run cluster jobs and containers) but
nothing with a lasting effect outside one project folder, and nothing that lets it act as you somewhere
else. Enforcement comes from **what the sandbox can reach** (mounts, hidden sockets and credentials, Linux
namespaces), not from scanning commands for forbidden words. Hooks only explain things, so they never block
a harmless command because some text happened to match. Jobs re-enter **exactly the same sandbox** on the
compute node, so a job can't do anything the interactive session couldn't.

## Allowed / blocked at a glance

### In the interactive session (login node)
| | allowed? | how / why |
|---|---|---|
| Read files you can read | yes | the whole filesystem is visible read-only |
| Write inside the project | yes | the only writable place (plus `write_paths` from the config, plus a private in-memory `/tmp`, which together with `/var/tmp` and `/dev/shm` is limited to `tmp_size`, 32G by default, so a session can't fill the login node's memory). The project and `write_paths` may not be or contain your home, `~/.config`, `~/.local/bin` or the clone (shell startup files, git config and the launcher run outside the sandbox); `init` and the launcher refuse |
| Caches (pip, npm, Hugging Face, conda packages, …) | yes, in `<project>/.cache/` | the home folder is read-only, so `XDG_CACHE_HOME` and friends point into the project. Jupyter kernels started in a session keep their connection files in a private `/tmp` folder |
| Use lots of CPU, processes or bandwidth on the login node | limited | a session is pinned to `session_cpus` cores (8) at low priority, with the BLAS/OpenMP thread variables set accordingly, and capped at `session_tasks` (300) processes+threads by its cgroup. A Remote Control server and all the sessions it starts share these limits (and `session_mem`), so `containedclaude server` runs at most 6 sessions at once unless given `--capacity N`. The CPU part is a courtesy cap (affinity could be widened again; a hard `CPUQuota` is used where the user manager delegates the cpu controller). Bandwidth can't be capped without root: the agent is told that large transfers are jobs |
| Use lots of memory on the login node | no | a session as a whole (all its processes, plus the files in `/tmp`) is limited to `session_mem` (32G by default) by a systemd user scope. The kernel kills the process that exceeds it (`Killed`, exit 137) and a hook tells the agent what happened; the session itself goes on. Jobs are limited by Slurm |
| Write anywhere else (home, other projects, shared storage) | no | read-only file system. Holds for network shares too, including ones mounted later (see "Network shares"), and through symlinks in the project (they resolve to the read-only view; a symlink loop just fails with ELOOP). Project and `write_paths` are checked on their real paths, so a symlink to your home or the clone can't become a project; a protected file that is a symlink protects its target |
| Change `.git/hooks`, `.git/config`, `.claude/settings*.json`, `.mcp.json`, `.envrc`, `.vscode/`, `.contained/`, `.contained_claude.sh`, `CLAUDE_environment.md` | no | these would run code **outside** the sandbox later, when *you* use git, Claude or your editor. Kept read-only, also in sub-folders up to 3 levels deep (nested git repositories, another project's `.contained/`) and in submodules' git folders (`.git/modules/…`). Folders that hold such files (`.git`, `.claude`, the folders leading to a nested repository) can't be renamed or deleted during a session ("Device or resource busy"). Anything new of this kind created during a session is reported when the session ends, and saved in `.contained/state/changes-*.txt` (in case the ssh connection dropped). Git can't rewrite them either: a checkout or merge that changes one of them (say a tracked `.vscode/settings.json`) switches everything else and leaves that file as it is, shown as modified; `git checkout -f BRANCH` / `git reset --hard` give a clean state again (the agent is told so, and the launcher prints a note when such a file differs between your branches) |
| `git commit`, `git log`, branches | yes | only `.git/config` and hooks are read-only |
| ssh keys, gpg agent, Kerberos tickets, `gh`/git/cloud credentials, Jupyter server tokens, shell histories | hidden | they would let the agent log in or run code as you elsewhere |
| Your own `~/.claude`, `~/.claude.json`, `~/.codex` | hidden | they configure your normal, **unsandboxed** sessions; they also hold transcripts of all your projects |
| systemd user session (`systemd-run --user`), your other Claude sessions' sockets, pipewire, … | hidden | `/run/user/<uid>` is empty inside; those sockets start processes outside. The system bus (`/run/dbus`, `loginctl`/`systemctl`) is hidden too |
| Slurm commands directly (`sbatch`, `squeue`, …) | no | Slurm's login socket (munge) is hidden, so even a program that reimplements `sbatch` can't submit (`containedclaude doctor` checks this by running a copy of the real `sbatch` binary inside the sandbox and in a job: it must fail to authenticate). Use `cc-run` etc. |
| apptainer | no (only in jobs) | style decision: containers mean real computation, which belongs on compute nodes |
| Typing into your terminal after the session (TIOCSTI) | no | seccomp filter |
| Gaining privileges via setuid programs (`sudo`, …) | no | no-new-privileges |
| Seeing or signalling your other processes | no | own process namespace |
| Talking to your other Claude sessions (`ListAgents`, `SendMessage`, teammates) | same project only | with Remote Control on, Claude Code also lists the account's Remote Control sessions: a hook refuses sends to them and doesn't deliver their messages (a separate claude.ai account for the contained config separates them fully). The session sockets and records live in a per-project folder; other projects, other users and unsandboxed sessions have other folders, and the sockets of other users are unreachable by permissions anyway. Every session's pid namespace starts at a random number, so two sessions never register under the same pid (Claude names those files after it) |
| Scheduled cloud routines (`RemoteTrigger`) | no | denied in the session's settings: routines run in the cloud with the account's connectors |
| Internet | yes | Claude needs it. See "Not covered" |
| Quick commands (grep, head, small Python) | yes | heavy work should go to jobs (a rule in CLAUDE_environment.md, not enforced) |

### In jobs (compute nodes)
| | allowed? | how / why |
|---|---|---|
| Any software in an apptainer image: your `.sif`, `docker://`, biocontainers | yes | `cc-run --image ...`. Images run unprivileged inside the sandbox, so even a malicious image only gets the sandbox's rights. That's why there's no image allowlist |
| Building images (`cc-build`), pulling (`cc-pull`) | yes | as jobs, inside the sandbox. A definition's `%setup`/`%files` can't touch anything outside the project, so no definition-file validation is needed. Base images must be glibc-based (Debian, Ubuntu, Rocky, micromamba); Alpine/musl fails with fakeroot here |
| Programs of the node's OS without an image (`--image none`) | yes | includes apptainer, so Snakemake/Nextflow can use per-step containers inside one job |
| Read everything you can read; write the project | yes | same rules as the session. Shares are mounted before the job's sandbox starts |
| Large temporary files | yes | `/tmp` is node-local disk, deleted at the end, also when the job is cancelled or hits its time limit. `HOME` is a fresh empty folder there |
| GPU | yes | `--gpu` (the gres from the config, e.g. one A40 shard) |
| Arrays, dependencies on the project's own jobs | yes | `--array`, `--after`, `--after-any` |
| Internet (downloads, SRA, pip/CRAN, registries) | yes | |
| Submitting further Slurm jobs from a job | no | there is no broker inside jobs. Run pipeline tools with their local executor in one job |
| `module load`, cluster software from `/software` | read only | visible, but usually tied to the host's libraries; prefer images |
| Exceeding the project's limits | no | checked by the broker; limits in `.contained/config.ini`. Per job: CPUs, memory, time, array size. Per project: queued + running jobs (every array task counts), and at the same time GPU jobs, total CPUs, total memory (an array throttled with `%N` counts N tasks) |
| `--env` variables | yes, inside the job | set inside the sandbox only. `PATH`, `LD_*`, `APPTAINER_*`, `SLURM_*` and similar are refused: they change how the loader, shells, apptainer or the sandbox tools behave |
| Cancelling or depending on other projects' jobs | no | jobs are tagged per project |
| Logins that need your credentials (EGA, private repos, scp to servers) | no | credentials are hidden. Download such data yourself, or put a project-specific credential into the project (the agent can then read it) |

## Design choices

**Tools live in the clone, not in each project.** The clone is outside every project, so it's read-only
inside every sandbox without any special protection, and one `git pull` updates all projects (important for
security fixes). Projects only get a 10-line starter script, a config file, an owner record and the generated
environment description. `.contained/VERSION` records which clone and commit set a project up.

**One command, `containedclaude`, plus a per-project starter.** `.contained_claude.sh` locates itself and
calls the launcher for its own folder, so it works from any directory.

**Where projects may live.** Only strictly below the site's `allowed_roots` (on curry: the user's own home,
`/mnt/sds-hd/sd17l002/u`, `/mnt/sds-hd/sd17l002/p`) and on the same file system as that root; everything else
is refused. File systems mounted inside a project (a share, a FUSE mount) are bound read-only again, so they
can't be written through the project. The agent gets a writable folder, so it should be a self-contained
project folder on the cluster's own storage, never a share's top level or a mount someone made themselves.
Site-wide so that a project's config can't widen it.
A project belongs to the user who set it up: its config and starter script run outside the sandbox as whoever
starts them, so the launcher refuses projects set up by someone else, or without a record (`init` records the
name in `.contained/state/owner`; file owners would not do, since curry shows every SDS file as `nobody`, and for the
same reason the launcher lists the project and its write paths in git's `safe.directory`, or git would refuse
every repository there). Several users working on one SDS project
each set up their own sub-folder and share a data folder through `write_paths` (`init --write`), which is
checked like a project folder.

**No nested projects.** A project's config and starter script must not be writable from any sandbox. So `init`
and the launcher refuse a project that lies inside another project or inside any project's write path, and a
folder that contains a project. Write paths are often shared folders that other users' projects could end up
in, so each one gets a `.contained_writable` file (read-only inside, one line per project that writes there):
every user's `init` and start refuse a project in or below a folder with such a file. The file is written at
`init --write`, `add-write` and every start, the first time only after checking that no project is inside
yet, and a project's line is removed at its next start once the folder is no longer one of its write paths.
For your own projects the launcher also remembers the write paths of every project ever started
(`~/.local/share/containedclaude/write_paths.txt`).

**A broker instead of Slurm access.** The sandbox can't talk to Slurm at all. A small broker running in the
launcher (outside the sandbox) accepts a few JSON request types over a socket. It builds every batch script
itself from validated values, never from text the agent supplied. Commands are passed as argument lists, not
shell strings. It writes logs, job scripts and its job record only into `.contained/`, which is read-only
inside: Slurm writes logs as you, outside the sandbox, so they never go to a place the agent can prepare. The
socket lies in a folder that is read-only inside too.

**Jobs re-enter the same sandbox.** The first plan was apptainer `--containall` with read-only binds. Testing showed apptainer's read-only binds aren't recursive: binding
`/mnt` read-only left `/mnt/sds-hd` **writable**. So each job instead starts the same bwrap sandbox on the
node, and apptainer runs inside it in unprivileged user-namespace mode. One policy covers everything.

**Network shares.** Shares that the automounter mounts after a sandbox has started arrive in it
*writable* (verified). The sandbox therefore runs in a private mount namespace, where new mounts never
appear, and the launcher mounts the configured shares (`automount`) first. Shares not mounted at the start
are invisible inside rather than unsafe.

**A separate Claude config for contained sessions.** `~/.local/share/containedclaude/claude-config`
(`CLAUDE_CONFIG_DIR`), shared by all contained projects, with one `/login`. Inside a session, its settings,
hooks, plugins, skills, agents, commands, rules and `CLAUDE.md` are read-only, so one project's agent can't
change how other projects' sessions behave. The launcher creates them empty if missing, because only existing
paths can be made read-only. Its `.claude.json` must stay writable (Claude rewrites it constantly), so the
launcher starts Claude with `--strict-mcp-config` and an empty server list: MCP servers configured there and
claude.ai connectors are not used (`mcp_from_user_config = yes` in the project config turns this off). What
Claude reads in the sandbox as the config's `settings.json` is that file with the launcher's hooks and rules
added, so every Claude process there has them, also the sessions `claude remote-control` starts (which get no
launcher flags; for them `allowedMcpServers: []` and `disableClaudeAiConnectors` in those settings keep MCP
off). Per-project entries in
`.claude.json` (for example allowed tools) can still be changed by any contained project's agent; they only
affect sessions that are sandboxed too. Change settings or install plugins with
`containedclaude admin`, whose fixed working folder the launcher marks as trusted. Codex gets its own
`CODEX_HOME` the same way: its `config.toml`, `hooks.json`, `AGENTS.md`, rules, prompts, skills, plugins and
packages are read-only in sessions (`containedclaude admin codex` changes them). The launcher writes the
folder trust for each project into `config.toml` itself, and turns off Codex's background server, which would
install a copy of Codex into that home and run it for every project.

**What a session records or executes stays with the project.** Claude's transcripts (`--resume`),
auto-memory, prompt history, plans and rewind checkpoints, and Codex's session rollouts, are not in those
shared folders: the launcher binds `<project>/.contained/state/claude/` and `.contained/state/codex/` over
their places inside. They move and disappear with the project, and no session can write into another
project's (so one project's agent cannot plant notes in another's memory). The same holds, from a
per-project folder that lives only while sessions run, for what a session sources or runs: Claude's shell
snapshots and hook environment files (part of every command it runs), its session records, and the helper
programs Codex places in its temp folder. Every entry of the two shared folders is classified this way. A
folder a new version adds is kept per project from the next start until it is classified; a new file stays
shared until then. `doctor` (run automatically after a Claude or Codex update) names both. The rest of the
two folders is shared, see the limitations below.

**Codex's own sandbox is an inner layer, kept working.** Codex sandboxes the commands it runs with its own
bubblewrap and seccomp filter, nested inside ours. That only ever restricts further, so nothing depends on it,
but it has to start: the session's `/tmp` is therefore a plain tmpfs mounted on `/tmp` (Codex resolves paths
through the mount table), and its workspace-write mode gets `network_access = true` in the contained Codex
config, since Codex would otherwise block every socket, including the local one the `cc-*` commands use. The
outer sandbox gives sessions the network anyway. In Codex's read-only mode (its default for `codex exec` and
for the plugin's reviews) the `cc-*` commands stay blocked by Codex itself.

**Hooks only explain, they don't enforce.** Commands that can't work in the sandbox (`sbatch`, `ssh`,
`apptainer` on the login node, …) are replaced by a stand-in that prints what to use instead. It only
triggers when the command is actually run, never because a word appears in a heredoc, a document or a grep
pattern. A Write/Edit hook compares only the target *path* with the writable areas, to give a clear
message instead of "read-only file system", and a hook after each command says so when a process was killed by
the session's memory limit (instead of a bare `Killed`). Another tells an agent once when it nears
auto-compaction, so it can write down what a summary would lose. A hook on messages between sessions keeps them within
the project when Remote Control makes other sessions visible. None of these is a security boundary; the
sandbox is.

**How agents learn the rules.** `CLAUDE_environment.md` is regenerated at every start from the current
config, with the actual paths, limits and defaults filled in. On disk it holds only a notice that tells an
agent started without ContainedClaude that it is not contained and that the user should start it with
`containedclaude`; every sandbox (sessions, shells, jobs) mounts the real description from
`.contained/run/CLAUDE_environment.md` read-only in its place. So an agent reads that it is sandboxed only
when it is. `CLAUDE.md` imports it with
`@CLAUDE_environment.md` as its first line, which Claude Code loads at startup for the main session and
sub-agents; `CLAUDE.md` is writable (agents edit it), so the launcher puts that line back at every start. Codex
reads `AGENTS.md`, which points to it. Besides the rules it tells the agent what to suggest when something is
blocked (`containedclaude add-write` for another writable folder, the config keys for shares and job limits,
`containedclaude admin` for Claude settings), so that it asks for the right thing instead of working around it.

**Starting uncontained by mistake.** With `install.sh --shell-guard`, `claude` and `codex` are shell functions
that ask in a contained project whether to start contained (Enter) or not (`u`). They are functions in your
shell, not wrappers on `PATH`: the launcher looks up the real `claude` on `PATH`, so a wrapper there would also
run inside every sandbox and in `claude update`. They only cover your interactive shell (not IDEs or other
programs), so the notice in `CLAUDE_environment.md` stays the safety net.

**The launcher trusts no program a sandbox could have written.** The launcher, the broker and the job wrapper
run outside the sandbox. They start with the system python (`/usr/bin/python3 -I`) and look up `unshare`,
`bwrap`, `sbatch`, `claude`, … only in `PATH` folders that are not inside a ContainedClaude project or a
`write_paths` folder (the same for `LD_LIBRARY_PATH` / `LD_PRELOAD`), also when a project environment is
activated in your shell. Inside the sandbox your `PATH` is unchanged. The starter script runs even before the launcher, so it uses `/bin/bash -p` (which also
ignores `BASH_ENV` and functions from the environment) and bash built-ins only. Older starter scripts are
replaced at the next start.

**Robust against tool updates.** Only Python's standard library is used. Optional flags of bwrap,
apptainer and Claude Code are detected from each tool's `--help` and left out with a warning if missing.
Essential ones stop the start with a clear message. After updates, `containedclaude test` checks that everything
that should work still works, and `containedclaude doctor` checks from the inside that everything that must be
blocked still is.

## Not covered (residual risks)
This list is not complete: the sandbox is defence in depth against mistakes and prompt injection, not a
guarantee (see the note at the top).

- **Network:** open in sessions and jobs. An agent can download things and send data out. Services that
  accept connections without a credential (on localhost or elsewhere) are reachable. Tokens and sockets of
  your own services are hidden, but a service without authentication isn't protected.
- **Code you run yourself outside the sandbox:** notebooks, scripts or Makefiles the agent wrote in the
  project. Protected files cover the *automatic* routes (git hooks, editor and Claude settings), not
  something you explicitly run.
- **Files in the project that tools load automatically:** only the common git, editor and Claude files are
  protected. Before you use the project outside the sandbox, review what the agent changed.
- **Protected files that didn't exist at the start** (e.g. a new `.mcp.json`, or `.git/` after `git init`
  inside the session) aren't read-only during that session. They are reported when it ends. Nested ones are
  only looked for up to 3 folders deep (skipping `.cache`, `node_modules`, environments and the like) and for
  at most a few seconds.
- **Shared contained config:** the contained projects share `.claude.json` (one project's agent can
  pre-approve tools for another project there) and the Codex home except its sessions, history and
  behaviour files: Codex's thread index lists every project's threads by title, folder and first prompt, and
  its memory database is shared. All of
  them are sandboxed. Transcripts, memory and prompt history are per project (see above); a session can still
  *read* another project's `.contained/state/` through the file system, like every other file of yours, it
  just cannot write there.
- **Library variables in your own shell:** if you set `LD_PRELOAD` or `LD_LIBRARY_PATH` to a folder inside a
  project yourself, the launcher's own python loads from there before it can clean them up (as does every other
  program you start from that shell). Don't point these at project folders. A project folder on `PATH` is handled.
- **Kernel or bubblewrap/apptainer vulnerabilities**, and resource use within the configured limits.
