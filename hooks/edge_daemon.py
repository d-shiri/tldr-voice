"""tldr-voice helper for the Microsoft Edge neural voice. Runs in tldr-voice's own venv.

Listens on a Unix socket for one-line JSON commands from speak.py:
  {"cmd": "speak", "text": "..."}  stream audio straight into mpv (latest wins)
  {"cmd": "stop"}                  silence current speech
  {"cmd": "warm"}                  tiny throwaway request so the next one is fast

Two identical requests race and the first to answer is played, which cuts the slow
tail of the free service. If no audio arrives within FIRST_AUDIO_TIMEOUT seconds,
or the service fails, the local Piper voice speaks instead. Exits after 30 min idle.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import edge_tts

HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "tldr-voice"
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "tldr-voice"
# Unix socket paths are capped at 108 chars, so prefer the short runtime dir.
SOCK = (Path(os.environ["XDG_RUNTIME_DIR"]) / "tldr-voice.sock"
        if os.environ.get("XDG_RUNTIME_DIR") else STATE / "edge.sock")
LOG = STATE / "edge.log"

VOICE = os.environ.get("TLDR_VOICE_EDGE_VOICE", "en-US-AvaMultilingualNeural")
PIPER = DATA / "venv/bin/piper"
PIPER_MODEL = Path(os.environ.get("TLDR_VOICE_PIPER_MODEL")
                   or DATA / "voices/en_US-lessac-medium.onnx")
IDLE_EXIT = 30 * 60
RACERS = 2                 # parallel identical requests; first to answer is played
FIRST_AUDIO_TIMEOUT = 5.0  # seconds without audio -> local Piper voice instead

current: "asyncio.Task | None" = None
procs: set = set()
last_used = 0.0


def log(line: str) -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.write_text("")
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def kill_procs() -> None:
    for p in list(procs):
        try:
            os.killpg(p.pid, 15)
        except OSError:
            pass
    procs.clear()


def piper_fallback(text: str) -> None:
    if not PIPER.exists():
        return
    rate = "22050"
    try:
        rate = str(json.loads(Path(str(PIPER_MODEL) + ".json").read_text())["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError):
        pass
    player = (f"paplay --raw --rate={rate} --channels=1 --format=s16le" if shutil.which("paplay")
              else f"aplay -q -r {rate} -f S16_LE -t raw -c 1 -")
    p = subprocess.Popen(
        ["sh", "-c", f'printf "%s" "$1" | "{PIPER}" -m "{PIPER_MODEL}" --output-raw 2>/dev/null | {player}',
         "piper", text],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs.add(p)


async def race_streams(text: str):
    """Yield audio chunks from whichever of RACERS identical requests answers first."""
    queue: asyncio.Queue = asyncio.Queue()

    async def run(k: int) -> None:
        try:
            async for chunk in edge_tts.Communicate(text, VOICE).stream():
                if chunk["type"] == "audio":
                    await queue.put((k, chunk["data"]))
            await queue.put((k, None))  # finished
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - reported through the queue
            await queue.put((k, e))

    tasks = [asyncio.create_task(run(k)) for k in range(RACERS)]
    winner, dead = None, 0
    try:
        while True:
            if winner is None:
                k, item = await asyncio.wait_for(queue.get(), FIRST_AUDIO_TIMEOUT)
            else:
                k, item = await queue.get()
            if winner is None:
                if item is None or isinstance(item, Exception):
                    dead += 1
                    if dead == RACERS:
                        raise item if isinstance(item, Exception) else RuntimeError("no audio")
                    continue
                winner = k
                for j, t in enumerate(tasks):
                    if j != k:
                        t.cancel()
            if k != winner:
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        for t in tasks:
            t.cancel()


async def speak(text: str) -> None:
    t0 = asyncio.get_running_loop().time()
    # Start the player and the request together; feed audio in as it streams.
    player = await asyncio.create_subprocess_exec(
        "mpv", "--no-video", "--really-quiet", "--cache=no", "--demuxer-lavf-format=mp3",
        "--audio-buffer=0.1", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    procs.add(player)
    assert player.stdin is not None
    got_audio = False
    try:
        async for data in race_streams(text):
            if not got_audio:
                log(f"first audio after {asyncio.get_running_loop().time() - t0:.2f}s")
            got_audio = True
            player.stdin.write(data)
            await player.stdin.drain()
        player.stdin.close()
        await player.wait()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log(f"edge voice failed ({type(e).__name__}); {'Piper fallback' if not got_audio else 'stopped'}")
        kill_procs()
        if not got_audio:
            piper_fallback(text)
    finally:
        procs.discard(player)


async def warm() -> None:
    try:
        async for _ in edge_tts.Communicate("Hi.", VOICE).stream():
            pass
    except Exception:  # noqa: BLE001 - warming is best effort
        pass


async def stop_current() -> None:
    global current
    if current and not current.done():
        current.cancel()
        try:
            await current
        except BaseException:  # noqa: BLE001
            pass
    kill_procs()
    current = None


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global current, last_used
    last_used = asyncio.get_running_loop().time()
    try:
        msg = json.loads((await reader.readline()) or b"{}")
    except ValueError:
        msg = {}
    cmd = msg.get("cmd")
    if cmd == "speak" and msg.get("text"):
        await stop_current()
        current = asyncio.create_task(speak(msg["text"]))
    elif cmd == "stop":
        await stop_current()
    elif cmd == "warm" and not (current and not current.done()):
        asyncio.create_task(warm())
    writer.write(b"ok\n")
    await writer.drain()
    writer.close()


async def main() -> None:
    global last_used
    STATE.mkdir(parents=True, exist_ok=True)
    SOCK.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=str(SOCK))
    os.chmod(SOCK, 0o600)
    loop = asyncio.get_running_loop()
    last_used = loop.time()
    async with server:
        while loop.time() - last_used < IDLE_EXIT:
            await asyncio.sleep(30)
    SOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
