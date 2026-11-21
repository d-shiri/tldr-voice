import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

// A speaker icon in the band above the prompt: click it to mute or unmute tldr-voice.
// The on/off switch is the same flag file the voice script reads, so the icon,
// `/tldr-voice:voice on|off` and the hooks always agree.

const isOn = atom({ plugin: 'tldr-voice', key: 'isOn' } as const, null)

async function flagPath($: EngineInterface): Promise<string> {
  const state = (await $.env.get('XDG_STATE_HOME')) || `${await $.env.get('HOME')}/.local/state`
  return `${state}/tldr-voice/disabled`
}

// Re-read the flag: the slash command may have changed it since the last look.
async function refresh($: EngineInterface): Promise<void> {
  const off = await $.fs.exists(await flagPath($))
  await update($, isOn, () => !off)
}

export const register: Register = on => {
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
      const { exitCode } = await $.process.run([
        'python3',
        `${$.plugin.root}/scripts/speak.py`,
        turnOn ? 'on' : 'off',
      ])

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
