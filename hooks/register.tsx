import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import { drawTldrLine } from './tldr-line'

// A speaker icon in the band above the prompt: click it to mute or unmute tldr-voice.
// The on/off switch is the same flag file the voice script reads, so the icon,
// `/tldr-voice:voice on|off` and the hooks always agree.

const isOn = atom({ plugin: 'tldr-voice', key: 'isOn' } as const, null)

// After a few days of use, a small "Star on GitHub" ask at the right of the same row.
// Pressing it opens the repository in the browser; either button puts it away for good.
// Days of use are the dates with a session start, kept in the plugin's store.
const askStar = atom({ plugin: 'tldr-voice', key: 'askStar' } as const, false)
const REPO_PATH = 'd-shiri/tldr-voice'
const REPO = `https://github.com/${REPO_PATH}`
const DAYS_BEFORE_ASKING = 3

// So the eye finds the ask, a shine runs across it and the star twinkles for a few
// seconds: when a session starts with it showing, and after the session's first reply.
const starShine = atom({ plugin: 'tldr-voice', key: 'starShine' } as const, null)
const ASK = 'Enjoying it? '
const SHINE_WIDTH = 3                       // letters lit at once
const SHINE_PASSES = 2
const SHINE_FRAMES = SHINE_PASSES * (ASK.length + SHINE_WIDTH)
const FRAME_MS = 80
const TWINKLE = ['✨', '⭐', '🌟', '⭐']    // all two cells wide, so nothing shifts
let shining: { cancel: () => void } | null = null
let shoneAfterReply = false

function shine($: EngineInterface): void {
  shining?.cancel()
  let frame = 0
  const timer = $.clock.every(FRAME_MS, () => {
    frame += 1
    const done = frame >= SHINE_FRAMES
    if (done) timer.cancel()
    void update($, starShine, () => (done ? null : frame))
  })
  shining = timer
  void update($, starShine, () => 0)
}

async function countDayOfUse($: EngineInterface): Promise<void> {
  if ((await $.store.get('starAsked')) || (await $.session.info()).isHosted) return
  const today = new Date(await $.clock.now()).toISOString().slice(0, 10)
  const stored = await $.store.get('daysUsed')
  const days = Array.isArray(stored) ? stored.filter(d => typeof d === 'string') : []
  if (!days.includes(today) && days.length < DAYS_BEFORE_ASKING) {
    days.push(today)
    await $.store.set('daysUsed', days)
  }
  const asking = days.length >= DAYS_BEFORE_ASKING
  await update($, askStar, () => asking)
  if (asking) shine($)
}

async function stopAsking($: EngineInterface): Promise<void> {
  shining?.cancel()
  await $.store.set('starAsked', true)
  await update($, askStar, () => false)
  await update($, starShine, () => null)
}

// Star the repository as the person, through the GitHub CLI when it is installed and
// signed in: one press and it is done. Pressing the button is the person's say-so.
async function starWithGitHubCli($: EngineInterface): Promise<boolean> {
  try {
    const { exitCode } = await $.process.run(
      ['gh', 'api', '--silent', '--method', 'PUT', `/user/starred/${REPO_PATH}`])
    return exitCode === 0
  } catch {
    return false  // no gh here
  }
}

// The person's browser, through the system's opener; no shell is involved.
async function openInBrowser($: EngineInterface, url: string): Promise<boolean> {
  const { platform } = await $.session.info()
  const openers =
    platform === 'macos' ? [['open', url]]
    : platform === 'windows' ? [['rundll32', 'url.dll,FileProtocolHandler', url]]
    : platform === 'wsl' ? [['wslview', url], ['cmd.exe', '/c', 'start', '', url]]
    : [['xdg-open', url]]
  for (const argv of openers) {
    try {
      if ((await $.process.run(argv)).exitCode === 0) return true
    } catch {
      // that opener isn't installed: try the next one
    }
  }
  return false
}

async function isWindows($: EngineInterface): Promise<boolean> {
  return (await $.session.info()).platform === 'windows'
}

// The same folder speak.py uses: Python's home on Windows is USERPROFILE, not HOME.
async function flagPath($: EngineInterface): Promise<string> {
  const home = (await isWindows($))
    ? (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
    : await $.env.get('HOME')
  const state = (await $.env.get('XDG_STATE_HOME')) || `${home}/.local/state`
  return `${state}/tldr-voice/disabled`
}

// Run speak.py with this system's Python 3. No shell here, so Windows tries the py
// launcher, then python (`python3` there is usually the Microsoft Store placeholder).
async function runSpeak($: EngineInterface, mode: string, args: string[] = []): Promise<number> {
  const script = `${$.plugin.root}/scripts/speak.py`
  const pythons = (await isWindows($)) ? [['py', '-3'], ['python']] : [['python3']]
  for (const py of pythons) {
    try {
      const { exitCode } = await $.process.run([...py, script, mode, ...args])
      // 103: the py launcher knows no Python 3 (e.g. one from uv), as in run.sh
      if (!(py[0] === 'py' && exitCode === 103)) return exitCode
    } catch {
      // that Python isn't installed: try the next one
    }
  }
  return 1
}

// Re-read the flag: the slash command may have changed it since the last look.
async function refresh($: EngineInterface): Promise<void> {
  const off = await $.fs.exists(await flagPath($))
  await update($, isOn, () => !off)
}

export const register: Register = (on, options) => {
  drawTldrLine(on, options)

  on('session.start', async ($, e, next) => {
    await refresh($)
    await countDayOfUse($)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)
    if (!shoneAfterReply && (await read($, askStar))) {
      shoneAfterReply = true
      shine($)
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, isOn)

    if (e.props.hasSurvey || current === null) {
      return next(e)
    }

    const { Box, Button, Text } = $.ui.resolve(e)
    const asking = await read($, askStar)
    const frame = asking ? await read($, starShine) : null

    const toggle = async () => {
      const turnOn = !(await read($, isOn))
      const exitCode = await runSpeak($, turnOn ? 'on' : 'off')

      if (exitCode === 0) {
        await update($, isOn, () => turnOn)
        $.ui.toast(turnOn ? 'TL;DR voice on' : 'TL;DR voice muted')
      } else {
        $.ui.toast('tldr-voice: could not switch, see /tldr-voice:voice status')
      }
    }

    // Say this session's last TL;DR again. Its id is the transcript's file name, as the
    // voice hooks get it in session_id.
    const replay = async () => {
      const { transcriptPath } = await $.session.info()
      const session = (transcriptPath.split(/[\\/]/).pop() ?? '').replace(/\.jsonl$/, '')
      const exitCode = await runSpeak($, 'again', session ? [session] : [])
      if (exitCode === 3) $.ui.toast('No TL;DR to replay yet')
      else if (exitCode !== 0) $.ui.toast('tldr-voice: could not replay, see /tldr-voice:voice status')
    }

    const star = async () => {
      await stopAsking($)
      if (await starWithGitHubCli($)) {
        $.ui.toast('Starred on GitHub. Thank you! ⭐')
      } else if (await openInBrowser($, REPO)) {
        $.ui.toast('Thank you! Press ☆ Star at the top right of the page.')
      } else {
        $.ui.toast(`Thank you! Star it at ${REPO}`)
      }
    }

    return (
      <Box flexDirection="row" width={e.props.bodyColumns}>
        <Box flexDirection="row" flexShrink={0}>
          <Button
            key="tldr-voice-toggle"
            plain
            label={current ? '🔊' : '🔇'}
            onPress={toggle}
          />
          <Text> </Text>
          <Button key="tldr-voice-replay" plain label="🗘" onPress={replay} />
          <Text dimColor> TL;DR voice {current ? 'on' : 'muted'}</Text>
        </Box>
        {asking ? (
          // The ask sits at the right end of the row, all on one line.
          <Box flexDirection="row" flexGrow={1} justifyContent="flex-end">
            {frame === null ? (
              <Text dimColor>{ASK}</Text>
            ) : (
              <Text>
                {[...ASK].map((letter, i) => {
                  // The lit stretch sweeps left to right, once per pass.
                  const head = frame % (ASK.length + SHINE_WIDTH)
                  const lit = i <= head && i > head - SHINE_WIDTH
                  return lit
                    ? <Text bold color="claude">{letter}</Text>
                    : <Text dimColor>{letter}</Text>
                })}
              </Text>
            )}
            <Button
              key="tldr-voice-star"
              plain
              label={`${frame === null ? '⭐' : TWINKLE[Math.floor(frame / 2) % TWINKLE.length]} Star on GitHub`}
              onPress={star}
            />
            <Text> </Text>
            <Button key="tldr-voice-no-star" plain label="✕" onPress={() => stopAsking($)} />
          </Box>
        ) : null}
      </Box>
    )
  })
}
