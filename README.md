<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
  <img alt="Claude Contained" src="assets/banner-light.svg">
</picture>

Run Claude Code and Codex on a Slurm cluster in a sandbox. The agent (and everything it starts) reads
everything you can read except credentials, writes only its project folder and the folders you add, and runs
cluster work only as Slurm jobs through `cc-run`, which run in the same sandbox on the compute node. The
sandbox enforces; hooks only explain. Built for the curry cluster; other sites need their own `site.ini`.

> **This is not perfect containment, so don't rely on it alone.** It is a best effort against a confused or
> prompt-injected agent, not a proven security boundary, and it can't protect against bugs in the kernel,
> bubblewrap or apptainer. Don't give a contained agent data or credentials that must not leak (the network
> is open). Review what it changed before you run anything from the project outside the sandbox, including
> the change report printed at the end of each session. Keep backups and version control. On curry, run every
> agent that can reach the SDS through ContainedClaude.

What exactly is allowed or blocked and why: [EXPLANATIONS.md](EXPLANATIONS.md). How it works inside and how
to debug it: [DEVELOPMENT.md](DEVELOPMENT.md).

## Install
Needs Linux with unprivileged user namespaces, `bwrap`, `unshare`, `/usr/bin/python3` (3.6 or newer, standard
library only), Claude Code, and for jobs Slurm and apptainer. Tested on curry (AlmaLinux 9, Python 3.9, bwrap
0.6.3, apptainer 1.5.5, Slurm 26.05).
```bash
git clone https://github.com/valentinwust/ContainedClaude.git ~/ContainedClaude
~/ContainedClaude/install.sh     # links `containedclaude` into ~/.local/bin, creates site.ini from site.curry.ini
```
`site.ini` (local, not in git) holds the cluster defaults: partition, GPU gres, shares to mount, limits, and
where projects may live. On another cluster start from `./install.sh site.example.ini`. Uninstall:
`~/ContainedClaude/uninstall.sh` (`--state` also removes the contained Claude and Codex config and login),
then delete the clone. Projects keep working as normal folders.

Optional: `~/ContainedClaude/install.sh --shell-guard` adds one line to `~/.bashrc` (or `~/.zshrc`). Then
`claude` or `codex` typed in a contained project asks first: Enter starts it contained, `u` as usual.
Elsewhere, inside sandboxes and for commands that start no agent (`claude update`, `codex login`, ...)
nothing changes; without a terminal it only warns. `cclaude` and `ccodex` always start contained, `command
claude` skips the question, and `uninstall.sh` removes the line.

## Set up a project
```bash
containedclaude init ~/projects/myanalysis
# a shared project on the SDS: your own sub-folder as the project, a shared data folder writable too
containedclaude init /mnt/sds-hd/sd17l002/p/myproject/$USER --write /mnt/sds-hd/sd17l002/p/myproject/data
containedclaude add-write ~/projects/myanalysis /another/folder      # later; from the next start
```
`init` creates `.contained_claude.sh`, `.contained/config.ini` and `CLAUDE_environment.md`, and adds an import
line to `CLAUDE.md` and a pointer to `AGENTS.md`. If the folder already has a Claude setup, it changes nothing
and lists what it would touch; `--force` backs those files up and goes ahead. `init` and every start check:
- The project and its write paths lie strictly below `allowed_roots` of `site.ini` (on curry: your home,
  `/mnt/sds-hd/sd17l002/u/`, `/mnt/sds-hd/sd17l002/p/`), not on a file system you mounted yourself, and are
  not and don't contain `~`, `~/.config`, `~/.local/bin`, the clone or a credential folder.
- A project belongs to the user who set it up. In a shared folder every user sets up a sub-folder of their
  own (`p/<project>/<you>`) and shares data through `--write`.
- No nesting: no project inside another project or inside any project's write path, and no project that
  contains one. Each write path gets a `.contained_writable` file that keeps every user's projects out of it.
- The SDS needs a valid Kerberos ticket (`klist`; `kinit` renews it). curry shows SDS files as owned by
  `nobody`, so for your own git use outside run `git config --global --add safe.directory
  '/mnt/sds-hd/sd17l002/*'` (inside, the launcher handles it).
- Don't hard-link files from elsewhere into a project: the agent editing one edits the original.

## Start
```bash
~/projects/myanalysis/.contained_claude.sh          # from anywhere
containedclaude                                     # from inside the project
containedclaude ~/projects/myanalysis -- --resume   # Claude arguments after --
containedclaude shell ~/projects/myanalysis         # a bash in the same sandbox
cclaude                                             # with the shell guard: like containedclaude
```
Contained sessions have their own Claude config, shared by all contained projects: `/login` once in the first
session. Transcripts, memory and prompt history stay with the project (`.contained/state/`). Settings,
plugins and skills of that config are read-only in projects. Change them with `containedclaude admin`, a
Claude session with the contained config writable (still sandboxed). Remote Control works
(`-- --remote-control`, or `-- remote-control` for the server that starts sessions from claude.ai; keep it
running in tmux). Contained sessions exchange messages
(`SendMessage`) only with the other sessions of their project and their own agents, also with Remote Control
on, and don't create cloud routines (`RemoteTrigger`).

## Inside: jobs and tools
Agents learn the rules from `CLAUDE_environment.md`, which `CLAUDE.md` imports. On disk it tells an agent
started without ContainedClaude that it is not contained; every sandbox shows the rules in its place
(`.contained/run/CLAUDE_environment.md`, regenerated at every start):
```bash
cc-run analysis.ipynb                           # a notebook as a job
cc-run --cpus 16 --mem 64G -- samtools sort -@ 16 -o out.bam in.bam
cc-run --gpu --image torch.sif train.py         # in an apptainer image (also docker://...)
cc-run --array 1-96%20 -- bash per_sample.sh
cc-wait, cc-jobs, cc-log, cc-cancel, cc-info, cc-usage, cc-cluster, cc-gpu, cc-build, cc-pull
```
Caches (pip, conda, Hugging Face, ...) go to `<project>/.cache/`, new conda environments into the project
(`conda create -p ./envs/x`). Sessions on the login node are limited in memory, cores, processes and `/tmp`;
big transfers run as jobs too.

## Configure
The project's `.contained/config.ini` (read-only for the agent) overrides `site.ini`, which overrides the
built-in defaults. It lists every key with a comment and the default it had at `init`: write paths, shares
to mount, hidden and protected paths, session limits, job defaults, job limits per job and per project, extra
`sbatch` options. Changes apply to the next session or job.

## Check
```bash
containedclaude test ~/projects/myanalysis     # what should work (seconds); --jobs, --gpu, --build, --image add more
containedclaude doctor ~/projects/myanalysis   # what must be blocked; --jobs --gpu also inside jobs
```
`test` must end with `EVERYTHING WORKS`, `doctor` with `ALL CHECKS PASSED`. Run both after updating bwrap,
apptainer or Slurm. After a Claude Code or Codex update, the first start runs the doctor by itself.

## Update
`containedclaude update [DIR]` pulls the clone (which updates every project at once), runs `claude update`
and `codex update` on the host when they are behind, and then checks DIR's project. Once a day the first
start says when something is behind. Contained sessions run the host's Claude Code and Codex and can't
update them. Plugins: `containedclaude admin`, then `/plugin update`.

## Codex
Codex runs in the same sandbox: `containedclaude shell DIR -- codex` (`ccodex` with the shell guard), or from
Claude through the Codex plugin.
Everything that limits Claude limits Codex the same way; Codex's own sandbox is an inner layer. It has its own
home, shared by all contained projects (your `~/.codex` is hidden), with its sessions kept per project. Its
config (`config.toml`, skills, plugins, rules, `AGENTS.md`) is read-only in projects: change it with
`containedclaude admin codex`. The launcher records the folder trust for each project itself.

The Codex plugin for Claude, once:
```bash
containedclaude admin          # inside: /plugin marketplace add openai/codex-plugin-cc
                               #         /plugin install codex@openai-codex, then /exit
containedclaude shell ~/projects/myanalysis -- codex login --device-auth      # Codex's own login, once
```
For long jobs, raise `background_terminal_max_timeout` to its 2 h limit, otherwise Codex is useless for them: it
waits at most that long on a running command (milliseconds, default 300000 = 5 min). On the host, put
`background_terminal_max_timeout = 7200000` near the top of `~/.local/share/containedclaude/codex-home/config.toml`
above the first `[section]` (the launcher appends the per-project trust tables at the end).

## Commands
| command | |
|---|---|
| `containedclaude init DIR [--force] [--write PATH ...]` | set up a project folder |
| `containedclaude add-write DIR PATH ...` | more writable folders for an existing project |
| `containedclaude [run] [DIR] [-- claude args]` | start Claude in the sandbox |
| `containedclaude shell [DIR] [-- cmd]` | bash (or a command) in the sandbox |
| `containedclaude admin` / `admin codex` | Claude / Codex with the contained config writable |
| `containedclaude test [DIR] [--jobs] [--build] [--gpu] [--image SIF]` | what should work |
| `containedclaude doctor [DIR] [--jobs] [--gpu] [--image SIF]` | what must be blocked |
| `containedclaude update [DIR] [--check] [--no-pull]` | update the clone, Claude Code and Codex |
| `containedclaude env [DIR]` | regenerate `CLAUDE_environment.md` |
| `cclaude` / `ccodex` [args] | Claude / Codex in the sandbox of the current folder (`install.sh --shell-guard`) |

## License
MIT, see [LICENSE](LICENSE).
