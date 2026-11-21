# TL;DR Voice

**Your terminal, read aloud.** tldr-voice makes [Claude Code](https://code.claude.com) open every answer with a one-line `TL;DR:` summary, and reads that line out loud while the rest of the answer is still printing.

![TL;DR Voice demo](docs/demo.webp)

The full-quality animation is in [`docs/demo.html`](docs/demo.html). Download it and open it in a browser.

## Why

Long answers are tiring to read. Most of the time you only need the gist: what happened, or what Claude needs from you. tldr-voice tells you that in one or two spoken sentences, so you can keep your eyes on your own work.

## What you get

- **One spoken sentence per answer.** Claude writes a plain `TL;DR:` line first, and only that line is spoken.
- **Fast.** Speech starts as soon as the summary line streams in, typically about a second later.
- **A natural voice, with an offline backup.** The default is Microsoft's free online neural voice *Ava* (via [edge-tts](https://github.com/rany2/edge-tts)). If it is slow (over 5 seconds) or unreachable, the local [Piper](https://github.com/OHF-Voice/piper1-gpl) voice speaks instead. You can also use Piper only.
- **Never in the way.**
  - Typing your next message stops the speech.
  - A new answer replaces the one being spoken.
  - Headless and scheduled `claude -p` runs stay silent.
- **Easy to undo.** It's a few hooks and one marked block in your `CLAUDE.md`. `./uninstall.sh` removes all of it.

## Requirements

- Linux with PulseAudio/PipeWire (`paplay`) or ALSA (`aplay`)
- Claude Code with hooks support (the streaming `MessageDisplay` hook gives the fastest speech; without it, the `Stop` hook still speaks when the answer finishes)
- Python 3, `curl`, and [`mpv`](https://mpv.io) for the online voice (`sudo apt install mpv`)
- [`uv`](https://docs.astral.sh/uv/) is recommended. Without it, the installer falls back to `python3 -m venv`.

macOS isn't supported yet. Pull requests are welcome.

## Install

```sh
git clone https://github.com/d-shiri/tldr-voice.git
cd tldr-voice
./install.sh
```

Restart Claude Code, ask it something, and listen.

The installer:

1. Copies two scripts to `~/.claude/hooks/tldr-voice/`.
2. Creates a private Python environment with `edge-tts` and `piper-tts` in `~/.local/share/tldr-voice/`.
3. Downloads the offline Piper voice (about 60 MB).
4. Adds three hooks to `~/.claude/settings.json`, keeping a timestamped backup next to it.
5. Adds a marked TL;DR instruction block to `~/.claude/CLAUDE.md`.
6. Links a `tldr-voice` command into `~/.local/bin`.

Running it again updates an existing install.

## Use

```sh
tldr-voice off             # stop reading answers aloud
tldr-voice on              # start again
tldr-voice engine piper    # offline voice only
tldr-voice engine edge     # natural online voice (default)
tldr-voice stop-audio      # be quiet right now
tldr-voice status
tldr-voice say "Hello"     # test the voice
```

### Settings

Set these environment variables, for example in the `env` block of `~/.claude/settings.json`:

| Variable | Default | What it does |
|---|---|---|
| `TLDR_VOICE_EDGE_VOICE` | `en-US-AvaMultilingualNeural` | Online voice. Run `~/.local/share/tldr-voice/venv/bin/edge-tts --list-voices` to see the options. |
| `TLDR_VOICE_PIPER_MODEL` | the bundled `en_US-lessac-medium` | Path to another [Piper voice](https://huggingface.co/rhasspy/piper-voices) `.onnx` file, such as a German one. |

## How it works

```
you press Enter ─► UserPromptSubmit hook: stop any speech, warm up the voice,
                   remind Claude to open with a TL;DR line
Claude streams  ─► MessageDisplay hook: the first lines contain "TL;DR: …"
                   → handed to a small background helper
helper          ─► sends the line to the voice service twice, plays whichever
                   answers first, streaming straight into mpv
                   (no audio within 5 s → local Piper voice instead)
answer done     ─► Stop hook: speaks the TL;DR only if it wasn't spoken already
```

The helper exits after 30 idle minutes and restarts on demand. Hook scripts use only the Python standard library, so they add a few milliseconds per prompt.

## Privacy and fair use

- With the default online voice, **each TL;DR line is sent to Microsoft's speech service** to be turned into audio. Nothing else from your conversation is sent. Use `tldr-voice engine piper` to stay fully offline.
- edge-tts uses the speech service behind Microsoft Edge's "Read aloud" feature. It isn't an official API, so it could change or stop working. tldr-voice keeps requests small (about three per prompt), and the Piper fallback keeps you covered if it breaks.

## Uninstall

```sh
./uninstall.sh            # remove hooks, CLAUDE.md block, scripts and command
./uninstall.sh --purge    # also delete the downloaded voice and Python environment
```

## Credits

- [edge-tts](https://github.com/rany2/edge-tts) (LGPL-3.0)
- [Piper](https://github.com/OHF-Voice/piper1-gpl) (GPL-3.0) and the [en_US-lessac voice](https://huggingface.co/rhasspy/piper-voices)
- [mpv](https://mpv.io)
- The demo animation uses [three.js](https://threejs.org)

These are installed or loaded separately and not bundled here.

tldr-voice is an independent project. It isn't affiliated with or endorsed by Anthropic or Microsoft.

## License

[MIT](LICENSE)
