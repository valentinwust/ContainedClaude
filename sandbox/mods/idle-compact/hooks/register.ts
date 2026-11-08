import type { Register } from 'claude-code'

// ContainedClaude (DEVELOPMENT.md §5.12): when the main session has been idle for AFTER minutes with at least
// MIN_TOKENS of context, the prompt cache (1 hour) is about to expire and the next request would send the whole
// context again at full price. So: ask the agent to write down what a summary would lose, and compact right after
// its answer, while the cache is still warm. Claude Code's own idle compaction skips sessions a Remote Control
// server starts (SDK sessions); this works in every session. Values from the session settings' env (the
// launcher writes them from the [claude] section of the config).

// The person sees only this line; hooks/notice.py (UserPromptSubmit) recognises it and gives the model the
// notice itself as hidden context (a mod's own hooks never see its own $.prompt.submit). Keep both in step.
const LINE = (min: number, k: number) =>
  `Idle for ${min} min with ${k}k tokens: notes, then compaction before the prompt cache expires.`
const INSTRUCTIONS =
  'Keep file paths, decisions, open work, job IDs and where notes were written, word for word.'

let timer: { cancel: () => void } | undefined
let noticed = false

const number = async (value: Promise<string | undefined>, fallback: number) => {
  const n = Number(await value)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

export const register: Register = on => {
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      timer?.cancel()
      timer = undefined
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined) return result            // subagents are never idle
    timer?.cancel()
    timer = undefined
    if (noticed) {                                         // the answer to the notice: compact now
      noticed = false
      if (e.reason === 'answer') {
        $.command.run({ command: 'compact', args: INSTRUCTIONS }).catch(err =>
          $.ui.log(`idle-compact: /compact failed: ${String(err)}`, { to: 'debug' }))
      }
      return result
    }
    const after = await number($.env.get('CONTAINEDCLAUDE_IDLE_COMPACT_AFTER'), 50)
    const minTokens = await number($.env.get('CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS'), 200000)
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens < minTokens) return result
    timer = $.clock.after(after * 60000, () => {
      timer = undefined
      noticed = true
      $.prompt.submit({ text: LINE(Math.round(after), Math.round(tokens / 1000)) }).catch(err => {
        noticed = false
        $.ui.log(`idle-compact: notice failed: ${String(err)}`, { to: 'debug' })
      })
    })
    return result
  })
}
