# TL;DR Voice

A Claude Code plugin: every answer opens with a one-line `TL;DR:`, and a voice reads it aloud while the rest is still printing. English or German.

![TL;DR Voice demo](docs/demo.webp)

🔊 **[Hear a sample](docs/ava-sample.mp3)** (Ava voice): *"I fixed the failing login test, and all checks pass now. Should I open a pull request?"*

## Install

In Claude Code:

```text
/plugin install tldr-voice --marketplace d-shiri/tldr-voice
```

On Claude Code older than 2.1.275, use two steps instead: `/plugin marketplace add d-shiri/tldr-voice`, then `/plugin install tldr-voice@tldr-voice`.

Pick English or German when asked, then restart Claude Code. On first start the plugin sets up its voices in the background, which takes about a minute. After that, every answer is read aloud.

Needs Linux with Python 3 and [`mpv`](https://mpv.io) (`sudo apt install mpv`). [`uv`](https://docs.astral.sh/uv/) makes setup faster. macOS is untested.

Or paste this into Claude Code and let it do the work:

```text
Install the tldr-voice plugin for me: run `claude plugin marketplace add d-shiri/tldr-voice`
and `claude plugin install tldr-voice@tldr-voice`, make sure python3 and mpv are installed
(install mpv if missing), then tell me to restart Claude Code.
```

## Use

Click the 🔊 icon above the prompt to mute or unmute. Or use the command:

```text
/tldr-voice:voice off | on            mute or unmute
/tldr-voice:voice language de | en    German or English
/tldr-voice:voice engine piper        offline voice only (edge = online, the default)
/tldr-voice:voice stop                stop talking now
/tldr-voice:voice test                hear the voice
/tldr-voice:voice status
```

Typing your next message stops the voice, and so does holding space to talk. The hold-space stop needs `pactl` (PulseAudio or PipeWire). Set `TLDR_VOICE_MIC_STOP=0` in the `env` block of `~/.claude/settings.json` to turn it off. `TLDR_VOICE_EDGE_VOICE` picks another online voice (default `en-US-AvaMultilingualNeural`, used for German too).

**Privacy:** the online voice sends only the TL;DR line to Microsoft's speech service. `/tldr-voice:voice engine piper` keeps everything offline.

## Uninstall

```text
/plugin uninstall tldr-voice@tldr-voice
```

Installed tldr-voice with the old `install.sh`? Run `./remove-script-install.sh --purge` from a clone of this repo first.

## License

[MIT](LICENSE). Uses [edge-tts](https://github.com/rany2/edge-tts), [Piper](https://github.com/OHF-Voice/piper1-gpl) and [mpv](https://mpv.io), downloaded on first use. Not affiliated with Anthropic or Microsoft.
