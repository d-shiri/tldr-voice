/** On (true), muted (false), or not read yet (null). */
export type VoiceOn = boolean | null

declare module 'claude-code' {
  interface PluginState {
    'tldr-voice': {
      isOn: VoiceOn
      /** Show the "Star on GitHub" ask beside the speaker icon (see register.tsx). */
      askStar: boolean
      /** Where the star ask's shine is (a frame number), or null while it rests. */
      starShine: number | null
    }
  }
}
