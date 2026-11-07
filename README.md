<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
  <img alt="Claude Contained" src="assets/banner-light.svg">
</picture>

Run Claude Code and Codex on a Slurm cluster in a sandbox. Built for the curry cluster; other sites need their
own `site.ini`.

**TL;DR**
- The agent, and everything it starts, reads everything you can read except credentials, writes only its
  project folder and the folders you add, and runs cluster work only as Slurm jobs through `cc-run`. Those
  jobs run in the same sandbox on the compute node.
- Once: clone and run `install.sh` (below). Per project: `containedclaude init DIR`, then
  `containedclaude DIR`; `/login` once in the first session.
- On curry, projects lie below your home, `/mnt/sds-hd/sd17l002/u/` or `/mnt/sds-hd/sd17l002/p/`. In a shared
  folder everyone sets up their own sub-folder `p/<project>/<you>` and shares data with `--write`.
- Jobs get 4 CPUs / 32G / 4 h by default, at most 72 CPUs / 700G / 24 h each, and 4 GPU jobs at a time. A
  session on the login node gets 32G of memory (`/tmp` included), 8 cores and 300 processes. Each project can
  change these in `.contained/config.ini`.
- `containedclaude test DIR` checks what should work, `containedclaude doctor DIR` what must be blocked, and
  `containedclaude update` updates ContainedClaude, Claude Code and Codex.

> **This is not perfect containment, so don't rely on it alone.** It is a best effort against a confused or
> prompt-injected agent, not a proven security boundary, and it can't protect against bugs in the kernel,
> bubblewrap or apptainer. Don't give a contained agent data or credentials that must not leak (the network
> is open). Before you run anything from the project outside the sandbox, review what the agent changed; every
> session ends with a change report. Keep backups and version control. On curry, run every agent that can
> reach the SDS through ContainedClaude.

What exactly is allowed or blocked and why: [EXPLANATIONS.md](EXPLANATIONS.md). How it works inside and how
to debug it: [DEVELOPMENT.md](DEVELOPMENT.md).

## Install
Needs Linux with unprivileged user namespaces, `bwrap`, `unshare`, `/usr/bin/python3` (3.6 or newer, no extra
packages), Claude Code, and for jobs Slurm and apptainer. Tested on curry (AlmaLinux 9, Python 3.9, bwrap
0.6.3, apptainer 1.5.5, Slurm 26.05).
```bash
git clone https://github.com/valentinwust/ContainedClaude.git ~/ContainedClaude
~/ContainedClaude/install.sh     # links `containedclaude` into ~/.local/bin, creates site.ini from site.curry.ini
```
On another cluster, start with `./install.sh site.example.ini` and adapt `site.ini` (local, not in git):
partition, GPUs, shares to mount, limits and where projects may live. Uninstall:
`~/ContainedClaude/uninstall.sh` (`--state` also removes the contained Claude and Codex login and config),
then delete the clone. Projects stay normal folders.

**Shell guard (optional).** `~/ContainedClaude/install.sh --shell-guard` adds one line to `~/.bashrc` (or
`~/.zshrc`). Typing `claude` or `codex` in a contained project then asks first: Enter starts it contained, `u`
as usual. Elsewhere nothing changes, and `command claude` skips the question. `cclaude` and `ccodex` start
Claude or Codex contained in the current folder's project and pass their arguments on; `cclaude server` starts
the Remote Control server. Everything else (`init`, `shell`, `test`, ...) stays `containedclaude`.

**Extras (optional).** `~/ContainedClaude/install.sh --extras` adds quality-of-life features to your own Claude
Code outside the sandbox too, in `~/.claude/settings.json`. For now that is the notice an agent gets shortly
before auto-compaction. Added once (running it again changes nothing); `uninstall.sh` removes it.

## Set up a project
```bash
containedclaude init ~/projects/myanalysis
# a shared project on the SDS: your own sub-folder is the project, the shared data folder is writable too
containedclaude init /mnt/sds-hd/sd17l002/p/myproject/$USER --write /mnt/sds-hd/sd17l002/p/myproject/data
containedclaude add-write ~/projects/myanalysis /another/folder      # later; applies from the next start
```
`init` adds `.contained_claude.sh`, `.contained/` and `CLAUDE_environment.md`, which `CLAUDE.md` and
`AGENTS.md` point to. If the folder already has a Claude setup, it changes nothing and lists what it would
touch; `--force` backs those files up and goes ahead. `init` and every start check:
- Projects and write paths lie below `allowed_roots` of `site.ini` (see TL;DR), and none of them is or
  contains your home, `~/.config`, `~/.local/bin`, the clone or a credential folder.
- A project belongs to the user who set it up. Projects don't nest: none lies inside another project or its
  write paths.
- The SDS needs a valid Kerberos ticket (`klist`; `kinit` renews it).

Don't hard-link files from elsewhere into a project: the agent editing one edits the original. curry shows
SDS files as owned by `nobody`, so for your own git use outside the sandbox run
`git config --global --add safe.directory '/mnt/sds-hd/sd17l002/*'` (inside, the launcher handles it).

## Start
```bash
~/projects/myanalysis/.contained_claude.sh          # from anywhere
containedclaude                                     # from inside the project
containedclaude ~/projects/myanalysis -- --resume   # Claude arguments after --
containedclaude shell ~/projects/myanalysis         # a bash in the same sandbox
cclaude --resume                                    # with the shell guard, from inside the project
```
Contained sessions share their own Claude login and config: `/login` once in the first session. Transcripts,
memory and prompt history stay with the project (`.contained/state/`). Settings, plugins and skills are
read-only in projects; change them with `containedclaude admin`, a Claude session (still sandboxed) with that
config writable.

## Jobs and tools
Agents learn the rules from `CLAUDE_environment.md` and run cluster work as jobs:
```bash
cc-run analysis.ipynb                           # a notebook as a job
cc-run --cpus 16 --mem 64G -- samtools sort -@ 16 -o out.bam in.bam
cc-run --gpu --image torch.sif train.py         # in an apptainer image (also docker://...)
cc-run --array 1-96%20 -- bash per_sample.sh
cc-wait, cc-jobs, cc-log, cc-cancel, cc-info, cc-usage, cc-cluster, cc-gpu, cc-build, cc-pull
```
Caches (pip, conda, Hugging Face, ...) go to `<project>/.cache/`, new conda environments into the project
(`conda create -p ./envs/x`). Big transfers run as jobs too.

## Remote Control
`containedclaude DIR -- --remote-control` lets you follow and continue a session from claude.ai or the Claude
app. `containedclaude server DIR` (or `cclaude server`) runs the Remote Control server: keep it running in
tmux and start new contained sessions in the project from claude.ai/code or the app. It opens one session at
start. Stopped with Ctrl+C, it keeps its sessions on claude.ai: started again in that folder within 4 hours,
it takes back the first one at once and each other one as soon as you open or message it.
Its sessions share the server's sandbox and limits, so at most 6 run at once (`--capacity N` changes that).
After you stop a server, claude.ai counts it for about a minute longer and refuses a new one in that folder
("already served").

The server's worktree mode (`--spawn worktree`, or `w` while it runs) is not supported. Its separate git
checkouts would add no containment here, and they hold only the files git tracks, so sessions there would
miss your data and results.

## Codex
Codex runs in the same sandbox with the same limits: `containedclaude shell DIR -- codex` (`ccodex` with the
shell guard), or from Claude through the Codex plugin. It has its own login and config, shared by all
contained projects (your `~/.codex` stays hidden); change the config with `containedclaude admin codex`. Once:
```bash
containedclaude shell ~/projects/myanalysis -- codex login --device-auth      # Codex's own login
containedclaude admin          # the plugin for Claude: /plugin marketplace add openai/codex-plugin-cc,
                               # /plugin install codex@openai-codex, then /exit
```
For long jobs, let Codex wait on a running command for its maximum of 2 h instead of 5 min: put
`background_terminal_max_timeout = 7200000` near the top of
`~/.local/share/containedclaude/codex-home/config.toml`, above the first `[section]`.

## Configure
The project's `.contained/config.ini` (read-only for the agent) lists every setting with a comment: write
paths, shares to mount, hidden and protected paths, session limits, job defaults and limits, extra `sbatch`
options. It overrides `site.ini`, which overrides the built-in defaults. Changes apply to the next session or
job.

## Check and update
```bash
containedclaude test ~/projects/myanalysis     # what should work (seconds); --jobs, --gpu, --build, --image add more
containedclaude doctor ~/projects/myanalysis   # what must be blocked; --jobs --gpu also inside jobs
containedclaude update ~/projects/myanalysis   # update the clone, Claude Code and Codex, then check
```
`test` must end with `EVERYTHING WORKS`, `doctor` with `ALL CHECKS PASSED`. Run both after updating bwrap,
apptainer or Slurm. After a Claude Code or Codex update, the first start runs the doctor by itself, and once a
day the first start says when something is behind. One clone serves all projects, so pulling it updates them
all. Contained sessions can't update Claude Code or Codex themselves. Plugins: `containedclaude admin`, then
`/plugin update`.

## Commands
| command | |
|---|---|
| `containedclaude init DIR [--force] [--write PATH ...]` | set up a project folder |
| `containedclaude add-write DIR PATH ...` | more writable folders for an existing project |
| `containedclaude [run] [DIR] [-- claude args]` | start Claude in the sandbox |
| `containedclaude shell [DIR] [-- cmd]` | bash (or a command) in the sandbox |
| `containedclaude server [DIR] [-- args]` | the Remote Control server: contained sessions started from claude.ai |
| `containedclaude admin` / `admin codex` | Claude / Codex with the contained config writable |
| `containedclaude test [DIR] [--jobs] [--build] [--gpu] [--image SIF]` | what should work |
| `containedclaude doctor [DIR] [--jobs] [--gpu] [--image SIF]` | what must be blocked |
| `containedclaude update [DIR] [--check] [--no-pull]` | update the clone, Claude Code and Codex |
| `containedclaude env [DIR]` | regenerate `CLAUDE_environment.md` |
| `cclaude` / `ccodex` [args], `cclaude server` [args] | with the shell guard: Claude / Codex / the server in the current folder's project |

## License
MIT, see [LICENSE](LICENSE).
