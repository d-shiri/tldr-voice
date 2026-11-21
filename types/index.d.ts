/** On (true), muted (false), or not read yet (null). */
export type VoiceOn = boolean | null

declare module 'claude-code' {
  interface PluginState {
    'tldr-voice': { isOn: VoiceOn }
  }
}
