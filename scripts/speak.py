#!/usr/bin/env python3
"""tldr-voice: speak the TL;DR line of Claude Code replies aloud.

Hook modes (first argument, hook JSON on stdin), wired up in hooks/hooks.json:
  session  SessionStart:     remember the data folder; set up the voices in the background
  prompt   UserPromptSubmit: stop speech, warm up the voice, remind Claude of the TL;DR line
  display  MessageDisplay:   speak the TL;DR as soon as it streams in at the top of a reply
  stop     Stop:             backup: speak the TL;DR if it wasn't spoken while streaming

Control modes (/tldr-voice:voice <mode>):
  on | off | status | stop | engine edge|piper | language en|de | test [TEXT] | setup

Uses only the Python standard library, so hooks start fast. The voices (edge-tts, Piper)
live in their own environment in the plugin's data folder, built on first use.
Only interactive sessions speak; headless and scheduled `claude -p` runs stay silent.
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
import urllib.request
from pathlib import Path

HOME = Path.home()
CODE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "tldr-voice"
DATA_POINTER = STATE / "data_dir"       # where hooks last saw ${CLAUDE_PLUGIN_DATA}


def data_dir() -> Path:
    """The plugin's data folder. Hooks get it in CLAUDE_PLUGIN_DATA; other callers (the
    voice helper, the control command) find it through the pointer the hooks leave."""
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(env)
    try:
        return Path(DATA_POINTER.read_text().strip())
    except OSError:
        return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "tldr-voice"


DATA = data_dir()
VENV = DATA / "venv"
VENV_PY = VENV / "bin/python"
PIPER = VENV / "bin/piper"
VOICES_DIR = DATA / "voices"
SETUP_VERSION = "2"                      # bump to rebuild the environment on update
READY = VENV / f".tldr-voice-ready-{SETUP_VERSION}"
SETUP_LOCK = DATA / "setup.lock"
SETUP_LOG = STATE / "setup.log"

DAEMON = CODE / "edge_daemon.py"
# Unix socket paths are capped at ~104 chars, so prefer the short runtime dir.
# The number changes when the helper's protocol does, so an old helper is never reused.
SOCK = (Path(os.environ["XDG_RUNTIME_DIR"]) / "tldr-voice-2.sock"
        if os.environ.get("XDG_RUNTIME_DIR") else STATE / "edge-2.sock")
OFF_FLAG = STATE / "disabled"
ENGINE_FILE = STATE / "engine"          # "edge" (default) or "piper"
LANGUAGE_FILE = STATE / "language"      # "en" or "de"; overrides the install-time choice
PID_FILE = STATE / "speaking.pid"       # process group of local (Piper) playback
LAST_FILE = STATE / "last_spoken.txt"   # avoids speaking the same TL;DR twice

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
LANGUAGES = {
    # code: (Edge voice, Piper voice path under PIPER_BASE)
    "en": ("en-US-AvaMultilingualNeural", "en/en_US/lessac/medium/en_US-lessac-medium"),
    "de": ("en-US-AvaMultilingualNeural", "de/de_DE/thorsten/medium/de_DE-thorsten-medium"),
}

RULES = ("Start your final reply (the message after any tool work) with one line "
         "'TL;DR: <one or two short, plain sentences>' as its very first line. It is read aloud, "
         "so no markdown, code, file paths, URLs or symbols: say what happened or what you need "
         "from the user, and if the reply asks a question, ask it there too. "
         "No TL;DR line in short progress notes between tool calls.")
REMINDER = {
    "en": "tldr-voice is on. " + RULES,
    "de": "tldr-voice is on. " + RULES + " Write the sentence after 'TL;DR:' in German, "
          "even when the rest of the reply is in another language; keep the 'TL;DR:' prefix.",
}
TLDR_RE = re.compile(r"^\W*TL;?DR\W*:?\W*\s*(.+)$", re.IGNORECASE)


# ---------- settings ----------

def interactive() -> bool:
    return (os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "cli"
            and os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "1") != "0")


def read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip() or default
    except OSError:
        return default


def engine() -> str:
    return read(ENGINE_FILE, "edge")


def language() -> str:
    lang = read(LANGUAGE_FILE) or os.environ.get("CLAUDE_PLUGIN_OPTION_LANGUAGE", "") or "en"
    lang = lang.strip().lower()[:2]
    return lang if lang in LANGUAGES else "en"


def edge_voice() -> str:
    return os.environ.get("TLDR_VOICE_EDGE_VOICE") or LANGUAGES[language()][0]


def piper_model() -> Path:
    custom = os.environ.get("TLDR_VOICE_PIPER_MODEL")
    if custom:
        return Path(custom)
    return VOICES_DIR / (LANGUAGES[language()][1].rsplit("/", 1)[1] + ".onnx")


# ---------- setup (runs in the background on first use) ----------

def ready() -> bool:
    return READY.exists() and VENV_PY.exists() and piper_model().exists()


def find_uv() -> str:
    for c in (shutil.which("uv"), HOME / ".local/bin/uv", HOME / ".cargo/bin/uv"):
        if c and Path(c).exists():
            return str(c)
    return ""


def ensure_setup() -> bool:
    """True when the voices are ready; otherwise start setting them up in the background."""
    if ready():
        return True
    try:
        if SETUP_LOCK.exists() and time.time() - SETUP_LOCK.stat().st_mtime < 900:
            return False  # already running
    except OSError:
        pass
    STATE.mkdir(parents=True, exist_ok=True)
    with open(SETUP_LOG, "a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "setup"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env={**os.environ, "CLAUDE_PLUGIN_DATA": str(DATA)})
    return False


def run_setup() -> int:
    """Build the private Python environment and download the offline voice. Idempotent."""
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(SETUP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - SETUP_LOCK.stat().st_mtime < 900:
            print("tldr-voice: setup is already running")
            return 0
        SETUP_LOCK.touch()  # stale lock from a crashed run
    stamp = lambda msg: print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)  # noqa: E731
    try:
        if not (READY.exists() and VENV_PY.exists()):
            stamp(f"creating the voice environment in {VENV}")
            uv = find_uv()
            if uv:
                subprocess.run([uv, "venv", "-q", "--allow-existing", "--python", "3.12", str(VENV)], check=True)
                subprocess.run([uv, "pip", "install", "-q", "--python", str(VENV_PY), "--upgrade",
                                "edge-tts", "piper-tts"], check=True)
            else:
                subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
                subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q", "--upgrade",
                                "pip", "edge-tts", "piper-tts"], check=True)
            for old in VENV.glob(".tldr-voice-ready-*"):
                old.unlink()
            READY.touch()
        model = piper_model()
        if not model.exists() and not os.environ.get("TLDR_VOICE_PIPER_MODEL"):
            VOICES_DIR.mkdir(parents=True, exist_ok=True)
            path = LANGUAGES[language()][1]
            stamp(f"downloading the offline voice {model.name} (about 60 MB)")
            for suffix in (".onnx.json", ".onnx"):  # the .onnx last: its presence means "done"
                part = VOICES_DIR / (model.name.replace(".onnx", "") + suffix + ".part")
                urllib.request.urlretrieve(PIPER_BASE + path + suffix, part)
                part.rename(VOICES_DIR / (model.name.replace(".onnx", "") + suffix))
        if not shutil.which("mpv"):
            stamp("note: mpv is not installed, so only the offline voice can play. "
                  "Install mpv for the online voice.")
        stamp("ready")
        return 0
    except Exception as e:  # noqa: BLE001 - reported in the log and in /tldr-voice:voice status
        stamp(f"setup failed: {e}")
        return 1
    finally:
        SETUP_LOCK.unlink(missing_ok=True)


# ---------- speaking ----------

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
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 env={**os.environ, "CLAUDE_PLUGIN_DATA": str(DATA)})
            time.sleep(0.05)
    return False


def stop_audio() -> None:
    daemon_send({"cmd": "stop"}, start=False)
    try:
        os.killpg(int(PID_FILE.read_text().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    PID_FILE.unlink(missing_ok=True)


def piper_command(model: Path) -> str:
    """Shell pipeline that reads $1 aloud with the local Piper voice."""
    rate = "22050"
    try:
        rate = str(json.loads(Path(str(model) + ".json").read_text())["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError):
        pass
    if shutil.which("paplay"):
        player = f"paplay --raw --rate={rate} --channels=1 --format=s16le"
    elif shutil.which("aplay"):
        player = f"aplay -q -r {rate} -f S16_LE -t raw -c 1 -"
    else:
        player = (f"mpv --really-quiet --no-video --demuxer=rawaudio --demuxer-rawaudio-rate={rate} "
                  "--demuxer-rawaudio-channels=1 --demuxer-rawaudio-format=s16le -")
    return f'printf "%s" "$1" | "{PIPER}" -m "{model}" --output-raw 2>/dev/null | {player}'


def mic_stop_enabled() -> bool:
    return os.environ.get("TLDR_VOICE_MIC_STOP", "1") != "0" and bool(shutil.which("pactl"))


def speak(text: str) -> None:
    if not text or not ensure_setup():
        return
    stop_audio()
    LAST_FILE.write_text(text)
    msg = {"cmd": "speak", "text": text, "voice": edge_voice(), "piper_model": str(piper_model()),
           "mic_stop": mic_stop_enabled()}
    # Edge voice: the warm helper streams audio as it arrives and falls back to Piper itself.
    if engine() == "edge" and shutil.which("mpv") and daemon_send(msg, start=True):
        return
    if not PIPER.exists():
        return
    # Same rule as the Edge helper: when any app opens the microphone (hold-space voice
    # input), stop. The watcher and the voice share a process group; `kill 0` ends both.
    # The kill sits inside the pipe because pactl ignores SIGPIPE and would outlive grep.
    script = piper_command(piper_model())
    if mic_stop_enabled():
        script = (f"""LC_ALL=C pactl subscribe 2>/dev/null | {{ grep -qm1 "'new' on source-output" && kill 0; }} & """
                  f"{script}; kill 0")
    proc = subprocess.Popen(
        ["sh", "-c", script, "speak", text],  # text passed as $1, never interpolated
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # own process group: outlives the hook, killable as a group
    PID_FILE.write_text(str(proc.pid))


# ---------- control command ----------

TEST_LINE = {
    "en": "Hi, I'm your TL;DR voice. I'll read the first line of every answer to you.",
    "de": "Hallo, ich bin deine TL;DR-Stimme. Ich lese dir die erste Zeile jeder Antwort vor.",
}


def status() -> str:
    if ready():
        setup = "ready"
    elif SETUP_LOCK.exists():
        setup = "setting up (about a minute)"
    else:
        setup = f"not ready, see {SETUP_LOG}"
    lines = [f"tldr-voice: {'off' if OFF_FLAG.exists() else 'on'} | language: {language()} | "
             f"engine: {engine()} | voices: {setup}"]
    if not shutil.which("mpv"):
        lines.append("mpv is not installed, so the online voice can't play; the offline voice is used.")
    if not shutil.which("pactl"):
        lines.append("pactl not found, so holding space to talk won't stop the voice (typing still does).")
    return "\n".join(lines)


def control(mode: str, args: list) -> int:
    if mode == "on":
        OFF_FLAG.unlink(missing_ok=True); print("tldr-voice: on"); return 0
    if mode == "off":
        OFF_FLAG.touch(); stop_audio(); print("tldr-voice: off"); return 0
    if mode in ("status", ""):
        print(status()); return 0
    if mode == "engine":
        if args[:1] and args[0] in ("edge", "piper"):
            ENGINE_FILE.write_text(args[0]); print("tldr-voice engine:", args[0]); return 0
        print("usage: engine edge|piper (edge = natural online voice, piper = offline)"); return 2
    if mode == "language":
        if args[:1] and args[0].lower() in LANGUAGES:
            LANGUAGE_FILE.write_text(args[0].lower())
            ensure_setup()  # fetch that language's offline voice if it's missing
            print("tldr-voice language:", args[0].lower()); return 0
        print("usage: language en|de"); return 2
    if mode in ("stop", "stop-audio"):
        stop_audio(); print("tldr-voice: stopped"); return 0
    if mode in ("test", "say"):
        if not ensure_setup():
            print("tldr-voice is still setting up its voices (about a minute). Try again shortly.")
            return 0
        speak(" ".join(args) or TEST_LINE[language()]); print("tldr-voice: speaking"); return 0
    if mode == "setup":
        return run_setup()
    print(__doc__); return 0 if mode in ("help", "-h", "--help") else 2


# ---------- entry point ----------

def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("session", "prompt", "display", "stop"):
        return control(mode, sys.argv[2:])

    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        try:  # let the helper and the control command find the data folder
            if read(DATA_POINTER) != str(DATA):
                DATA_POINTER.write_text(str(DATA))
        except OSError:
            pass
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}

    if mode == "session":
        ensure_setup()
        return 0  # SessionStart stdout would go into Claude's context, so print nothing

    if mode == "prompt":
        stop_audio()
        LAST_FILE.unlink(missing_ok=True)
        if interactive() and not OFF_FLAG.exists():
            if engine() == "edge" and ready():
                daemon_send({"cmd": "warm", "voice": edge_voice()}, start=True)  # warm up while Claude works
            print(REMINDER[language()])
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
    if tldr and tldr != read(LAST_FILE):
        speak(tldr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
