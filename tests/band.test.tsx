import { expect, test } from 'claude-code/testing'

// The row above the prompt: the speaker icon, and after a few days of use the
// "Star on GitHub" ask at its right. Hooks here stand for the engine beneath the plugin.

const BAND = {
  plugin: 'tldr-voice',
  component: 'AbovePrompt',
  requestId: 'tldr-voice',
  surface: 'terminal',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const DAY = 24 * 60 * 60 * 1000

// `ghWorks`: whether the GitHub CLI is there and signed in.
function engine(on: any, stored: Record<string, unknown>, runs: string[][] = [], ghWorks = false) {
  on('session.info', () => ({
    value: { platform: 'linux', isHosted: false, transcriptPath: '/home/t/.claude/projects/p/abc-123.jsonl' },
  }))
  on('env.get', (_: unknown, e: any) => ({ value: e.name === 'HOME' ? '/home/t' : undefined }))
  on('fs.exists', () => ({ value: false }))
  on('clock.now', () => ({ value: Date.UTC(2026, 10, 24, 12) }))
  on('store.get', (_: unknown, e: any) => ({ value: stored[e.key] }))
  on('store.set', (_: unknown, e: any) => {
    stored[e.key] = e.value
    return { value: undefined }
  })
  on('process.run', (_: unknown, e: any) => {
    runs.push([...e.argv])
    const exitCode = e.argv[0] === 'gh' && !ghWorks ? 1 : stored.exitCode ?? 0
    return { value: { exitCode, stdout: '', stderr: '' } }
  })
  on('session.start', (_: unknown, e: any) => ({ cwd: e.cwd }))
}

test('no ask before the third day of use', async ($, on) => {
  const stored: Record<string, unknown> = { daysUsed: ['2026-11-23'] }
  engine(on, stored)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-toggle' })).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-star' })).toBeFalsy()
  expect(stored.daysUsed).toEqual(['2026-11-23', '2026-11-24'])
  await ui.unmount()
})

test('on the third day it asks; without the GitHub CLI the star opens the page', async ($, on) => {
  const stored: Record<string, unknown> = { daysUsed: ['2026-11-22', '2026-11-23'] }
  const runs: string[][] = []
  engine(on, stored, runs)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-star' })).toBeTruthy()
  await ui.press({ key: 'tldr-voice-star' })
  expect(runs).toContainEqual(['xdg-open', 'https://github.com/d-shiri/tldr-voice'])
  expect(stored.starAsked).toBe(true)
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-star' })).toBeFalsy()
  await ui.unmount()
})

test('with the GitHub CLI signed in, the star is given right away', async ($, on) => {
  const stored: Record<string, unknown> = { daysUsed: ['2026-11-21', '2026-11-22', '2026-11-23'] }
  const runs: string[][] = []
  engine(on, stored, runs, true)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  await ui.press({ key: 'tldr-voice-star' })
  expect(runs).toContainEqual(
    ['gh', 'api', '--silent', '--method', 'PUT', '/user/starred/d-shiri/tldr-voice'])
  expect(runs.some(argv => argv[0] === 'xdg-open')).toBe(false)
  expect(stored.starAsked).toBe(true)
  await ui.unmount()
})

test('the ✕ puts the ask away for good', async ($, on) => {
  const stored: Record<string, unknown> = { daysUsed: ['2026-11-21', '2026-11-22', '2026-11-23'] }
  engine(on, stored)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  await ui.press({ key: 'tldr-voice-no-star' })
  expect(stored.starAsked).toBe(true)
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-star' })).toBeFalsy()
  await ui.unmount()
})

test('once asked, it never counts or asks again', async ($, on) => {
  const stored: Record<string, unknown> = { starAsked: true, daysUsed: ['a', 'b', 'c'] }
  engine(on, stored)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Button', key: 'tldr-voice-star' })).toBeFalsy()
  await ui.unmount()
})

test("🗘 replays this session's last line", async ($, on) => {
  const stored: Record<string, unknown> = { daysUsed: [] }
  const runs: string[][] = []
  engine(on, stored, runs)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND)
  await ui.press({ key: 'tldr-voice-replay' })
  const call = runs.find(argv => argv.includes('again'))
  expect(call?.slice(-2)).toEqual(['again', 'abc-123'])
  await ui.unmount()
})
