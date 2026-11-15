import type { Register } from 'claude-code'

// ContainedClaude (DEVELOPMENT.md §5.13): waits on jobs, and the Bash timeouts of cc:worker. No state, and
// nothing here changes a call's input (auto mode refuses a call a hook changed, and the agent then copies the
// changed text): a call is refused with what to do, or runs as written and gets a note after its output.

// Waits on jobs (cc-wait, cc-run --wait, cc-log -f) that the agent runs in the foreground: Bash's timeout (2
// minutes unless set) would move them to the background mid-run, and the agent then works around that (sleep,
// cc-jobs, another wait). So they must carry the longest timeout, 57 minutes (BASH_MAX_TIMEOUT_MS,
// project.claude_env: the most any command may set, so none outlasts the 1-hour cache). The waits themselves
// return after 55 minutes (CALL_LIMIT in sandbox/bin/cc; "still running ... cc-wait JOBID" to go on): the next
// request then still finds the 1-hour cache and renews it.
// WAIT: one of the three where a command starts in the call (also after `;`, `&&`, `|`, a newline, `then`, and
// behind `VAR=x`), so a call that only names them (grep, echo, a commit message) is left alone. Of 5840 Bash
// calls in four projects (2026-11) it finds the 599 real ones; a script written with a heredoc that has such a
// line is refused once too, harmlessly. Not found: a wait behind another command (`timeout 500 cc-wait ...`).
const WAIT = new RegExp(
  '(^|[;&|\\n(`{]|\\b(?:then|do|else)\\s)\\s*(?:[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+)*' +
  '(cc-wait(\\s|$)|cc-run(\\s[^;&|\\n]*)?\\s--wait(\\s|$)|cc-log(\\s[^;&|\\n]*)?\\s-f(\\s|$))')
const BASH_MAX_MS = 3420000 // = BASH_MAX_TIMEOUT_MS in project.claude_env
const TIMEOUT = (isMain: boolean) =>
  'Not run: this call waits on jobs in the foreground (cc-wait, cc-run --wait or cc-log -f), which needs the ' +
  `long Bash timeout (the default would move it to the background after 2 minutes). Run the same command again ` +
  `with timeout: ${BASH_MAX_MS}; the wait returns by itself after at most 55 minutes ("still running"), then ` +
  'run it again.' + (isMain ? ' Or run it in the background (run_in_background) and go on working.' : '')

// cc:worker (5-minute cache) is not the agent for long waits: after a pause of 5 minutes its whole context is
// sent again. So its Bash timeouts are at most 4 minutes (moved to the background then, not killed), its
// foreground waits must carry exactly that, and when a command of its is moved to the background on timeout or
// it puts a wait in the background, it gets NOTE: hand back or speed up.
const WORKER = 'cc:worker'
const WORKER_MAX_MS = 240000
const WORKER_TIMEOUT = (isWait: boolean) =>
  `Not run: you are cc:worker, and your Bash timeouts are at most ${WORKER_MAX_MS} (4 minutes; your prompt ` +
  'cache lasts 5)' +
  (isWait ? ', which a wait on jobs in the foreground (cc-wait, cc-run --wait, cc-log -f) needs in full' : '') +
  `. Run the same command again with timeout: ${WORKER_MAX_MS}.`
const NOTE =
  'Note: you are cc:worker, and your prompt cache lasts 5 minutes: a pause of 5 minutes or more sends your ' +
  'whole context again, so you are not the worker for long waits. If the task needs them, stop here and report ' +
  'the job IDs and where you stand, and ask your caller to give the rest to a cc:job-worker. If it doesn\'t, ' +
  'find a faster way (a smaller test or input, fewer steps).'
// Any other subagent that puts a job wait in the background: ending its turn to wait reports to its caller as
// if it were done (the caller may then start the same wait), so it is told to wait in the foreground.
const BG_NOTE =
  'Note: you put a wait in the background. As a subagent, if you end your turn to wait, your caller gets your ' +
  `report as if you were done. Wait in the foreground instead (cc-wait JOBID, with timeout: ${BASH_MAX_MS}): ` +
  'it returns after at most 55 minutes with "still running", then run it again.'

export const register: Register = on => {
  const types = new Map<string, string>()

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    let isWorker = false
    if (e.agentId !== undefined) {
      if (!types.has(e.agentId))
        for (const a of await $.agent.list()) types.set(a.id, a.type)
      isWorker = types.get(e.agentId) === WORKER
    }
    const isWait = WAIT.test(e.command)
    const isBackground = e.run_in_background === true
    const timeout = e.timeout ?? 0
    if (isWorker && (timeout > WORKER_MAX_MS || (isWait && !isBackground && timeout < WORKER_MAX_MS)))
      return { deny: WORKER_TIMEOUT(isWait && timeout <= WORKER_MAX_MS) }
    if (!isWorker && isWait && !isBackground && timeout < BASH_MAX_MS)
      return { deny: TIMEOUT(e.agentId === undefined) }
    const ran = await next(e)
    if (ran.deny !== undefined || e.agentId === undefined) return ran
    const result = ran.result as { timedOutAfterMs?: number } | undefined
    const note = isWorker
      ? (result?.timedOutAfterMs !== undefined || (isWait && isBackground) ? NOTE : undefined)
      : (isWait && isBackground ? BG_NOTE : undefined)
    return note === undefined ? ran : { ...ran, context: [...(ran.context ?? []), note] }
  }).catch(($, e, next) => next(e))
}
