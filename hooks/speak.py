#!/usr/bin/env python3
"""tldr-voice: speak the TL;DR line of Claude Code replies aloud.

Hook modes (first argument, hook JSON on stdin):
  prompt   UserPromptSubmit: stop speech, warm up the voice, remind Claude of the TL;DR line
  display  MessageDisplay:   speak the TL;DR as soon as it streams in at the top of a reply
  stop     Stop:             backup: speak the TL;DR if it wasn't spoken while streaming

Command modes (`tldr-voice <cmd>`):
  on | off | status | stop-audio | engine edge|piper | say TEXT

Uses only the Python standard library, so hooks start fast. Only interactive sessions
speak (CLAUDE_CODE_ENTRYPOINT=cli); headless and scheduled `claude -p` runs stay silent.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "tldr-voice"
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "tldr-voice"

VENV_PY = DATA / "venv/bin/python"
PIPER = DATA / "venv/bin/piper"
PIPER_MODEL = Path(os.environ.get("TLDR_VOICE_PIPER_MODEL")
                   or DATA / "voices/en_US-lessac-medium.onnx")
DAEMON = CODE / "edge_daemon.py"
# Unix socket paths are capped at 108 chars, so prefer the short runtime dir.
SOCK = (Path(os.environ["XDG_RUNTIME_DIR"]) / "tldr-voice.sock"
        if os.environ.get("XDG_RUNTIME_DIR") else STATE / "edge.sock")
OFF_FLAG = STATE / "disabled"
ENGINE_FILE = STATE / "engine"          # "edge" (default) or "piper"
PID_FILE = STATE / "speaking.pid"       # process group of local (Piper) playback
LAST_FILE = STATE / "last_spoken.txt"   # avoids speaking the same TL;DR twice

REMINDER = ("Voice readback is on: start your final reply with one line "
            "'TL;DR: <one or two plain sentences, no markdown or code>' as its very first line.")
TLDR_RE = re.compile(r"^\W*TL;?DR\W*:?\W*\s*(.+)$", re.IGNORECASE)


# ---------- helpers ----------

def interactive() -> bool:
    return (os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "cli"
            and os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "1") != "0")


def engine() -> str:
    try:
        return ENGINE_FILE.read_text().strip() or "edge"
    except OSError:
        return "edge"


def clean(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)               # inline code -> plain
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links -> label
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#>|~]", "", text)                   # markdown symbols
    return re.sub(r"\s+", " ", text).strip()[:400]


def extract_tldr(message: str) -> str:
    for line in message.splitlines():
        m = TLDR_RE.match(line.strip())
        if m:
            return clean(m.group(1))
    return ""


def last_spoken() -> str:
    try:
        return LAST_FILE.read_text()
    except OSError:
        return ""


def daemon_send(msg: dict, start: bool) -> bool:
    """Send one command to the warm Edge-voice helper; optionally start it first."""
    payload = (json.dumps(msg) + "\n").encode()
    for attempt in range(40 if start else 1):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(str(SOCK))
                s.sendall(payload)
                s.recv(8)
            return True
        except OSError:
            if not start or not VENV_PY.exists():
                return False
            if attempt == 0:
                subprocess.Popen([str(VENV_PY), "-I", str(DAEMON)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.05)
    return False


def stop_audio() -> None:
    daemon_send({"cmd": "stop"}, start=False)
    try:
        os.killpg(int(PID_FILE.read_text().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    PID_FILE.unlink(missing_ok=True)


def piper_command() -> str:
    """Shell pipeline that reads $1 aloud with the local Piper voice."""
    rate = "22050"
    try:
        rate = str(json.loads(Path(str(PIPER_MODEL) + ".json").read_text())["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError):
        pass
    if shutil.which("paplay"):
        player = f"paplay --raw --rate={rate} --channels=1 --format=s16le"
    else:
        player = f"aplay -q -r {rate} -f S16_LE -t raw -c 1 -"
    return f'printf "%s" "$1" | "{PIPER}" -m "{PIPER_MODEL}" --output-raw 2>/dev/null | {player}'


def speak(text: str) -> None:
    if not text:
        return
    stop_audio()
    LAST_FILE.write_text(text)
    # Edge voice: the warm helper streams audio as it arrives and falls back to Piper itself.
    if engine() == "edge" and daemon_send({"cmd": "speak", "text": text}, start=True):
        return
    if not PIPER.exists():
        return
    proc = subprocess.Popen(
        ["sh", "-c", piper_command(), "speak", text],  # text passed as $1, never interpolated
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # own process group: outlives the hook, killable as a group
    PID_FILE.write_text(str(proc.pid))


# ---------- entry point ----------

def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "on":
        OFF_FLAG.unlink(missing_ok=True); print("tldr-voice: on"); return 0
    if mode == "off":
        OFF_FLAG.touch(); stop_audio(); print("tldr-voice: off"); return 0
    if mode == "status":
        print("tldr-voice:", "off" if OFF_FLAG.exists() else "on", "| engine:", engine()); return 0
    if mode == "engine":
        if len(sys.argv) > 2 and sys.argv[2] in ("edge", "piper"):
            ENGINE_FILE.write_text(sys.argv[2]); print("tldr-voice engine:", sys.argv[2]); return 0
        print("usage: tldr-voice engine edge|piper", file=sys.stderr); return 2
    if mode == "stop-audio":
        stop_audio(); return 0
    if mode == "say":
        speak(" ".join(sys.argv[2:])); return 0
    if mode not in ("prompt", "display", "stop"):
        print(__doc__); return 0 if mode in ("", "help", "-h", "--help") else 2

    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}

    if mode == "prompt":
        stop_audio()
        LAST_FILE.unlink(missing_ok=True)
        if interactive() and not OFF_FLAG.exists():
            if engine() == "edge":
                daemon_send({"cmd": "warm"}, start=True)  # warm up while Claude works
            print(REMINDER)
        return 0

    if not interactive() or OFF_FLAG.exists():
        return 0

    if mode == "display":
        if int(data.get("index", 99)) > 2:   # only the first lines of a message
            return 0
        text = data.get("delta") or ""
    else:  # stop
        text = data.get("last_assistant_message") or ""

    tldr = extract_tldr(text)
    if tldr and tldr != last_spoken():
        speak(tldr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
