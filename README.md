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

Optional, both undone by `uninstall.sh`:
- `install.sh --shell-guard` adds a line to `~/.bashrc` (or `~/.zshrc`): `claude` or `codex` typed in a
  contained project asks first (Enter: contained, `u`: as usual; `command claude` skips it). `cclaude` and
  `ccodex` start them contained in the current folder's project, `cclaude server` the Remote Control server.
- `install.sh --extras` adds the compaction notice and idle compaction (`[claude]`, see Configure) to your own
  Claude Code too, in `~/.claude/settings.json`, with the values of `site.ini` as they are then: run it again
  after changing them.

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

## Jobs
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
`containedclaude DIR -- --remote-control` lets you follow a session from claude.ai or the Claude app.
`containedclaude server DIR` (or `cclaude server`) runs the Remote Control server: keep it running in tmux and
start contained sessions in the project from claude.ai/code or the app.
- Its sessions share the server's sandbox and limits: at most 6 at once (`--capacity N`).
- Stopped with Ctrl+C and started again in that folder within 4 hours, it takes back its sessions: the first at
  once, the others when you open or message them. For about a minute after a stop, claude.ai still refuses a
  new server there ("already served").
- Worktree mode (`--spawn worktree`, `w`) is not supported: those checkouts hold only what git tracks, so your
  data and results would be missing.

## Codex
Codex runs in the same sandbox with the same limits: `containedclaude shell DIR -- codex` (`ccodex` with the
shell guard), or from Claude through the Codex plugin. It has its own login and config, shared by all
contained projects (your `~/.codex` stays hidden); change the config with `containedclaude admin codex`. Once:
```bash
containedclaude shell ~/projects/myanalysis -- codex login --device-auth      # Codex's own login
containedclaude admin          # the plugin for Claude: /plugin marketplace add openai/codex-plugin-cc,
                               # /plugin install codex@openai-codex, then /exit
```
For long jobs, `background_terminal_max_timeout = 7200000` above the first `[section]` of
`~/.local/share/containedclaude/codex-home/config.toml` lets Codex wait on a command for 2 h instead of 5 min.

## Other features
- **Worker agent types**: besides Claude's own, contained sessions can spawn `cc:worker` (5-minute prompt
  cache, for steady work; it waits at most a few minutes and is told to hand longer waits back) and
  `cc:job-worker` (1-hour cache, for tasks that wait on jobs, so a wait doesn't send the whole context again). Both compact at about 180k tokens; the spawning agent picks the type and, with
  the Agent tool's `model`, Sonnet or Opus.
- **Waits on jobs**: in contained Claude sessions, a `cc-wait` (also `cc-run --wait`, `cc-log -f`) in the
  foreground returns after at most 55 minutes with "still running", instead of being moved to the
  background mid-wait after Bash's 2-minute default. You can interrupt it.
- **The same instructions for many texts** (summarise, classify, extract): `cc-ask-batch prompt.md tasks.csv`
  makes one model call per row (columns `text`, `out`: the reply's file), without tools and on your
  subscription, with the shared prompt read from the prompt cache. `--model` (haiku by default), `--effort` (more thinking), `--schema`
  (the reply as JSON in that shape), `--no-cache` (cheaper for a lone call or a prompt under ~500 tokens), `-j` (8 at a time), `--budget USD`
  (starts no row past it). After the first rows it prints an estimate for the rest, and a hint if the other
  cache setting would cost over 10% less; `cc-ask prompt.md text.txt reply.md` for one text,
  `cc-ask --help` for how to keep the cache hits.

## Configure
The project's `.contained/config.ini` (read-only for the agent) lists every setting with a comment. It
overrides `site.ini` in the clone, which overrides the built-in defaults; changes apply to the next session or
job.
- `[sandbox]`, `[jobs]`: write paths, shares to mount, hidden and protected paths, session limits, job defaults
  and limits, extra `sbatch` options.
- `[claude]`, compaction notice and idle compaction: `compact_notice` tells an agent this many tokens before
  auto-compaction that it may want to write down what matters (25000; 0 = never). With `idle_compact`, a
  session idle for `idle_compact_after` minutes (50) with at least `idle_compact_min_tokens` (200000) is asked
  for notes and compacted right after its answer, before the 1-hour prompt cache expires and your next message
  would send the whole context again at full price. Claude Code does that itself only in a terminal; this
  covers Remote Control sessions too. Past `compact_offer` tokens (180000; 0 = never) the main session is told
  once that it may ask for compaction when much of its context is no longer needed: `cc-compact "<what to
  keep>"` compacts it once the turn ends, and it is prompted to continue.

Model, effort and auto-compaction window are Claude's and Codex's own settings, in the contained config shared
by all projects (change them with `containedclaude admin` / `admin codex`, or edit the files):
- Claude: `modelSettings` (`autoCompactWindow`, `effortLevel` per model; without a window a model compacts near
  its full context window) and `effortLevel` in `~/.local/share/containedclaude/claude-config/settings.json`,
  copied at each sandbox start (a running server keeps the old values).
- Codex: `model`, `model_reasoning_effort`, `model_auto_compact_token_limit` above the first `[section]` of
  `~/.local/share/containedclaude/codex-home/config.toml`, used by every new Codex session.

## Check and update
```bash
containedclaude test ~/projects/myanalysis     # what should work (seconds); --jobs, --gpu, --build, --image add more
containedclaude doctor ~/projects/myanalysis   # what must be blocked; --jobs --gpu also inside jobs
containedclaude update ~/projects/myanalysis   # update the clone, Claude Code and Codex, then check
```
`test` must end with `EVERYTHING WORKS`, `doctor` with `ALL CHECKS PASSED`; run both after updating bwrap,
apptainer or Slurm. After a Claude Code or Codex update the first start runs the doctor itself, and once a day
it says when something is behind. One clone serves all projects. Plugins: `containedclaude admin`, then
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
