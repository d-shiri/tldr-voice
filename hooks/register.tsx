import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import { drawTldrLine } from './tldr-line'

// A speaker icon in the band above the prompt: click it to mute or unmute tldr-voice.
// The on/off switch is the same flag file the voice script reads, so the icon,
// `/tldr-voice:voice on|off` and the hooks always agree.

const isOn = atom({ plugin: 'tldr-voice', key: 'isOn' } as const, null)

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
async function runSpeak($: EngineInterface, mode: string): Promise<number> {
  const script = `${$.plugin.root}/scripts/speak.py`
  const pythons = (await isWindows($)) ? [['py', '-3'], ['python']] : [['python3']]
  for (const py of pythons) {
    try {
      const { exitCode } = await $.process.run([...py, script, mode])
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
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, isOn)

    if (e.props.hasSurvey || current === null) {
      return next(e)
    }

    const { Box, Button, Text } = $.ui.resolve(e)

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

    return (
      <Box>
        <Button
          key="tldr-voice-toggle"
          plain
          label={current ? '🔊' : '🔇'}
          onPress={toggle}
        />
        <Text dimColor> TL;DR voice {current ? 'on' : 'muted'}</Text>
      </Box>
    )
  })
}
