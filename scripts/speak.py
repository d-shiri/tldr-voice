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
import io
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

MACOS = sys.platform == "darwin"
WINDOWS = os.name == "nt"
# Windows Python has no Unix sockets, so there the voice helper listens on a local TCP
# port. TLDR_VOICE_TCP=1 forces that path elsewhere, to test it off Windows.
TCP = WINDOWS or os.environ.get("TLDR_VOICE_TCP") == "1"
if MACOS:
    # Homebrew's mpv and uv live here, and Claude Code isn't always started from a shell
    # that has them on PATH. The voice helper and players inherit this PATH too.
    _path = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        _path + [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if d not in _path])

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
        return Path(DATA_POINTER.read_text(encoding="utf-8").strip())
    except OSError:
        return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "tldr-voice"


DATA = data_dir()
VENV = DATA / "venv"
EXE = ".exe" if WINDOWS else ""
VENV_BIN = VENV / ("Scripts" if WINDOWS else "bin")
VENV_PY = VENV_BIN / f"python{EXE}"
PIPER = VENV_BIN / f"piper{EXE}"
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
# With TCP the helper writes "<port> <key>" here; a command without the key is ignored,
# so only someone who can read this user's state folder can make it speak.
PORT_FILE = STATE / "edge-2.port"
OFF_FLAG = STATE / "disabled"
ENGINE_FILE = STATE / "engine"          # "edge" (default) or "piper"
LANGUAGE_FILE = STATE / "language"      # "en" or "de"; overrides the install-time choice
PID_FILE = STATE / "speaking.pid"       # process group of local (Piper) playback
LAST_FILE = STATE / "last_spoken.txt"   # avoids speaking the same TL;DR twice

# Linux can play the offline voice without mpv (paplay or aplay); macOS and Windows can't.
if MACOS:
    MPV_INSTALL = "Install it with: brew install mpv"
elif WINDOWS:
    MPV_INSTALL = "Install it with: winget install shinchiro.mpv (then restart Claude Code)"
else:
    MPV_INSTALL = "Install it with your package manager, e.g. sudo apt install mpv"


def mpv_missing() -> str:
    if not (MACOS or WINDOWS) and (shutil.which("paplay") or shutil.which("aplay")):
        return "mpv is not installed, so only the offline voice can play."
    return "mpv is not installed, so no voice can play."


# ---------- processes ----------

def detached() -> dict:
    """Popen options for a process that outlives the hook, in a process group of its own
    (so it can be stopped as one, and Ctrl+C in the terminal doesn't reach it), with no
    console window on Windows."""
    if WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Stop a detached process and everything it started."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass


def utf8_env() -> dict:
    """Environment for Piper: Windows Python reads stdin in the ANSI code page unless told."""
    return {**os.environ, "PYTHONUTF8": "1"}

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
        return path.read_text(encoding="utf-8", errors="replace").strip() or default
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
    for c in (shutil.which("uv"), HOME / f".local/bin/uv{EXE}", HOME / f".cargo/bin/uv{EXE}"):
        if c and Path(c).exists():
            return str(c)
    return ""


def venv_pythons() -> list:
    """Pythons to build the voice environment with when uv isn't there, best first. Prefer a
    recent one: macOS's built-in python3 is 3.9, which only gets older onnxruntime builds for
    the offline voice. The running Python comes last as the fallback, since a found pythonX.Y
    may not be able to make a venv (Debian's missing pythonX.Y-venv, a pyenv shim)."""
    found = [shutil.which(f"python{v}") for v in ("3.13", "3.12", "3.11", "3.10")]
    return [p for p in found if p] + [sys.executable]


def make_venv() -> None:
    """Create VENV with the first Python from venv_pythons() that manages it."""
    for py in venv_pythons():
        if subprocess.run([py, "-m", "venv", "--clear", str(VENV)]).returncode == 0:
            return
    raise RuntimeError("no Python could create the voice environment (is python3-venv installed?)")


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
                         env={**os.environ, "CLAUDE_PLUGIN_DATA": str(DATA)}, **detached())
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
            stop_helper()  # Windows can't replace the venv's python.exe while it runs
            stamp(f"creating the voice environment in {VENV}")
            uv = find_uv()
            if uv:
                subprocess.run([uv, "venv", "-q", "--allow-existing", "--python", "3.12", str(VENV)], check=True)
                subprocess.run([uv, "pip", "install", "-q", "--python", str(VENV_PY), "--upgrade",
                                "edge-tts", "piper-tts"], check=True)
            else:
                make_venv()
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
                part.replace(VOICES_DIR / (model.name.replace(".onnx", "") + suffix))
        if not shutil.which("mpv"):
            stamp(f"note: {mpv_missing()} {MPV_INSTALL}")
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


def daemon_connect() -> "tuple[socket.socket, str]":
    """A connection to the running helper and the key it expects. Raises OSError (or
    ValueError for a garbled port file) when there is none."""
    if TCP:
        # No port file means no helper: fail at once, since a refused local TCP connect
        # can take Windows a second or two.
        port, key = PORT_FILE.read_text(encoding="utf-8").split()
        return socket.create_connection(("127.0.0.1", int(port)), timeout=1), key
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(str(SOCK))
    except OSError:
        s.close()
        raise
    return s, ""


def daemon_send(msg: dict, start: bool, wait: "float | None" = None) -> bool:
    """Send one command to the warm Edge-voice helper; optionally start it first. `wait` caps
    how long to wait for a freshly started helper (0: start it and don't wait)."""
    started = False
    if wait is None:
        wait = 5 if WINDOWS else 2  # Windows starts Python slower
    deadline = time.monotonic() + wait
    while True:
        stale = False  # nothing (or not our helper) answers at the address in PORT_FILE
        try:
            try:
                s, key = daemon_connect()
            except (ConnectionError, socket.timeout):
                stale = True  # a refused local connect times out on Windows
                raise
            with s:
                s.sendall((json.dumps({**msg, "key": key}) + "\n").encode())
                if not s.recv(8).startswith(b"ok"):
                    stale = True  # another program took the port, or the key is old
                    raise ConnectionError("no answer from the voice helper")
            return True
        except (OSError, ValueError):
            if not start or not VENV_PY.exists():
                return False
            if not started:  # start it before looking at the clock, so wait=0 still starts it
                if TCP and stale:
                    PORT_FILE.unlink(missing_ok=True)  # left behind by a helper that died
                subprocess.Popen([str(VENV_PY), "-I", str(DAEMON)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 env={**os.environ, "CLAUDE_PLUGIN_DATA": str(DATA)}, **detached())
                started = True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)


def stop_helper() -> None:
    """Ask a running helper to exit, and give it a moment to be gone."""
    if not daemon_send({"cmd": "quit"}, start=False):
        return
    address = PORT_FILE if TCP else SOCK
    for _ in range(30):  # the helper removes its address as it exits
        if not address.exists():
            break
        time.sleep(0.1)
    time.sleep(0.5)  # and then the process ends, releasing its files


def stop_audio() -> None:
    daemon_send({"cmd": "stop"}, start=False)
    try:
        kill_tree(int(PID_FILE.read_text().strip()))
    except (OSError, ValueError):
        pass
    PID_FILE.unlink(missing_ok=True)


def piper_rate(model: Path) -> str:
    try:
        return str(json.loads(Path(str(model) + ".json").read_text())["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError):
        return "22050"


def piper_argv(model: Path) -> list:
    return [str(PIPER), "-m", str(model), "--output-raw"]


def raw_player_argv(rate: str) -> list:
    """mpv playing Piper's raw 16-bit mono audio from stdin."""
    return [shutil.which("mpv") or "mpv", "--really-quiet", "--no-video", "--demuxer=rawaudio",
            f"--demuxer-rawaudio-rate={rate}", "--demuxer-rawaudio-channels=1",
            "--demuxer-rawaudio-format=s16le", "-"]


def play_piper(text: str) -> int:
    """Read text aloud with Piper into mpv, without a shell (Windows has no `sh`). The two
    processes are joined by a pipe; this process waits, so stopping it as a tree stops both."""
    model = piper_model()
    r, w = os.pipe()
    try:
        player = subprocess.Popen(raw_player_argv(piper_rate(model)), stdin=r,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.close(r)
    try:
        piper = subprocess.Popen(piper_argv(model), stdin=subprocess.PIPE, stdout=w,
                                 stderr=subprocess.DEVNULL, env=utf8_env())
    finally:
        os.close(w)
    piper.communicate(text.encode("utf-8"))
    rc = player.wait()
    # Done: forget this pid, so a later stop can't hit another process that reuses it.
    if read(PID_FILE) == str(os.getpid()):
        PID_FILE.unlink(missing_ok=True)
    return rc


def piper_command(model: Path) -> str:
    """Shell pipeline that reads $1 aloud with the local Piper voice (Linux and macOS)."""
    rate = piper_rate(model)
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
    LAST_FILE.write_text(text, encoding="utf-8")
    msg = {"cmd": "speak", "text": text, "voice": edge_voice(), "piper_model": str(piper_model()),
           "mic_stop": mic_stop_enabled()}
    # Edge voice: the warm helper streams audio as it arrives and falls back to Piper itself.
    if engine() == "edge" and shutil.which("mpv") and daemon_send(msg, start=True):
        return
    if not PIPER.exists():
        return
    if WINDOWS:
        # No shell pipeline here: a detached copy of this script joins Piper to mpv.
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "play-piper", text],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **detached())
        PID_FILE.write_text(str(proc.pid))
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
        lines.append(f"{mpv_missing()} {MPV_INSTALL}")
    if MACOS or WINDOWS:
        lines.append(f"On {'macOS' if MACOS else 'Windows'}, holding space to talk doesn't stop "
                     "the voice (typing still does).")
    elif not shutil.which("pactl"):
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
    if mode == "play-piper":  # internal: the Windows offline voice, started by speak()
        return play_piper(" ".join(args))
    print(__doc__); return 0 if mode in ("help", "-h", "--help") else 2


# ---------- entry point ----------

def main() -> int:
    if WINDOWS and isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # UTF-8 like everywhere else, not the code page
    STATE.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("session", "prompt", "display", "stop"):
        return control(mode, sys.argv[2:])

    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        try:  # let the helper and the control command find the data folder
            if read(DATA_POINTER) != str(DATA):
                DATA_POINTER.write_text(str(DATA), encoding="utf-8")
        except OSError:
            pass
    try:  # bytes, so json decodes UTF-8 whatever the system's code page
        data = json.loads(sys.stdin.buffer.read() or b"{}")
    except ValueError:
        data = {}

    if mode == "session":
        ensure_setup()
        return 0  # SessionStart stdout would go into Claude's context, so print nothing

    if mode == "prompt":
        stop_audio()
        LAST_FILE.unlink(missing_ok=True)
        if interactive() and not OFF_FLAG.exists():
            print(REMINDER[language()], flush=True)  # first: this hook has only 5 s
            if engine() == "edge" and ready():
                # Warm up while Claude works; a helper that isn't up yet starts without waiting.
                daemon_send({"cmd": "warm", "voice": edge_voice()}, start=True, wait=0)
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
