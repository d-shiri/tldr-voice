<p align="center">
  <img src=".claude-plugin/icon.png" alt="TL;DR Voice" width="128">
</p>

<h1 align="center">TL;DR Voice</h1>

<p align="center">
  A Claude Code plugin: every answer opens with a one-line <code>TL;DR:</code>,<br>
  and a voice reads it aloud while the rest is still printing. English or German.
</p>

<p align="center">
  <img alt="Claude Code plugin" src="https://img.shields.io/badge/Claude_Code-plugin-D97757">
  <img alt="Version" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fd-shiri%2Ftldr-voice%2Fmain%2F.claude-plugin%2Fplugin.json&query=%24.version&label=version&color=5BC8DC">
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-informational">
  <img alt="English and German" src="https://img.shields.io/badge/voice-EN_%7C_DE-blueviolet">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <img src="docs/demo.webp" alt="TL;DR Voice demo">
</p>

<p align="center">
  🔊 <a href="docs/ava-sample.mp3"><b>Hear a sample</b></a> (Ava voice):<br>
  <i>"I fixed the failing login test, and all checks pass now. Should I open a pull request?"</i>
</p>

---

## Install

In Claude Code:

```bash
/plugin install tldr-voice --marketplace d-shiri/tldr-voice
```

Pick **English** or **German** when asked, then restart Claude Code. On first start the plugin sets up its voices in the background, which takes about a minute. After that, every answer is read aloud.

> [!IMPORTANT]
> Needs **Linux** with **Python 3** and [`mpv`](https://mpv.io). [`uv`](https://docs.astral.sh/uv/) makes setup faster. macOS is untested.
>
> ```bash
> sudo apt install mpv
> ```

**Claude Code older than 2.1.275?** Install in two steps:

```bash
# 1. Add the marketplace
/plugin marketplace add d-shiri/tldr-voice

# 2. Install the plugin
/plugin install tldr-voice@tldr-voice
```

**Let Claude do it:** paste this prompt into Claude Code:

```markdown
Install the tldr-voice plugin for me: run `claude plugin marketplace add d-shiri/tldr-voice`
and `claude plugin install tldr-voice@tldr-voice`, make sure python3 and mpv are installed
(install mpv if missing), then tell me to restart Claude Code.
```

## Use

Click the 🔊 icon above the prompt to mute or unmute, or use the command:

| Command | What it does |
| --- | --- |
| `/tldr-voice:voice off` · `on` | Mute or unmute |
| `/tldr-voice:voice language de` · `en` | Speak German or English |
| `/tldr-voice:voice engine piper` | Offline voice only (`edge` = online, the default) |
| `/tldr-voice:voice stop` | Stop talking now |
| `/tldr-voice:voice test` | Hear the voice |
| `/tldr-voice:voice status` | Show the current settings |

> [!TIP]
> Typing your next message stops the voice, and so does holding space to talk. The hold-space stop needs `pactl` (PulseAudio or PipeWire).

### Settings

Set these in the `env` block of `~/.claude/settings.json`:

```json
{
  "env": {
    "TLDR_VOICE_MIC_STOP": "0",
    "TLDR_VOICE_EDGE_VOICE": "en-US-AvaMultilingualNeural"
  }
}
```

| Variable | Default | What it does |
| --- | --- | --- |
| `TLDR_VOICE_MIC_STOP` | `1` | `0` turns off the hold-space stop |
| `TLDR_VOICE_EDGE_VOICE` | `en-US-AvaMultilingualNeural` | Picks another online voice (used for German too) |

> [!NOTE]
> **Privacy:** the online voice sends only the TL;DR line to Microsoft's speech service. `/tldr-voice:voice engine piper` keeps everything offline.

## Uninstall

```bash
/plugin uninstall tldr-voice@tldr-voice
```

Installed tldr-voice with the old `install.sh`? Run this from a clone of this repo first:

```bash
./remove-script-install.sh --purge
```

## License

[MIT](LICENSE). Uses [edge-tts](https://github.com/rany2/edge-tts), [Piper](https://github.com/OHF-Voice/piper1-gpl) and [mpv](https://mpv.io), downloaded on first use. Not affiliated with Anthropic or Microsoft.
