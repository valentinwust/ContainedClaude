import type { EngineInterface, Register } from 'claude-code'

// ContainedClaude (DEVELOPMENT.md §5.12): the two compactions of the main session that Claude Code doesn't do
// itself, both run when a turn ends.
// - Idle: after AFTER minutes without a turn and with at least MIN_TOKENS of context, the prompt cache (1 hour) is
//   about to expire and the next request would send the whole context again at full price. So the agent is asked
//   to write down what a summary would lose, and the conversation is compacted right after its answer, while the
//   cache is still warm. (Claude Code's own idle compaction skips the SDK sessions a Remote Control server starts.)
// - On request: the agent runs cc-compact (sandbox/bin) once hooks/context_notice.py has offered it.
// One rule for both: a compaction belongs to ONE turn (`pending`, by turnId) and runs only when that turn ends
// with an answer. Anything else (another turn's end, an interrupted turn, a notice that never arrived, this
// module loaded anew) compacts nothing, and leaves a line saying so (`say`); a request whose /compact the
// engine refused (after an interrupt) is tried once more, after the person's next prompt. Nothing here changes a
// tool call's input: auto mode refuses a call a hook changed, and the agent then copies the changed text.
// The compaction is a bare /compact ($.session.compact refuses in SDK sessions) whose summarizer gets the
// instructions through the session.compact hook, so the visible command stays short.

// Idle. The person sees only this short line (submitted asUser: no "plugin sent a message" frame around it);
// hooks/notice.py (UserPromptSubmit) recognises it and gives the model the notice itself as hidden context (a
// mod's own prompt.submit hooks don't see its own $.prompt.submit). Keep the three in step.
const LINE = (min: number, k: number) => `Idle ${min} min, ${k}k tokens: notes, then compaction.`
const IS_LINE = /^Idle \d+ min, \d+k tokens: notes, then compaction\.$/
const INSTRUCTIONS =
  'Keep file paths, decisions, open work, job IDs and where notes were written, word for word.'

// On request. cc-compact is found wherever a command starts in the Bash call (also after `;`, `&&`, `|`, a
// newline, `then`), and the call runs as the agent wrote it. The script only prints UNSEEN (true of a call this
// can't see: inside a script, `bash -c`) and what to keep. Where that line is in the output (a failed
// `a && cc-compact` has none), the request counts, and the agent reads SCHEDULED in the line's place: the
// output is changed, never the input.
const COMPACT = /(^|[;&|\n(`{]|\b(?:then|do|else)\s)\s*cc-compact(?=[\s;&|)`]|$)/
const UNSEEN = /^cc-compact: nothing scheduled: containedclaude did not see this call\..*$/m     // cc-compact's line
const KEEP = /^Keep: (.*)$/m
const SCHEDULED =
  'cc-compact: compaction is scheduled for the end of this turn: finish your reply now; you are prompted to ' +
  'continue then.'
const UNKNOWN_TURN =
  'cc-compact: nothing scheduled: the compaction mod was loaded anew during this turn. Run cc-compact again in ' +
  'your next turn if you still want it.'
const DONE = 'Compacted as you asked. Continue where you left off.'
const FAILED = (why: string) => `The compaction you asked for did not run (${why}). Continue where you left off.`
const RETRY = (why: string) =>
  `containedclaude: the compaction you asked for with cc-compact could not run (${why}). It runs when this turn ` +
  'ends instead.'
const NOT_DONE = (why: string) =>
  `containedclaude: the compaction you asked for with cc-compact was not done (${why}). Run cc-compact again ` +
  'if you still want it.'
const SUBAGENT = 'cc-compact: the command was not run: only the main session can be compacted on request.'
const OFF = 'cc-compact: the command was not run: compaction on request is off in this project (compact_offer = 0).'
const BACKGROUND = 'cc-compact: the command was not run: run cc-compact in the foreground.'
const SMALL = (k: number, min: number) =>
  `cc-compact: the command was not run: the context holds only ${k}k tokens; below about ${min}k compaction ` +
  'costs more than it saves. Carry on.'
const ENDED = {
  aborted: 'the turn was interrupted', refusal: 'the turn ended in a refusal', error: 'the turn ended in an error',
}

type Pending = { turnId: string; instructions: string; onRequest: boolean; retry?: true }
// After an interrupt the engine runs no command a mod asks for until the person sends a prompt. These origins
// are not the person's; any other may be (claude.ai's prompts in a Remote Control session among them), and one
// that isn't only costs a second refusal, which is then said.
const NOT_PERSON = new Set(['task-notification', 'scheduled-trigger', 'peer', 'peer-send-message', 'projects-relay',
  'coordinator', 'observer', 'observer-activity', 'plugin'])
const reason = (err: unknown) => err instanceof Error ? err.message : String(err)

// What went wrong, as a notice row in the conversation (the terminal shows it, claude.ai doesn't, the model
// never reads it, and it stays in the transcript file): the trace of a compaction that didn't happen. If the row is refused, the line
// goes to the client and the debug log. The agent hears of a request's fate through `note` instead.
const say = ($: EngineInterface, line: string) => {
  const text = `containedclaude: ${line}`
  $.session.append({ message: { type: 'system', content: [{ type: 'text', text }] } })
    .then(row => { if (row.deny !== undefined) $.ui.log(text) }, () => $.ui.log(text))
}

export const register: Register = on => {
  let turn: string | undefined          // the main loop's running turn
  let pending: Pending | undefined      // the compaction to run when its turn ends
  let compacting: string | undefined    // the instructions for the /compact this mod is running
  let compacted = false                 // whether that /compact compacted
  let note: string | undefined          // for the agent, with the next prompt: a request that was dropped
  let refused: { why: string; instructions: string } | undefined   // a request the engine refused, to try again
  let retry: string | undefined         // its instructions, for the turn the person's next prompt starts
  let timer: { cancel: () => void } | undefined

  const number = async (value: Promise<string | undefined>) => {
    const n = Number(await value)
    return Number.isFinite(n) && n > 0 ? n : 0
  }
  const kind = (p: Pending) => (p.onRequest ? 'compaction on request' : 'idle compaction')

  on('turn.start', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    timer?.cancel()
    timer = undefined
    if (pending !== undefined && pending.turnId !== e.turnId) {
      say($, `${kind(pending)}: not done, its turn never reported its end`)
      pending = undefined
    }
    turn = e.turnId
    if (IS_LINE.test(e.text.trim())) pending = { turnId: e.turnId, instructions: INSTRUCTIONS, onRequest: false }
    else if (retry !== undefined) pending = { turnId: e.turnId, instructions: retry, onRequest: true, retry: true }
    retry = undefined
    return next(e)
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!COMPACT.test(e.command)) return next(e)
    const set = await $.env.get('CONTAINEDCLAUDE_COMPACT_OFFER')
    if (set === undefined) return next(e)                   // outside the sandbox: no cc-compact there
    if (e.agentId !== undefined) return { deny: SUBAGENT }
    if (e.run_in_background) return { deny: BACKGROUND }
    const offer = Number(set)
    if (!Number.isFinite(offer) || offer <= 0) return { deny: OFF }
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens < offer * 0.9) return { deny: SMALL(Math.round(tokens / 1000), Math.round(offer * 0.9 / 1000)) }
    const ran = await next(e)
    if (ran.deny !== undefined) return ran
    const shown = (ran.result ?? {}) as { stdout?: unknown; stderr?: unknown }
    const key = (['stdout', 'stderr'] as const).find(k => typeof shown[k] === 'string' && UNSEEN.test(shown[k]))
    if (key === undefined) return ran                       // the script didn't run
    const output = shown[key] as string
    const answer = (line: string) => ({
      result: { ...shown, [key]: output.replace(UNSEEN, line) },
      ...(ran.context === undefined ? {} : { context: ran.context }),
    })
    if (turn === undefined) {
      say($, 'compaction on request: not scheduled, the mod was loaded anew during this turn')
      return answer(UNKNOWN_TURN)
    }
    const keep = (KEEP.exec(output.slice(UNSEEN.exec(output)?.index ?? 0))?.[1] ?? '').trim()
    // in the turn of an idle notice, the person is away: what to keep is taken, the prompt to continue is not
    pending = {
      turnId: turn,
      instructions: keep ? `${keep} ${INSTRUCTIONS}` : INSTRUCTIONS,
      onRequest: pending?.turnId !== turn || pending.onRequest,
    }
    return answer(SCHEDULED)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined) return result            // subagents are never idle, and can't be compacted
    if (e.turnId === turn) turn = undefined
    const p = pending?.turnId === e.turnId ? pending : undefined
    if (p !== undefined) {
      pending = undefined
      if (e.reason === 'answer') {
        compacting = p.instructions
        compacted = false
        // The agent was told "scheduled": it hears the outcome, through the prompt to continue (failures: else as
        // a note with the next prompt). After an interrupt the engine runs no command a mod asks for, and accepts no
        // prompt from it, until the person sends another message (one queued before the interrupt doesn't
        // count), and the interrupt empties the queue a waiting /compact is in. So a request the engine refuses
        // is tried once more, at the end of the turn the person's next prompt starts.
        const outcome = async (why?: string) => {
          const text = why === undefined ? DONE : FAILED(why)
          const sent = await $.prompt.submit({ text, asUser: true }).catch(err => ({ drop: String(err) }))
          if (sent.drop === undefined) return
          say($, `${kind(p)}: the prompt to continue was not delivered: ${sent.drop}`)
          if (why !== undefined) note = NOT_DONE(why)
        }
        $.command.run({ command: 'compact' })
          .then(async () => {
            if (!compacted) say($, `${kind(p)}: /compact ran, but nothing was compacted`)
            if (p.onRequest) await outcome(compacted ? undefined : '/compact compacted nothing')
          }, async err => {
            const why = `/compact did not run: ${reason(err)}`
            if (!p.onRequest) return say($, `${kind(p)}: ${why}`)
            if (p.retry) {
              say($, `${kind(p)}: ${why}, also on the second try`)
              return outcome(why)
            }
            say($, `${kind(p)}: ${why}; tried again when the turn of the person's next prompt ends`)
            refused = { why, instructions: p.instructions }
          })
          .catch(err => say($, `${kind(p)}: ${String(err)}`))
          .finally(() => { compacting = undefined })
        return result
      }
      say($, `${kind(p)}: not done, ${ENDED[e.reason]}`)
      if (p.onRequest) note = NOT_DONE(ENDED[e.reason])
    }
    // idle: the timer runs from the end of every turn of the main loop to the start of the next
    timer?.cancel()
    timer = undefined
    const after = await number($.env.get('CONTAINEDCLAUDE_IDLE_COMPACT_AFTER'))
    const minTokens = await number($.env.get('CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS'))
    if (!after || !minTokens) return result               // idle compaction is off
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens < minTokens || turn !== undefined) return result
    timer = $.clock.after(after * 60000, () => {
      timer = undefined
      $.prompt.submit({ text: LINE(Math.round(after), Math.round(tokens / 1000)), asUser: true })
        .then(sent => {
          if (sent.drop !== undefined) say($, `idle compaction: the notice was not delivered: ${sent.drop}`)
        })
        .catch(err => say($, `idle compaction: the notice failed: ${String(err)}`))
    })
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.command !== undefined) return next(e)
    const add: string[] = []
    if (note !== undefined) add.push(note)
    note = undefined
    if (refused !== undefined && !NOT_PERSON.has(e.origin.kind)) {
      add.push(RETRY(refused.why))
      retry = refused.instructions
      refused = undefined
    }
    return add.length ? next({ ...e, context: [...(e.context ?? []), ...add] }) : next(e)
  }).catch(($, e, next) => next(e))

  on('session.compact', async ($, e, next) => {
    if (e.agentId !== undefined || compacting === undefined) return next(e)
    const instructions = compacting
    compacting = undefined
    const done = await next({ ...e, instructions })
    compacted = done.skip === undefined
    return done
  }).catch(($, e, next) => next(e))
}
