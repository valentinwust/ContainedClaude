import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

// Each hook registered here stands for the engine beneath the mod. `world` records what reached it.
const INSTRUCTIONS = 'Keep file paths, decisions, open work, job IDs and where notes were written, word for word.'
const LINE = 'Idle 50 min, 160k tokens: notes, then compaction.'
const UNSEEN = 'cc-compact: nothing scheduled: containedclaude did not see this call. Run cc-compact as a command of its own (not inside a script or bash -c).'
const DONE = 'Compacted as you asked. Continue where you left off.'
const MIN = 60000

type Options = {
  env?: Record<string, string>
  tokens?: number
  drop?: string                              // the engine declines every prompt the mod submits
  output?: string                            // what Bash prints
  toStderr?: boolean
  compaction?: 'runs' | 'skipped' | 'none' | 'rejects'
  subagentFirst?: boolean                    // a subagent's own compaction comes just before the mod's /compact
}

type Engine = Parameters<Extract<Parameters<typeof test>[1], (...a: never[]) => unknown>>[0]
const MESSAGES = [{ role: 'user' as const, text: 'summary', toolUses: [] }]

const world = ($: Engine, on: On, options: Options = {}) => {
  const seen = {
    prompts: [] as { text: string; context?: readonly string[]; origin: unknown }[],
    commands: [] as { command: string; args: string }[],
    compactions: [] as (string | undefined)[],
    bash: [] as Record<string, unknown>[],
  }
  const state = { tokens: options.tokens ?? 160000, drop: options.drop, compaction: options.compaction ?? 'runs' }
  const clock = mock.clock(on)
  mock.env(on, options.env ?? {
    CONTAINEDCLAUDE_IDLE_COMPACT_AFTER: '50', CONTAINEDCLAUDE_IDLE_COMPACT_MIN_TOKENS: '150000',
    CONTAINEDCLAUDE_COMPACT_OFFER: '100000',
  })
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: state.tokens, window: 1000000 }, rateLimits: [] } }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  const session = mock.session(on)             // what the mod says (`say`): notice rows in the conversation
  on('prompt.submit', (_, e) => {
    seen.prompts.push({ text: e.text, context: e.context, origin: e.origin })
    return state.drop !== undefined ? { drop: state.drop } : { text: e.text, context: e.context }
  })
  on('tool.call', (_, e) => {
    seen.bash.push(e as unknown as Record<string, unknown>)
    const stdout = options.output ?? `${UNSEEN}\nKeep: the code word\n`
    const result = { stdout: options.toStderr ? '' : stdout, stderr: options.toStderr ? stdout : '', interrupted: false }
    return { result, text: stdout, ref: 1 }
  })
  on('command.run', async (_, e) => {           // /compact: the engine raises session.compact, as the test's $ does
    seen.commands.push({ command: e.command, args: e.args })
    if (state.compaction === 'rejects') throw new Error('a turn is running')
    if (options.subagentFirst) await $.session.compact({ trigger: 'auto', messages: MESSAGES, agentId: 'agent-1' })
    if (state.compaction !== 'none') await $.session.compact({ trigger: 'manual', messages: MESSAGES })
    return { text: '' }
  })
  on('session.compact', (_, e) => {
    seen.compactions.push(e.instructions)
    return state.compaction === 'skipped' ? { skip: 'a hook said no' } : { messages: MESSAGES }
  })
  const said = () => session.appended().map(row => {
    const block = row.message.content[0]
    return `${row.message.type}/${row.door}: ${block?.type === 'text' ? block.text : ''}`
  }).join('\n')
  return { seen, state, clock, said }
}

const start = ($: Engine, turnId: string, text = 'hello', agentId?: string) =>
  $.turn.start({ turnId, text, ...(agentId ? { agentId } : {}) })
const end = ($: Engine, turnId: string, reason: 'answer' | 'aborted' | 'error' = 'answer', agentId?: string) =>
  $.turn.complete({
    turnId, reason, answer: 'ok', durationMs: 1, isAborted: reason === 'aborted', ...(agentId ? { agentId } : {}),
  })
const turn = async ($: Engine, turnId: string, text = 'hello') => {
  await start($, turnId, text)
  await end($, turnId)
}
const bash = ($: Engine, command: string, more: Record<string, unknown> = {}) =>
  $.tool.call({ tool: 'Bash', command, ...more })
// what the agent reads of a call the mod scheduled: its line in the script's place, the rest as printed
type Ran = { deny?: string; result?: unknown; context?: readonly string[] }
const out = (ran: Ran) => {
  const r = ran.result as { stdout: string; stderr: string } | undefined
  return `${r?.stdout ?? ''}${r?.stderr ?? ''}`
}
const isScheduled = (ran: Ran) =>
  /^cc-compact: compaction is scheduled for the end of this turn: finish your reply now/m.test(out(ran)) &&
  !out(ran).includes('nothing scheduled')
const isUntouched = (ran: Ran) => out(ran).includes('containedclaude did not see this call') || !out(ran).includes('cc-compact:')

// ---- idle ---------------------------------------------------------------------------------------------

test('idle: notice after 50 minutes, compaction after the answer to it, no prompt to continue', async ($, on) => {
  const { seen, clock, said } = world($, on)
  await turn($, 't1')
  await clock.advance(49 * MIN)
  expect(seen.prompts).toHaveLength(0)
  await clock.advance(MIN)
  expect(seen.prompts.map(p => p.text)).toEqual([LINE])
  expect(seen.prompts[0]?.origin).toMatchObject({ kind: 'plugin', asUser: true })
  await start($, 't2', LINE)
  expect(seen.commands).toHaveLength(0)
  await end($, 't2')
  await clock.settle()
  expect(seen.commands).toEqual([{ command: 'compact', args: '' }])
  expect(seen.compactions).toEqual([INSTRUCTIONS])
  expect(seen.prompts).toHaveLength(1)
  expect(said()).toBe('')
  await clock.advance(120 * MIN)                // nothing more: the compacted session has no timer
  expect(seen.prompts).toHaveLength(1)
  expect(seen.commands).toHaveLength(1)
})

test('idle: a notice the engine dropped compacts nothing, also not after the next prompt (2026-11-14)', async ($, on) => {
  const { seen, state, clock, said } = world($, on, { drop: 'a Stop stands' })
  await turn($, 't1')
  await clock.advance(50 * MIN)
  expect(seen.prompts).toHaveLength(1)
  expect(said()).toBe('system/note: containedclaude: idle compaction: the notice was not delivered: a Stop stands')
  state.drop = undefined
  await turn($, 't2', 'I am back, what did you find?')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
  expect(seen.compactions).toHaveLength(0)
})

test('idle: only the turn of the notice compacts, whatever other turns end meanwhile', async ($, on) => {
  const { seen, clock } = world($, on)
  await turn($, 't1')
  await clock.advance(50 * MIN)
  await start($, 't2', LINE)
  await end($, 't1')                             // another turn's end, late
  await end($, 'sub', 'answer', 'agent-1')       // a subagent's
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
  await end($, 't2')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
})

test('idle: an interrupted answer to the notice compacts nothing; the timer starts again', async ($, on) => {
  const { seen, clock, said } = world($, on)
  await turn($, 't1')
  await clock.advance(50 * MIN)
  await start($, 't2', LINE)
  await end($, 't2', 'aborted')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
  expect(said()).toContain('idle compaction: not done, the turn was interrupted')
  await clock.advance(50 * MIN)
  expect(seen.prompts.map(p => p.text)).toEqual([LINE, LINE])
})

test('idle: an answer that failed (error) compacts nothing', async ($, on) => {
  const { seen, clock } = world($, on)
  await turn($, 't1')
  await clock.advance(50 * MIN)
  await start($, 't2', LINE)
  await end($, 't2', 'error')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('idle: a turn starting before the time is up cancels the notice; each turn end starts the time anew', async ($, on) => {
  const { seen, clock } = world($, on)
  await turn($, 't1')
  await clock.advance(40 * MIN)
  await start($, 't2')
  await clock.advance(120 * MIN)                 // a long turn: no notice while it runs
  expect(seen.prompts).toHaveLength(0)
  await end($, 't2')
  await clock.advance(49 * MIN)
  expect(seen.prompts).toHaveLength(0)
  await clock.advance(MIN)
  expect(seen.prompts).toHaveLength(1)
})

test('idle: the late end of an earlier turn starts no time while a turn runs', async ($, on) => {
  const { seen, clock } = world($, on)
  await start($, 't1')
  await start($, 't2')
  await end($, 't1', 'aborted')
  await clock.advance(120 * MIN)                 // t2 is still running (a long wait)
  expect(seen.prompts).toHaveLength(0)
})

test('idle: a subagent turn neither starts nor cancels the time', async ($, on) => {
  const { seen, clock } = world($, on)
  await start($, 's1', 'task', 'agent-1')
  await end($, 's1', 'answer', 'agent-1')
  await clock.advance(60 * MIN)
  expect(seen.prompts).toHaveLength(0)
  await turn($, 't1')
  await clock.advance(30 * MIN)
  await start($, 's2', 'task', 'agent-1')
  await end($, 's2', 'answer', 'agent-1')
  await clock.advance(20 * MIN)
  expect(seen.prompts).toHaveLength(1)
})

test('idle: no notice for a small context', async ($, on) => {
  const { seen, clock } = world($, on, { tokens: 149999 })
  await turn($, 't1')
  await clock.advance(600 * MIN)
  expect(seen.prompts).toHaveLength(0)
})

test('idle: off without its values (idle_compact = no)', async ($, on) => {
  const { seen, clock } = world($, on, { env: { CONTAINEDCLAUDE_COMPACT_OFFER: '100000' } })
  await turn($, 't1')
  await clock.advance(600 * MIN)
  expect(seen.prompts).toHaveLength(0)
  await start($, 't2', LINE)                     // the line typed by hand still means what it says
  await end($, 't2')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
})

test('idle: a prompt that only contains the line is an ordinary prompt', async ($, on) => {
  const { seen, clock } = world($, on)
  await turn($, 't1', `What does this mean: ${LINE}`)
  await turn($, 't2', `${LINE}\nand more`)
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

// ---- on request ---------------------------------------------------------------------------------------

test('request: the call runs as written, gets the note, and its turn end compacts and prompts to continue', async ($, on) => {
  const { seen, clock, said } = world($, on)
  const command = 'cd ~/p; cc-compact "the code word"'
  await start($, 't1')
  const ran = await bash($, command, { timeout: 5000 })
  expect(isScheduled(ran)).toBe(true)
  expect(out(ran)).toContain('\nKeep: the code word\n')
  expect(ran.context).toBeUndefined()
  expect(seen.bash).toEqual([expect.objectContaining({ command, timeout: 5000 })])
  expect(seen.commands).toHaveLength(0)
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toEqual([{ command: 'compact', args: '' }])
  expect(seen.compactions).toEqual([`the code word ${INSTRUCTIONS}`])
  expect(seen.prompts.map(p => p.text)).toEqual([DONE])
  expect(seen.prompts[0]?.origin).toMatchObject({ kind: 'plugin', asUser: true })
  expect(said()).toBe('')
})

test('request: every place a command can start is recognised', async ($, on) => {
  const { seen } = world($, on)
  await start($, 't1')
  for (const command of ['cd ~/p; cc-compact "the code word"', 'true && cc-compact x', 'true || cc-compact x',
    'if true; then cc-compact; fi', 'echo a\ncc-compact "b"', '(cc-compact x)', '  cc-compact', '{ cc-compact; }',
    'for i in 1; do cc-compact; done', 'echo `cc-compact`', 'x | cc-compact', 'cc-compact;ls', 'cc-compact']) {
    const ran = await bash($, command)
    expect(isScheduled(ran), command).toBe(true)
  }
  expect(seen.bash).toHaveLength(13)
})

test('request: a call that only names cc-compact, or whose output lacks the script\'s line, schedules nothing', async ($, on) => {
  const { seen, clock } = world($, on)
  await start($, 't1')
  // not at a command's start: runs untouched (the script's own line then says no note means nothing scheduled)
  for (const command of ['bash -c "cc-compact x"', 'grep -rn cc-compact .', 'cat sandbox/bin/cc-compact',
    'echo cc-compact', 'which cc-compact', './cc-compact', 'cc-compact-all', 'mycc-compact', 'man cc-compact.1',
    'sh run.sh']) {
    const ran = await bash($, command)
    expect(isUntouched(ran), command).toBe(true)
  }
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: the script did not run (a failed chain, --help): nothing scheduled', async ($, on) => {
  const { seen, clock } = world($, on, { output: 'cd: no such file or directory' })
  await start($, 't1')
  const ran = await bash($, 'cd nowhere && cc-compact x')
  expect(out(ran)).toBe('cd: no such file or directory')
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: without text to keep, and with a Keep line elsewhere in the output', async ($, on) => {
  const first = world($, on, { output: `Keep: not this\n${UNSEEN}\n` })
  await start($, 't1')
  await bash($, 'echo "Keep: not this"; cc-compact')
  await end($, 't1')
  await first.clock.settle()
  expect(first.seen.compactions).toEqual([INSTRUCTIONS])
})

test('request: the script\'s line in stderr counts too', async ($, on) => {
  const { seen, clock } = world($, on, { toStderr: true })
  await start($, 't1')
  const ran = await bash($, 'cc-compact "the code word" >&2')
  expect(isScheduled(ran)).toBe(true)
  await end($, 't1')
  await clock.settle()
  expect(seen.compactions).toEqual([`the code word ${INSTRUCTIONS}`])
})

test('request: text after the Keep line is not taken', async ($, on) => {
  const { seen, clock } = world($, on, { output: `${UNSEEN}\nKeep:   a b  \nmore output\nKeep: no` })
  await start($, 't1')
  await bash($, 'cc-compact a b; echo more output')
  await end($, 't1')
  await clock.settle()
  expect(seen.compactions).toEqual([`a b ${INSTRUCTIONS}`])
})

test('request: refused without running in a subagent, in the background, when off, and for a small context', async ($, on) => {
  const { seen, state, clock } = world($, on)
  await start($, 't1')
  expect((await bash($, 'cc-compact x', { agentId: 'agent-1' })).deny).toContain('only the main session')
  expect((await bash($, 'cc-compact x', { run_in_background: true })).deny).toContain('in the foreground')
  state.tokens = 89999
  expect((await bash($, 'cc-compact x')).deny).toContain('only 90k tokens; below about 90k')
  state.tokens = 90000
  expect(isScheduled(await bash($, 'cc-compact x'))).toBe(true)
  expect(seen.bash).toHaveLength(1)
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
})

test('request: refused when compact_offer is 0', async ($, on) => {
  const { seen } = world($, on, { env: { CONTAINEDCLAUDE_COMPACT_OFFER: '0' } })
  await start($, 't1')
  expect((await bash($, 'cc-compact x')).deny).toContain('compact_offer = 0')
  expect(seen.bash).toHaveLength(0)
})

test('request: outside the sandbox (no compact_offer value) the mod leaves the call alone', async ($, on) => {
  const { seen, clock } = world($, on, { env: {} })
  await start($, 't1')
  const ran = await bash($, 'cc-compact x', { agentId: 'agent-1' })
  expect(ran.deny).toBeUndefined()
  expect(isUntouched(ran)).toBe(true)
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: an interrupted turn compacts nothing, and the agent hears it with the next prompt, once', async ($, on) => {
  const { seen, clock, said } = world($, on)
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1', 'aborted')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
  expect(said()).toContain('compaction on request: not done, the turn was interrupted')
  const slash = await $.prompt.submit({ text: '/model', command: { name: 'model', args: '' }, wait: false, origin: { kind: 'composer' } })
  expect(slash.context).toBeUndefined()
  const next = await $.prompt.submit({ text: 'go on', context: ['other'], wait: false, origin: { kind: 'composer' } })
  expect(next.context).toHaveLength(2)
  expect(next.context?.[1]).toContain('was not done (the turn was interrupted). Run cc-compact again')
  const later = await $.prompt.submit({ text: 'and now', wait: false, origin: { kind: 'composer' } })
  expect(later.context).toBeUndefined()
  await turn($, 't2', 'go on')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: the end of an earlier, interrupted turn arriving late does not use it up', async ($, on) => {
  const { seen, clock } = world($, on)
  await start($, 't1')
  await start($, 't2')                           // t1 was interrupted, its end not reported yet
  await bash($, 'cc-compact x')
  await end($, 't1', 'aborted')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
  await end($, 't2')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
  expect(seen.prompts.map(p => p.text)).toEqual([DONE])
})

test('request: a turn that never reports its end is dropped when the next begins', async ($, on) => {
  const { seen, clock, said } = world($, on)
  await start($, 't1')
  await bash($, 'cc-compact x')
  await start($, 't2')
  expect(said()).toContain('compaction on request: not done, its turn never reported its end')
  await end($, 't2')
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: asked twice in a turn, the last one counts, one compaction', async ($, on) => {
  const { seen, clock } = world($, on)
  await start($, 't1')
  await bash($, 'cc-compact one')
  await bash($, 'cc-compact two')
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
  expect(seen.prompts).toHaveLength(1)
})

test('request: a call outside any turn the mod knows (loaded anew mid-turn) says so and schedules nothing', async ($, on) => {
  const { seen, clock } = world($, on)
  const ran = await bash($, 'cc-compact x')
  expect(out(ran)).toMatch(/^cc-compact: nothing scheduled: the compaction mod was loaded anew during this turn\..*\nKeep: the code word\n$/)
  await end($, 't1')
  await clock.settle()
  expect(seen.commands).toHaveLength(0)
})

test('request: when /compact compacts nothing, is skipped or fails, the agent is not told "compacted"', async ($, on) => {
  const { seen, state, clock, said } = world($, on, { compaction: 'none' })
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1')
  await clock.settle()
  expect(seen.prompts.map(p => p.text)).toEqual(['The compaction you asked for did not run (/compact compacted nothing). Continue where you left off.'])
  state.compaction = 'skipped'
  await start($, 't2')
  await bash($, 'cc-compact x')
  await end($, 't2')
  await clock.settle()
  expect(seen.prompts[1]?.text).toContain('did not run')
  state.compaction = 'rejects'
  await start($, 't3')
  await bash($, 'cc-compact x')
  await end($, 't3')
  await clock.settle()
  expect(seen.prompts).toHaveLength(2)
  expect(said()).toContain('/compact did not run')
  // and the instructions of a compaction that never came are not given to a later one
  state.compaction = 'runs'
  await $.session.compact({ trigger: 'manual', messages: MESSAGES, instructions: 'mine' })
  expect(seen.compactions.at(-1)).toBe('mine')
})

// After an interrupt the engine refuses $.command.run ("the session was stopped and nobody has sent input since")
// until the person's next prompt (2026-11-22/24, kathanome_claude: three requests lost, the agent never told).
test('request: /compact refused: tried again at the end of the turn the person\'s next prompt starts', async ($, on) => {
  const { seen, state, clock, said } = world($, on, { compaction: 'rejects' })
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1')
  await clock.settle()
  expect(seen.compactions).toHaveLength(0)
  expect(said()).toContain('/compact did not run')
  const notice = await $.prompt.submit({ text: 'job done', wait: false, origin: { kind: 'task-notification' } })
  expect(notice.context).toBeUndefined()                 // not the person's: lifts no stop, arms nothing
  await turn($, 't2', 'job done')
  await clock.settle()
  expect(seen.commands).toHaveLength(1)
  state.compaction = 'runs'
  const next = await $.prompt.submit({ text: 'go on', wait: false, origin: { kind: 'bridge' } })
  expect(next.context?.[0]).toMatch(new RegExp('^containedclaude: the compaction you asked for with cc-compact ' +
    'could not run \\(/compact did not run: .+\\)\\. It runs when this turn ends instead\\.$'))
  await turn($, 't3', 'go on')
  await clock.settle()
  expect(seen.compactions).toEqual([`the code word ${INSTRUCTIONS}`])
  expect(seen.prompts.at(-1)?.text).toBe(DONE)
  await turn($, 't4')                                    // once only
  await clock.settle()
  expect(seen.commands).toHaveLength(2)
})

test('request: refused on the second try too: a visible prompt says so, or a note if that is dropped', async ($, on) => {
  const { seen, state, clock } = world($, on, { compaction: 'rejects' })
  const twice = async (id: string) => {
    await start($, `${id}a`)
    await bash($, 'cc-compact x')
    await end($, `${id}a`)
    await clock.settle()
    await $.prompt.submit({ text: 'go on', wait: false, origin: { kind: 'composer' } })
    await turn($, `${id}b`, 'go on')
    await clock.settle()
  }
  await twice('1')
  expect(seen.prompts.at(-1)?.text).toMatch(/^The compaction you asked for did not run \(\/compact did not run: .+\)\. Continue where you left off\.$/)
  state.drop = 'the session was stopped'
  await twice('2')
  state.drop = undefined
  const next = await $.prompt.submit({ text: 'and now', wait: false, origin: { kind: 'composer' } })
  expect(next.context?.[0]).toMatch(/was not done \(\/compact did not run: .+\)\. Run cc-compact again/)
})

test('request: a "did not run" prompt the engine dropped reaches the agent as a note with the next prompt', async ($, on) => {
  const { state, clock } = world($, on, { drop: 'the session was stopped', compaction: 'none' })
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1')
  await clock.settle()
  state.drop = undefined
  const next = await $.prompt.submit({ text: 'go on', wait: false, origin: { kind: 'composer' } })
  expect(next.context?.[0]).toContain('was not done (/compact compacted nothing)')
})

test('request: in the turn of an idle notice, what to keep is taken and no prompt to continue is sent', async ($, on) => {
  const { seen, clock } = world($, on)
  await turn($, 't1')
  await clock.advance(50 * MIN)
  await start($, 't2', LINE)
  await bash($, 'cc-compact x')
  await end($, 't2')
  await clock.settle()
  expect(seen.compactions).toEqual([`the code word ${INSTRUCTIONS}`])
  expect(seen.prompts.map(p => p.text)).toEqual([LINE])
})

test('request: a subagent compacting at the same moment does not get the instructions', async ($, on) => {
  const { seen, clock } = world($, on, { subagentFirst: true })
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1')
  await clock.settle()
  expect(seen.compactions).toEqual([undefined, `the code word ${INSTRUCTIONS}`])
  expect(seen.prompts.map(p => p.text)).toEqual([DONE])
})

// ---- other compactions --------------------------------------------------------------------------------

test('compactions the mod did not start keep their own instructions', async ($, on) => {
  const { seen, clock } = world($, on)
  await $.session.compact({ trigger: 'manual', messages: MESSAGES, instructions: 'mine' })
  await $.session.compact({ trigger: 'auto', messages: MESSAGES })
  expect(seen.compactions).toEqual(['mine', undefined])
  await start($, 't1')
  await bash($, 'cc-compact x')
  await end($, 't1')
  await clock.settle()
  await $.session.compact({ trigger: 'auto', messages: MESSAGES, agentId: 'agent-1' })
  await $.session.compact({ trigger: 'manual', messages: MESSAGES, instructions: 'later' })
  expect(seen.compactions.slice(2)).toEqual([`the code word ${INSTRUCTIONS}`, undefined, 'later'])
})
