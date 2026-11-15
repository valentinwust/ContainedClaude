import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

// Each hook here stands for the engine beneath the mod: what the Bash tool would answer.
const LONG = 3420000
const WORKER_MAX = 240000

type Call = { command: string; timeout?: number; run_in_background?: boolean; agentId?: string }

const setup = (on: On, seen: Call[], result: Record<string, unknown> = {}) => {
  on('agent.list', () => ({
    value: [
      { id: 'w1', type: 'cc:worker', description: '', status: 'running' },
      { id: 'j1', type: 'cc:job-worker', description: '', status: 'running' },
      { id: 'g1', type: 'general-purpose', description: '', status: 'running' },
    ],
  }))
  on('tool.call', (_, e) => {
    seen.push(e as unknown as Call)
    return { result, text: 'out' }
  })
}

test('main session: a foreground wait needs the long timeout, and runs unchanged with it', async ($, on) => {
  const seen: Call[] = []
  setup(on, seen)
  for (const command of ['cc-wait 123', 'cd x && cc-wait --max 20 1 2', 'cc-run --cpus 2 --wait -- python a.py',
    'cc-log 5 -n 3 -f', 'id=$(cc-run -- x); cc-wait $id', 'cc-wait 1; python server.py', 'X=1 Y=b cc-wait 1',
    'if true; then cc-wait 1; fi', 'ls |\ncc-wait 1', 'echo $(cc-wait 1)', 'cat > a.sh <<EOF\ncc-wait $id\nEOF']) {
    for (const timeout of [undefined, 120000, 600000, LONG - 1]) {
      const refused = await $.tool.call({ tool: 'Bash', command, ...(timeout === undefined ? {} : { timeout }) })
      expect(refused.deny, `${command} @ ${timeout}`).toContain(`timeout: ${LONG}`)
      expect(refused.deny).toContain('run_in_background')
    }
    expect(seen).toHaveLength(0)
    const ran = await $.tool.call({ tool: 'Bash', command, timeout: LONG })
    expect(ran.text).toBe('out')
    expect(ran.context).toBeUndefined()
    expect(seen.pop()).toMatchObject({ command, timeout: LONG })
  }
})

test('main session: background waits and other commands run as written, without a note', async ($, on) => {
  const seen: Call[] = []
  setup(on, seen)
  const calls: Call[] = [
    { command: 'cc-wait 123', run_in_background: true },
    { command: 'ls' }, { command: 'sleep 500', timeout: 600000 },
    { command: 'cc-run -- python a.py' }, { command: 'cc-log 5 -n 50' }, { command: 'cc-jobs' },
    { command: 'grep -r cc-wait-x .' }, { command: 'echo mycc-wait' }, { command: 'cat doc/cc-wait.md' },
    { command: 'cc-run --name a--wait -- x' }, { command: 'cc-logs -f' },
    // a call that only names a wait
    { command: 'grep -rn "cc-wait " docs/' }, { command: 'echo "then run cc-wait JOBID"' },
    { command: 'git commit -m "use cc-run --wait in the docs"' }, { command: 'man cc-wait' },
    { command: 'cc-compact "jobs 1-3 running; next: cc-wait them, then cc-log 3 -f"' }, { command: 'which cc-wait cc-log' },
    { command: 'cc-run --cpus 2 -- python a.py\ncc-log 5 -n 3' }, { command: 'cc-log 5; tail -f x.log' },
  ]
  for (const call of calls) {
    const ran = await $.tool.call({ tool: 'Bash', ...call })
    expect(ran.text, call.command).toBe('out')
    expect(ran.context).toBeUndefined()
    expect(seen.pop()).toMatchObject(call)
  }
})

test('subagents other than cc:worker: the same rule, and a note for a wait in the background', async ($, on) => {
  const seen: Call[] = []
  setup(on, seen)
  for (const agentId of ['j1', 'g1', 'unlisted']) {
    const refused = await $.tool.call({ tool: 'Bash', command: 'cc-wait 1', agentId })
    expect(refused.deny).toContain(`timeout: ${LONG}`)
    expect(refused.deny).not.toContain('run_in_background')
    expect(seen).toHaveLength(0)
    const ran = await $.tool.call({ tool: 'Bash', command: 'cc-wait 1', timeout: LONG, agentId })
    expect(ran.context).toBeUndefined()
    const bg = await $.tool.call({ tool: 'Bash', command: 'cc-wait 1', run_in_background: true, agentId })
    expect(bg.text).toBe('out')
    expect(bg.context).toHaveLength(1)
    expect(bg.context?.[0]).toContain('Wait in the foreground instead')
    const other = await $.tool.call({ tool: 'Bash', command: 'sleep 5', run_in_background: true, agentId })
    expect(other.context).toBeUndefined()
    seen.length = 0
  }
})

test('cc:worker: timeouts of at most 4 minutes, waits with exactly that', async ($, on) => {
  const seen: Call[] = []
  setup(on, seen)
  const refusals: Call[] = [
    { command: 'sleep 300', timeout: 600000 }, { command: 'ls', timeout: WORKER_MAX + 1 },
    { command: 'cc-wait 1' }, { command: 'cc-wait 1', timeout: 120000 }, { command: 'cc-wait 1', timeout: LONG },
    { command: 'cc-wait 1', timeout: LONG, run_in_background: true },
  ]
  for (const call of refusals) {
    const refused = await $.tool.call({ tool: 'Bash', ...call, agentId: 'w1' })
    expect(refused.deny, JSON.stringify(call)).toContain(`timeout: ${WORKER_MAX}`)
  }
  expect(seen).toHaveLength(0)
  for (const call of [{ command: 'ls' }, { command: 'make', timeout: WORKER_MAX }, { command: 'cc-wait 1', timeout: WORKER_MAX },
    { command: 'sleep 9', run_in_background: true }] as Call[]) {
    const ran = await $.tool.call({ tool: 'Bash', ...call, agentId: 'w1' })
    expect(ran.text, JSON.stringify(call)).toBe('out')
    expect(ran.context).toBeUndefined()
    expect(seen.pop()).toMatchObject(call)
  }
  const bg = await $.tool.call({ tool: 'Bash', command: 'cc-wait 1', run_in_background: true, agentId: 'w1' })
  expect(bg.context?.[0]).toContain('you are cc:worker')
})

test('cc:worker: a command moved to the background on timeout gets the note; nobody else does', async ($, on) => {
  const seen: Call[] = []
  setup(on, seen, { timedOutAfterMs: 240000 })
  const worker = await $.tool.call({ tool: 'Bash', command: 'make all', timeout: WORKER_MAX, agentId: 'w1' })
  expect(worker.context).toHaveLength(1)
  expect(worker.context?.[0]).toContain('cc:job-worker')
  for (const agentId of ['j1', 'g1', undefined]) {
    const ran = await $.tool.call({ tool: 'Bash', command: 'make all', ...(agentId ? { agentId } : {}) })
    expect(ran.context).toBeUndefined()
  }
})

test('a refusal from beneath passes through untouched', async ($, on) => {
  on('agent.list', () => ({ value: [{ id: 'w1', type: 'cc:worker', description: '', status: 'running' }] }))
  on('tool.call', () => ({ deny: 'no' }))
  const ran = await $.tool.call({ tool: 'Bash', command: 'cc-wait 1', run_in_background: true, agentId: 'w1' })
  expect(ran.deny ?? ran.text).toContain('no')
  expect(ran.context).toBeUndefined()
})
