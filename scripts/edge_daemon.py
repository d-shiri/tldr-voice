"""tldr-voice helper for the Microsoft Edge neural voice. Runs in tldr-voice's own venv.

Listens on a Unix socket for one-line JSON commands from speak.py:
  {"cmd": "speak", "text": "...", "voice": "...", "piper_model": "...", "mic_stop": true}
                                   stream audio straight into mpv (latest wins)
  {"cmd": "stop"}                  silence current speech
  {"cmd": "warm", "voice": "..."}  tiny throwaway request so the next one is fast

Identical requests race and the first to answer is played; while none has answered,
a fresh one joins every HEDGE_EVERY seconds. This cuts the slow tail of the free
service. If no audio arrives within FIRST_AUDIO_TIMEOUT seconds, or every request
fails, the local Piper voice speaks instead. Exits after 30 min idle.
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir out
import speak as cfg  # noqa: E402 - shared paths and the Piper pipeline

STATE, SOCK, PIPER = cfg.STATE, cfg.SOCK, cfg.PIPER
LOG = STATE / "edge.log"
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
IDLE_EXIT = 30 * 60
RACERS = 2                 # identical requests started at once; first to answer is played
HEDGE_EVERY = 1.2          # no audio yet -> start one more fresh request this often
MAX_RACERS = 6             # cap on requests per sentence (failed ones are replaced too)
FIRST_AUDIO_TIMEOUT = 8.0  # seconds without audio -> local Piper voice instead

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


async def spawn(*args, **kw) -> asyncio.subprocess.Process:
    """Start a subprocess and track it in procs. If the caller is cancelled while the
    process is being created, the new process is killed instead of left playing."""
    task = asyncio.ensure_future(asyncio.create_subprocess_exec(*args, **kw))

    def reap(t: asyncio.Future) -> None:
        if not t.cancelled() and t.exception() is None:
            try:
                os.killpg(t.result().pid, 15)
            except OSError:
                pass

    try:
        p = await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(reap)
        raise
    procs.add(p)
    return p


async def piper_fallback(text: str, model: Path) -> None:
    if not PIPER.exists() or not model.exists():
        return
    p = await spawn(
        "sh", "-c", cfg.piper_command(model), "piper", text,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        await p.wait()  # keep speak() running while Piper talks, so the mic watch covers it
    finally:
        procs.discard(p)


async def mic_opened(enabled: bool) -> bool:
    """Return True as soon as any app starts recording (e.g. Claude Code's hold-space voice
    input). Returns False if it can't watch (off, no pactl, or the audio server went away)."""
    if not enabled or not shutil.which("pactl"):
        return False
    p = await asyncio.create_subprocess_exec(
        "pactl", "subscribe", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "LC_ALL": "C"})  # pactl translates its event lines
    try:
        assert p.stdout is not None
        async for line in p.stdout:
            if b"'new' on source-output" in line:
                return True
        return False
    finally:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        await p.wait()


async def speak_until_mic(msg: dict) -> None:
    """Speak, but stop the moment the microphone opens: you want to talk, so Ava stops."""
    speech = asyncio.create_task(speak(msg["text"], msg.get("voice") or DEFAULT_VOICE,
                                       Path(msg.get("piper_model") or "")))
    watch = asyncio.create_task(mic_opened(bool(msg.get("mic_stop", True))))
    try:
        await asyncio.wait({speech, watch}, return_when=asyncio.FIRST_COMPLETED)
        if watch.done() and not watch.cancelled() and watch.exception() is None and watch.result():
            log("microphone opened; speech stopped")
            speech.cancel()
            kill_procs()
        await asyncio.gather(speech, return_exceptions=True)
    finally:
        speech.cancel()
        watch.cancel()


async def race_streams(text: str, voice: str):
    """Yield audio chunks from whichever identical request answers first.

    The free service has a slow tail: most requests answer in ~0.6 s, some take 3-6 s.
    Starting RACERS at once and adding a fresh one every HEDGE_EVERY seconds (and right
    away when one fails) keeps the Ava voice instead of falling back to Piper.
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FIRST_AUDIO_TIMEOUT

    async def run(k: int) -> None:
        try:
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk["type"] == "audio":
                    await queue.put((k, chunk["data"]))
            await queue.put((k, None))  # finished
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - reported through the queue
            await queue.put((k, e))

    tasks: list = []

    def start() -> None:
        tasks.append(asyncio.create_task(run(len(tasks))))

    for _ in range(RACERS):
        start()
    winner, dead, last_err = None, 0, None
    next_hedge = loop.time() + HEDGE_EVERY
    try:
        while True:
            if winner is None:
                now = loop.time()
                if now >= deadline:
                    raise last_err or TimeoutError("no audio")
                try:
                    k, item = await asyncio.wait_for(
                        queue.get(), max(0.0, min(next_hedge, deadline) - now))
                except asyncio.TimeoutError:
                    if loop.time() >= next_hedge:
                        if len(tasks) < MAX_RACERS:
                            start()
                            next_hedge = loop.time() + HEDGE_EVERY
                        else:
                            next_hedge = deadline
                    continue
            else:
                k, item = await queue.get()
            if winner is None:
                if item is None or isinstance(item, Exception):
                    dead += 1
                    if isinstance(item, Exception):
                        last_err = item
                    if len(tasks) < MAX_RACERS:
                        start()  # replace the failed request right away
                    elif dead == len(tasks):
                        raise last_err or RuntimeError("no audio")
                    continue
                winner = k
                if len(tasks) > RACERS:
                    log(f"answered by request {k + 1} of {len(tasks)}")
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


async def speak(text: str, voice: str, piper_model: Path) -> None:
    t0 = asyncio.get_running_loop().time()
    # Start the player and the request together; feed audio in as it streams.
    player = await spawn(
        "mpv", "--no-video", "--really-quiet", "--cache=no", "--demuxer-lavf-format=mp3",
        "--audio-buffer=0.1", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    assert player.stdin is not None
    got_audio = False
    try:
        async for data in race_streams(text, voice):
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
            await piper_fallback(text, piper_model)
    finally:
        procs.discard(player)


async def warm(voice: str) -> None:
    try:
        async for _ in edge_tts.Communicate("Hi.", voice).stream():
            pass
    except Exception:  # noqa: BLE001 - warming is best effort
        pass


async def stop_current() -> None:
    global current
    # Kill the audio first: a cancelled speak() drops its processes from procs on the way out.
    kill_procs()
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
        current = asyncio.create_task(speak_until_mic(msg))
    elif cmd == "stop":
        await stop_current()
    elif cmd == "warm" and not (current and not current.done()):
        asyncio.create_task(warm(msg.get("voice") or DEFAULT_VOICE))
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
