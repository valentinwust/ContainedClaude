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

export const register: Register = on => {
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
