import type { Register } from 'claude-code'

// A reply opens with "TL;DR: ..." (the voice reads it). Draw that line with a megaphone
// on its left and the whole line in Claude's color, so it stands out; the stored reply is unchanged.
// Only the reply's first line with text counts, as for the voice.
// The label may be bold (**TL;DR:**); what follows it is kept as written, quotes included.
const TLDR = /^\W*TL;?DR[*_\s]*[:–—-]?[*_]*\s*(.*)$/i

export const drawTldrLine: Register = on => {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (e.props.isSummary) return next(e)
    const lines = e.props.text.split('\n')
    const first = lines.findIndex(l => l.trim() !== '')
    const m = first < 0 ? null : TLDR.exec((lines[first] ?? '').trim())
    if (!m) return next(e)
    const said = (m[1] ?? '').replace(/\*\*|__|`/g, '').trim()
    const rest = lines.slice(first + 1).join('\n').replace(/^\s*\n/, '')
    const { Box, Text } = $.ui.resolve(e)
    return (
      // The engine's own row has a blank line above it and its text indented under the
      // reply's dot; the megaphone takes the dot's place, so the rest keeps that indent.
      <Box flexDirection="column" marginTop={1}>
        <Box flexDirection="row">
          <Box width={3} flexShrink={0}><Text>📣</Text></Box>
          <Box flexGrow={1} flexShrink={1}>
            <Text color="claude">
              <Text bold>TL;DR:</Text> {said}
            </Text>
          </Box>
        </Box>
        {rest.trim() ? (
          <Box paddingLeft={2}>
            {await next({ ...e, props: { ...e.props, text: rest, isFirstOfReply: false } })}
          </Box>
        ) : null}
      </Box>
    )
  })
}
