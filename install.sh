#!/usr/bin/env bash
# tldr-voice installer: makes Claude Code read the TL;DR line of each reply aloud.
# Safe to run again (it replaces its own previous install). Linux only for now.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODE="$CLAUDE_DIR/hooks/tldr-voice"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/tldr-voice"
BIN="$HOME/.local/bin"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
VOICE="en_US-lessac-medium"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "tldr-voice currently supports Linux only."
command -v python3 >/dev/null || die "python3 is required."
command -v curl >/dev/null || die "curl is required."
command -v mpv >/dev/null || warn "mpv not found: the online voice needs it (e.g. sudo apt install mpv). The offline voice still works."
command -v paplay >/dev/null || command -v aplay >/dev/null || die "paplay (PulseAudio/PipeWire) or aplay (ALSA) is required."
[ -d "$CLAUDE_DIR" ] || die "$CLAUDE_DIR not found. Install and run Claude Code once first."

say "Copying hook scripts to $CODE"
mkdir -p "$CODE"
install -m 755 "$REPO/hooks/speak.py" "$CODE/speak.py"
install -m 644 "$REPO/hooks/edge_daemon.py" "$CODE/edge_daemon.py"

say "Creating a private Python environment in $DATA/venv (edge-tts + piper-tts)"
mkdir -p "$DATA/voices"
if command -v uv >/dev/null; then
  uv venv -q --allow-existing --python 3.12 "$DATA/venv"
  uv pip install -q --python "$DATA/venv/bin/python" --upgrade edge-tts piper-tts
else
  python3 -m venv "$DATA/venv"
  "$DATA/venv/bin/pip" install -q --upgrade pip edge-tts piper-tts \
    || die "pip install failed. Installing uv (https://docs.astral.sh/uv/) and re-running usually fixes this."
fi

if [ ! -s "$DATA/voices/$VOICE.onnx" ]; then
  say "Downloading the offline Piper voice ($VOICE, ~60 MB)"
  curl -fsSL -o "$DATA/voices/$VOICE.onnx" "$VOICE_URL/$VOICE.onnx"
  curl -fsSL -o "$DATA/voices/$VOICE.onnx.json" "$VOICE_URL/$VOICE.onnx.json"
fi

say "Linking the tldr-voice command into $BIN"
mkdir -p "$BIN"
ln -sf "$CODE/speak.py" "$BIN/tldr-voice"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH; add it to use the tldr-voice command." ;; esac

say "Adding hooks to $CLAUDE_DIR/settings.json (backup saved next to it)"
python3 - "$CLAUDE_DIR/settings.json" "$CODE/speak.py" <<'PY'
import json, shutil, sys, time
from pathlib import Path
path, script = Path(sys.argv[1]), sys.argv[2]
settings = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
if path.exists():
    shutil.copy2(path, path.with_name(f"settings.json.bak-tldr-voice-{time.strftime('%Y%m%d-%H%M%S')}"))
hooks = settings.setdefault("hooks", {})
for event in list(hooks):  # drop any earlier tldr-voice entries
    hooks[event] = [g for g in hooks[event]
                    if not any("tldr-voice/speak.py" in h.get("command", "") for h in g.get("hooks", []))]
    if not hooks[event]:
        del hooks[event]
def entry(mode, **extra):
    return {"hooks": [{"type": "command", "command": f'"{script}" {mode}', **extra}]}
hooks.setdefault("UserPromptSubmit", []).append(entry("prompt", timeout=5))
hooks.setdefault("MessageDisplay", []).append(entry("display", timeout=10, **{"async": True}))
hooks.setdefault("Stop", []).append(entry("stop", timeout=30, **{"async": True}))
path.write_text(json.dumps(settings, indent=2) + "\n")
path.chmod(0o600)
PY

say "Adding the TL;DR instruction to $CLAUDE_DIR/CLAUDE.md"
python3 - "$CLAUDE_DIR/CLAUDE.md" "$REPO/claude-md-snippet.md" <<'PY'
import re, sys
from pathlib import Path
path, snippet = Path(sys.argv[1]), Path(sys.argv[2]).read_text().strip()
block = f"<!-- tldr-voice:start -->\n{snippet}\n<!-- tldr-voice:end -->"
text = path.read_text() if path.exists() else ""
pattern = re.compile(r"<!-- tldr-voice:start -->.*?<!-- tldr-voice:end -->", re.S)
text = pattern.sub(block, text) if pattern.search(text) else (text.rstrip() + "\n\n" + block).lstrip()
path.write_text(text.rstrip() + "\n")
PY

say "Testing the voice"
"$CODE/speak.py" engine edge >/dev/null
"$CODE/speak.py" say "tldr-voice is installed. Restart Claude Code to hear your answers." || true

cat <<EOF

Done. Restart Claude Code, ask it anything, and listen.

  tldr-voice off | on            turn readback off or on
  tldr-voice engine piper|edge   offline voice, or the online neural voice (default)
  tldr-voice stop-audio          stop talking now (typing your next message does this too)
EOF
