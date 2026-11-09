import type { Register } from 'claude-code'

// ContainedClaude (DEVELOPMENT.md §5.13): compaction when the main session asks for it. Past compact_offer tokens,
// hooks/context_notice.py tells the main session once that it may end a reply with a line starting with MARK.
// When a main-loop answer ends so, this runs /compact with the rest of that line as instructions (as the
// idle-compact mod does: $.session.compact refuses in SDK sessions), then prompts the agent to go on. Below
// 2/3 of compact_offer it doesn't compact (too small to pay off) and says so. A mod can't compact a subagent
// (2.1.319); the worker types in agents/ compact at ~180k instead. Keep MARK in step with context_notice.py.
const MARK = '[[compact]]'
const INSTRUCTIONS =
  'Keep file paths, decisions, open work, job IDs and where notes were written, word for word.'
const DONE = 'Compacted as you asked. Continue where you left off.'
const SMALL = (k: number) =>
  `Not compacted: the context holds only ${k}k tokens, too little for compaction to pay off. Continue where you left off.`

// Waits on jobs (cc-wait, cc-run --wait, cc-log -f) that the agent runs in the foreground: Bash's timeout (2
// minutes unless set) would move them to the background mid-run, and the agent then works around that (sleep,
// cc-jobs, another wait). So they get 57 minutes (BASH_MAX_TIMEOUT_MS, project.claude_env: also the most any
// command may set, so none outlasts the 1-hour cache), and CONTAINEDCLAUDE_WAIT_LIMIT makes them return before
// that ("still running ... cc-wait JOBID" to go on): the next request then still finds the 1-hour cache and
// renews it. Waits the agent puts in the background stay uncapped.
const WAIT = /(^|[\s;&|(`])(cc-wait(\s|$)|cc-run(\s[^;&|]*)?\s--wait(\s|$)|cc-log(\s[^;&|]*)?\s-f(\s|$))/
const BASH_MAX_MS = 3420000 // = BASH_MAX_TIMEOUT_MS in project.claude_env
const WAIT_LIMIT_MIN = 55   // checks every 30 s or less: returns by 55.5 min

// cc:worker (5-minute cache) is not the agent for long waits: after a pause of 5 minutes its whole context is
// sent again. Every Bash timeout it sets is lowered to 4 minutes (moved to the background then, not killed);
// its foreground waits return after 3 (by 3.5), so a short job can still be waited on (Claude Code itself
// moves a background subagent's command to the background at 3). When a wait stops there, any of its commands
// is moved to the background on timeout, or it puts a wait in the background, it gets NOTE: hand back or speed
// up; CONTAINEDCLAUDE_WAIT_HANDBACK drops cc-wait's "to go on waiting" hint.
const WORKER = 'cc:worker'
const WORKER_WAIT_MIN = 3
const WORKER_MAX_MS = 240000
const NOTE =
  'Note: you are cc:worker, and your prompt cache lasts 5 minutes: a pause of 5 minutes or more sends your ' +
  'whole context again, so you are not the worker for long waits. If the task needs them, stop here and report ' +
  'the job IDs and where you stand, and ask your caller to give the rest to a cc:job-worker. If it doesn\'t, ' +
  'find a faster way (a smaller test or input, fewer steps).'
// Any other subagent that puts a job wait in the background: ending its turn to wait reports to its caller as
// if it were done (the caller may then start the same wait), so it is told to wait in the foreground.
const BG_NOTE =
  'Note: you put a wait in the background. As a subagent, if you end your turn to wait, your caller gets your ' +
  'report as if you were done. Wait in the foreground instead (cc-wait JOBID): it returns after at most 55 ' +
  'minutes with "still running", then run it again.'

export const register: Register = on => {
  const types = new Map<string, string>()

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    let worker = false
    if (e.agentId !== undefined) {
      if (!types.has(e.agentId))
        for (const a of await $.agent.list()) types.set(a.id, a.type)
      worker = types.get(e.agentId) === WORKER
    }
    const wait = WAIT.test(e.command)
    if (!worker) {
      if (!wait) return next(e)
      if (!e.run_in_background)
        return next({ ...e, timeout: BASH_MAX_MS, command: `export CONTAINEDCLAUDE_WAIT_LIMIT=${WAIT_LIMIT_MIN}; ${e.command}` })
      const ran = await next(e)
      return e.agentId === undefined || ran.deny !== undefined ? ran : { ...ran, context: [...(ran.context ?? []), BG_NOTE] }
    }
    const capped = (e.timeout ?? 0) > WORKER_MAX_MS ? { ...e, timeout: WORKER_MAX_MS } : e
    const ran = await next(e.run_in_background || !wait ? capped : {
      ...e, timeout: WORKER_MAX_MS,
      command: `export CONTAINEDCLAUDE_WAIT_LIMIT=${WORKER_WAIT_MIN} CONTAINEDCLAUDE_WAIT_HANDBACK=1; ${e.command}`,
    })
    if (ran.deny !== undefined) return ran   // not isError: cc-wait's "still running" exits 2
    const r = ran.result as { timedOutAfterMs?: number }
    const long = r?.timedOutAfterMs !== undefined || (e.run_in_background && wait) ||
      /still running after|stopped following after/.test(ran.text ?? '')
    return long ? { ...ran, context: [...(ran.context ?? []), NOTE] } : ran
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined || e.reason !== 'answer') return result
    const last = result.text.trimEnd().split('\n').pop()?.trim() ?? ''
    if (!last.startsWith(MARK)) return result
    const offer = Number(await $.env.get('CONTAINEDCLAUDE_COMPACT_OFFER'))
    if (!Number.isFinite(offer) || offer <= 0) return result
    const tokens = (await $.session.usage()).context.tokens ?? 0
    const fail = (err: unknown) => $.ui.log(`cc: compaction on request failed: ${String(err)}`, { to: 'debug' })
    if (tokens < (offer * 2) / 3) {
      $.prompt.submit({ text: SMALL(Math.round(tokens / 1000)) }).catch(fail)
      return result
    }
    const keep = last.slice(MARK.length).trim()
    $.command.run({ command: 'compact', args: keep ? `${keep} ${INSTRUCTIONS}` : INSTRUCTIONS })
      .then(() => $.prompt.submit({ text: DONE }))
      .catch(fail)
    return result
  })
}
