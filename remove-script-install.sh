#!/usr/bin/env bash
# Removes tldr-voice: its hooks, its CLAUDE.md block, its command and scripts.
# Add --purge to also delete the downloaded voice and Python environment.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODE="$CLAUDE_DIR/hooks/tldr-voice"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/tldr-voice"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/tldr-voice"

[ -x "$CODE/speak.py" ] && "$CODE/speak.py" stop-audio || true
pkill -f "[t]ldr-voice/edge_daemon.py" 2>/dev/null || true

if [ -f "$CLAUDE_DIR/settings.json" ]; then
  python3 - "$CLAUDE_DIR/settings.json" <<'PY'
import json, shutil, sys, time
from pathlib import Path
path = Path(sys.argv[1])
settings = json.loads(path.read_text())
shutil.copy2(path, path.with_name(f"settings.json.bak-tldr-voice-{time.strftime('%Y%m%d-%H%M%S')}"))
hooks = settings.get("hooks", {})
for event in list(hooks):
    hooks[event] = [g for g in hooks[event]
                    if not any("tldr-voice/speak.py" in h.get("command", "") for h in g.get("hooks", []))]
    if not hooks[event]:
        del hooks[event]
path.write_text(json.dumps(settings, indent=2) + "\n")
PY
fi

if [ -f "$CLAUDE_DIR/CLAUDE.md" ]; then
  python3 - "$CLAUDE_DIR/CLAUDE.md" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
text = re.sub(r"\n*<!-- tldr-voice:start -->.*?<!-- tldr-voice:end -->\n*", "\n", path.read_text(), flags=re.S)
path.write_text(text.strip() + "\n" if text.strip() else "")
PY
fi

rm -f "$HOME/.local/bin/tldr-voice"
rm -rf "$CODE" "$STATE"
rm -f "${XDG_RUNTIME_DIR:-/nonexistent}/tldr-voice.sock"
[ "${1:-}" = "--purge" ] && rm -rf "$DATA"
echo "tldr-voice removed. Restart Claude Code."
[ "${1:-}" = "--purge" ] || echo "(Voice files kept in $DATA; run with --purge to delete them.)"
