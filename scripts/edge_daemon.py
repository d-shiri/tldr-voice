"""tldr-voice helper for the Microsoft Edge neural voice. Runs in tldr-voice's own venv.

Listens on a Unix socket (on Windows: a local TCP port, with a key) for one-line JSON
commands from speak.py:
  {"cmd": "speak", "text": "...", "voice": "...", "piper_model": "...", "mic_stop": true,
   "timeout": 3, "session": "...", "rate": "+0%", "speed": 1,
   "intro": "...", "intro_clip": "..."}  stream audio straight into mpv, after the intro
                                    (optional; see load_intro). A newer line from the
                                    same session replaces the one playing; one from
                                    another session waits its turn (see handle)
  {"cmd": "stop"}                   silence current speech and drop the waiting lines
  {"cmd": "stop", "session": "..."} a prompt was sent there: stop that session's line,
                                    playing or waiting; other sessions' lines go on
  {"cmd": "warm", "session": "..."} a reply is coming: hold connections open for it
  {"cmd": "rest", "session": "..."} that reply ended without a line to speak
  {"cmd": "quit"}                   stop and exit (setup is rebuilding the environment)

Opening a connection to the service takes about half a second, so between a prompt
and its reply the helper holds RACERS of them open (see keep_lines) and the sentence
goes out on them at once. Identical requests race and the first to answer is played;
while none has answered, a fresh one joins every HEDGE_EVERY seconds. This cuts the slow tail of the
free service. If no audio arrives within the timeout, or every request fails, the
local Piper voice speaks instead. Exits after 30 min idle, or when a newer helper has
taken its place.
"""
import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

import edge_tts

try:
    # What a held-open connection needs from inside edge-tts. A release that moves any
    # of it leaves LINES off, and every sentence goes through edge-tts's own interface.
    from xml.sax.saxutils import escape

    import aiohttp
    from edge_tts import communicate as _edge
    from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
    from edge_tts.data_classes import TTSConfig
    from edge_tts.drm import DRM
    _ = (_edge.connect_id, _edge.date_to_string, _edge.mkssml, _edge.ssml_headers_plus_data,
         _edge.remove_incompatible_characters, _edge._SSL_CTX,
         DRM.generate_sec_ms_gec, DRM.headers_with_muid, DRM.handle_client_response_error)
    LINES = True
except Exception:  # noqa: BLE001
    LINES = False

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir out
import speak as cfg  # noqa: E402 - shared paths and the Piper pipeline

STATE, SOCK, PIPER = cfg.STATE, cfg.SOCK, cfg.PIPER
LOG = STATE / "edge.log"
DEFAULT_VOICE = cfg.LANGUAGES["en"][0]
IDLE_EXIT = 30 * 60
RACERS = 2                 # identical requests started at once; first to answer is played
HEDGE_EVERY = 1.2          # no audio yet -> start one more fresh request this often
MAX_RACERS = 6             # cap on requests per sentence (failed ones are replaced too)
STALL_TIMEOUT = 15.0       # the playing request goes quiet for this long -> give up on it
KEEP_WARM = 20 * 60        # hold connections open this long for a reply that hasn't come
KEEP_ALIVE_EVERY = 15.0    # the service drops a connection that is silent for ~30 s

current: "asyncio.Task | None" = None
procs: set = set()
recording: set = set()   # intros being recorded for next time
current_session = ""     # whose line is playing
waiting: list = []       # lines from other sessions, in the order they came; one per session
MAX_WAITING = 4          # past this the oldest waiting line is dropped
last_used = 0.0
quitting: "asyncio.Event | None" = None  # made in main(): an Event belongs to its loop
commands: "asyncio.Lock | None" = None   # made in main() too: one command at a time
KEY = secrets.token_hex(16) if cfg.TCP else ""  # TCP only: commands must carry it
re_rate = re.compile(r"[+-]\d{1,3}%")


def end(p) -> None:
    """Stop one player process: its whole group on Linux/macOS (the Piper fallback is a
    shell pipeline there); on Windows every process is started and tracked on its own."""
    try:
        if cfg.WINDOWS:
            p.kill()
        else:
            os.killpg(p.pid, 15)
    except OSError:
        pass


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
        end(p)
    procs.clear()


async def spawn(*args, **kw) -> asyncio.subprocess.Process:
    """Start a subprocess and track it in procs. If the caller is cancelled while the
    process is being created, the new process is killed instead of left playing."""
    task = asyncio.ensure_future(asyncio.create_subprocess_exec(*args, **kw))

    def reap(t: asyncio.Future) -> None:
        if not t.cancelled() and t.exception() is None:
            end(t.result())

    try:
        p = await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(reap)
        raise
    procs.add(p)
    return p


async def piper_fallback(text: str, model: Path, factor: float = 1.0) -> None:
    if not PIPER.exists() or not model.exists():
        return
    if cfg.WINDOWS:
        await piper_fallback_windows(text, model, factor)
        return
    p = await spawn(
        "sh", "-c", cfg.piper_command(model, factor), "piper", text,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        await p.wait()  # keep speak() running while Piper talks, so the mic watch covers it
    finally:
        procs.discard(p)


async def piper_fallback_windows(text: str, model: Path, factor: float = 1.0) -> None:
    """Windows has no `sh`: start Piper and mpv joined by a pipe, each tracked in procs."""
    dn = asyncio.subprocess.DEVNULL
    r, w = os.pipe()
    try:
        player = await spawn(*cfg.raw_player_argv(cfg.piper_rate(model)),
                             stdin=r, stdout=dn, stderr=dn, **cfg.detached())
    finally:
        os.close(r)
    try:
        piper = await spawn(*cfg.piper_argv(model, factor), stdin=asyncio.subprocess.PIPE, stdout=w,
                            stderr=dn, env=cfg.utf8_env(), **cfg.detached())
    finally:
        os.close(w)
    try:
        assert piper.stdin is not None
        piper.stdin.write(text.encode("utf-8"))
        await piper.stdin.drain()
        piper.stdin.close()
        await player.wait()
    finally:
        procs.discard(player)
        procs.discard(piper)


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


def load_intro(msg: dict) -> bytes:
    """The recording of the line's intro ("Message from <project>."), if there is one."""
    clip = str(msg.get("intro_clip") or "")
    if not msg.get("intro") or not clip:
        return b""
    try:
        return Path(clip).read_bytes()
    except OSError:
        return b""


async def record_intro(intro: str, voice: str, clip: Path, rate: str = "+0%") -> None:
    """Record the intro once, so from then on it plays from disk without waiting for the
    service. Its audio has the same format as a line's, so the two play as one stream."""
    tmp = clip.with_suffix(f".{os.getpid()}.part")
    try:
        clip.parent.mkdir(parents=True, exist_ok=True)
        await edge_tts.Communicate(intro, voice, rate=rate).save(str(tmp))
        if tmp.stat().st_size > 0:
            tmp.replace(clip)
    except Exception as e:  # noqa: BLE001 - it is tried again with the next intro
        log(f"couldn't record the intro ({type(e).__name__})")
    finally:
        tmp.unlink(missing_ok=True)


def record_later(msg: dict) -> None:
    task = asyncio.ensure_future(record_intro(
        str(msg["intro"]), msg.get("voice") or DEFAULT_VOICE, Path(str(msg["intro_clip"])),
        rate_of(msg)))
    recording.add(task)  # asyncio itself keeps only a weak reference to a running task
    task.add_done_callback(recording.discard)


async def speak_until_mic(msg: dict, lines: list) -> None:
    """Speak, but stop the moment the microphone opens: you want to talk, so the voice stops."""
    intro = load_intro(msg)
    text = msg["text"]
    if msg.get("intro") and not intro:
        text = f"{msg['intro']} {text}"  # no recording yet: the service says it with the line
        if msg.get("intro_clip"):
            record_later(msg)
    speech = asyncio.create_task(speak(text, msg.get("voice") or DEFAULT_VOICE,
                                       Path(msg.get("piper_model") or ""),
                                       first_audio_timeout(msg), lines, intro,
                                       rate_of(msg), speed_of(msg)))
    watch = asyncio.create_task(mic_opened(bool(msg.get("mic_stop", True))))
    try:
        await asyncio.wait({speech, watch}, return_when=asyncio.FIRST_COMPLETED)
        if watch.done() and not watch.cancelled() and watch.exception() is None and watch.result():
            log("microphone opened; speech stopped")
            speech.cancel()
            kill_procs()
            if waiting:  # you are talking now: the other lines would talk over you
                log(f"dropped {len(waiting)} waiting line(s)")
                waiting.clear()
        await asyncio.gather(speech, return_exceptions=True)
    finally:
        speech.cancel()
        watch.cancel()


# ---------- connections held open for the next sentence ----------

class Line:
    """One open connection to the speech service, good for one sentence."""

    def __init__(self, session, ws) -> None:
        self.session, self.ws = session, ws

    async def close(self) -> None:
        for part in (self.ws, self.session):
            try:  # the goodbye waits for the service's answer, which a dead network never sends
                await asyncio.wait_for(part.close(), 2)
            except Exception:  # noqa: BLE001 - it's going away either way
                pass


closing: set = set()


def let_go(line: Line) -> None:
    """Close a line in the background, so no sentence waits for the goodbye. Letting the
    same line go twice does no harm."""
    task = asyncio.ensure_future(line.close())
    closing.add(task)  # asyncio itself keeps only a weak reference to a running task
    task.add_done_callback(closing.discard)


def config_message() -> str:
    """The settings message every sentence starts with. Sending it again later is also
    what keeps an idle connection open: websocket pings don't."""
    return (f"X-Timestamp:{_edge.date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')


async def open_line() -> Line:
    session = aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
    try:
        for attempt in (1, 2):
            try:
                ws = await session.ws_connect(
                    f"{WSS_URL}&ConnectionId={_edge.connect_id()}"
                    f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
                    f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
                    compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_edge._SSL_CTX)
                return Line(session, ws)
            except aiohttp.ClientResponseError as e:
                if e.status != 403 or attempt == 2:
                    raise
                DRM.handle_client_response_error(e)  # this clock is off: adjust, try again
    except BaseException:
        await session.close()
        raise
    raise AssertionError("unreachable")


async def line_audio(line: Line, text: str, voice: str, rate: str = "+0%"):
    """Yield the audio for one sentence sent over an open line."""
    tts = TTSConfig(voice, rate, "+0%", "+0Hz", "SentenceBoundary")
    ssml = _edge.mkssml(tts, escape(_edge.remove_incompatible_characters(text)))
    await line.ws.send_str(config_message())
    await line.ws.send_str(
        _edge.ssml_headers_plus_data(_edge.connect_id(), _edge.date_to_string(), ssml))
    got_audio = False
    async for m in line.ws:
        if m.type == aiohttp.WSMsgType.TEXT:
            if "Path:turn.end" in m.data.split("\r\n\r\n", 1)[0]:
                break
        elif m.type == aiohttp.WSMsgType.BINARY:
            data = m.data[int.from_bytes(m.data[:2], "big") + 2:]  # after the header block
            if data:
                got_audio = True
                yield data
        else:
            raise ConnectionError("the line broke")
    if not got_audio:  # also how a line the service closed while it waited shows up
        raise ConnectionError("no audio on the open line")


warm_until: dict = {}  # session -> when to stop holding lines open for its reply
kept: list = []        # open lines, waiting for the next sentence
wake: "asyncio.Event | None" = None  # made in main(); set when who wants lines has changed


def wanted() -> bool:
    now = asyncio.get_running_loop().time()
    for session in [s for s, until in warm_until.items() if until <= now]:
        del warm_until[session]
    return bool(warm_until)


async def keep_lines() -> None:
    """While some session waits for its reply, hold open one line for each request the
    sentence will start with. Runs for as long as the helper does."""
    assert wake is not None
    loop = asyncio.get_running_loop()
    next_sign_of_life = 0.0
    try:
        while True:
            if not wanted():
                for line in take_lines():
                    let_go(line)
                await wake.wait()  # until a prompt says a reply is coming
                wake.clear()
                continue
            opened = await asyncio.gather(*[open_line() for _ in range(RACERS - len(kept))],
                                          return_exceptions=True)
            for line in opened:
                if isinstance(line, Line):
                    kept.append(line)
                elif isinstance(line, Exception):  # offline, or the service says no
                    log(f"couldn't open a line ahead of the reply ({type(line).__name__})")
                else:
                    raise line  # cancelled
            try:
                await asyncio.wait_for(wake.wait(), max(0.0, next_sign_of_life - loop.time()))
            except asyncio.TimeoutError:
                pass
            wake.clear()
            if loop.time() >= next_sign_of_life:
                next_sign_of_life = loop.time() + KEEP_ALIVE_EVERY
                for line in list(kept):
                    try:
                        await line.ws.send_str(config_message())
                    except Exception:  # noqa: BLE001 - the service closed it: open another
                        if line in kept:  # not if a sentence took it meanwhile
                            kept.remove(line)
                            let_go(line)
    finally:
        for line in take_lines():
            let_go(line)


def keep_warm(session: str) -> None:
    if LINES and wake is not None:
        warm_until[session] = asyncio.get_running_loop().time() + KEEP_WARM
        wake.set()


def take_lines() -> list:
    """The held lines. They belong to the caller now, who closes them."""
    lines = kept[:]
    kept.clear()
    return lines


async def race_streams(text: str, voice: str, timeout: float, lines: list, t0: float,
                       rate: str = "+0%"):
    """Yield audio chunks from whichever identical request answers first.

    The free service has a slow tail: how long a new sentence takes depends on the voice
    (well under a second for most, several for the Multilingual ones) and now and then
    one takes far longer. Starting RACERS at once and adding a fresh one every HEDGE_EVERY
    seconds (and right away when one fails) keeps the online voice instead of falling
    back to Piper. The first requests go out on `lines`, the connections held open for
    this sentence; with none left, a request opens its own, which takes about half a
    second more. No audio within `timeout` seconds raises.

    The service starts sending once it has the line's first sentence, so the wait grows
    with that sentence's length, not the line's. Asking for the first sentence on its own
    was tried and measured: it starts no sooner.
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async def run(k: int) -> None:
        try:
            if k < len(lines):
                line, got_audio = lines[k], False
                try:
                    async for data in line_audio(line, text, voice, rate):
                        got_audio = True
                        await queue.put((k, data))
                except Exception as e:  # noqa: BLE001
                    if got_audio:
                        raise
                    log(f"the open line gave no audio ({type(e).__name__}); asking again")
                finally:
                    let_go(line)
                if got_audio:
                    await queue.put((k, None))  # finished
                    return
            async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
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
                k, item = await asyncio.wait_for(queue.get(), STALL_TIMEOUT)
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
                log(f"first audio after {loop.time() - t0:.2f}s"
                    + (" on an open line" if k < len(lines) else ""))
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


async def speak(text: str, voice: str, piper_model: Path, timeout: float, lines: list,
                intro: bytes = b"", rate: str = "+0%", factor: float = 1.0) -> None:
    """Say one sentence. `lines` are the connections that were held open for it. `intro` is
    recorded audio that plays first, while the service is still working on the sentence."""
    t0 = asyncio.get_running_loop().time()
    player = None
    got_audio = False
    try:
        # Start the player and the request together; feed audio in as it streams.
        player = await spawn(
            *cfg.stream_player_argv(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **cfg.detached())
        assert player.stdin is not None
        if intro:
            player.stdin.write(intro)
            await player.stdin.drain()
        async for data in race_streams(text, voice, timeout, lines, t0, rate):
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
            await piper_fallback(text, piper_model, factor)
    finally:
        procs.discard(player)
        for line in lines:
            let_go(line)  # however this ended


def rate_of(msg: dict) -> str:
    """The online voice's rate the line asks for, such as "+20%"; anything else is "+0%"."""
    rate = str(msg.get("rate") or "+0%")
    return rate if re_rate.fullmatch(rate) else "+0%"


def speed_of(msg: dict) -> float:
    try:
        return min(2.0, max(0.5, float(msg.get("speed") or 1)))
    except (TypeError, ValueError):
        return 1.0


def first_audio_timeout(msg: dict) -> float:
    try:
        return min(30.0, max(1.0, float(msg.get("timeout") or cfg.EDGE_TIMEOUT)))
    except (TypeError, ValueError):
        return cfg.EDGE_TIMEOUT


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


def busy() -> bool:
    return current is not None and not current.done()


def play(msg: dict) -> None:
    """Start speaking a line now. Call with nothing playing, holding `commands`."""
    global current, current_session
    assert wake is not None
    lines = take_lines()  # now, before the keeper sees they're no longer wanted
    wake.set()  # another session may still be waiting: open new ones for it
    current_session = str(msg.get("session") or "")
    current = asyncio.create_task(speak_until_mic(msg, lines))
    current.add_done_callback(finished)


def finished(task: asyncio.Task) -> None:
    """A line ended. Unless it was stopped (whoever stopped it decides what comes next),
    the next waiting line gets its turn."""
    if not task.cancelled() and waiting:
        task = asyncio.ensure_future(play_next())
        recording.add(task)  # held like the recordings, so it isn't collected mid-way
        task.add_done_callback(recording.discard)


async def play_next() -> None:
    assert commands is not None
    async with commands:
        if waiting and not busy():
            play(waiting.pop(0))


def wait_turn(msg: dict) -> None:
    """Hold a line until the one playing ends. A newer line from the same session takes
    the place of its older one: only a session's latest line is worth hearing."""
    session = str(msg.get("session") or "")
    waiting[:] = [m for m in waiting if str(m.get("session") or "") != session] + [msg]
    if len(waiting) > MAX_WAITING:
        del waiting[0]
    log(f"line waits its turn ({len(waiting)} waiting)")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global last_used
    last_used = asyncio.get_running_loop().time()
    try:
        msg = json.loads((await reader.readline()) or b"{}")
    except ValueError:
        msg = {}
    if KEY and not secrets.compare_digest(str(msg.get("key", "")), KEY):
        writer.close()  # not from speak.py: another program found the port
        return
    cmd, session = msg.get("cmd"), str(msg.get("session") or "")
    assert commands is not None and wake is not None
    async with commands:  # one at a time: two sentences arriving together would both play
        if cmd == "speak" and msg.get("text"):
            warm_until.pop(session, None)  # this is the reply the lines were held for
            # Lines from two sessions both get heard: the later one waits its turn. A
            # newer line from the session that is talking replaces what it was saying.
            if busy() and session != current_session:
                wait_turn(msg)
            else:
                waiting[:] = [m for m in waiting if str(m.get("session") or "") != session]
                await stop_current()
                play(msg)
        elif cmd == "stop" and session:
            # The user sent a prompt in that session: its line is old news, so it stops and
            # its waiting line goes. A line from another session plays on: it is news.
            waiting[:] = [m for m in waiting if str(m.get("session") or "") != session]
            if session == current_session:
                await stop_current()
                if waiting:
                    play(waiting.pop(0))
        elif cmd in ("stop", "quit"):
            waiting.clear()
            await stop_current()
            if cmd == "quit" and quitting:  # setup is about to rebuild the venv we run from
                quitting.set()
        elif cmd == "warm":
            keep_warm(session)
        elif cmd == "rest":
            warm_until.pop(session, None)
            wake.set()
    writer.write(b"ok\n")
    await writer.drain()
    writer.close()


def register(registered: Path, mine: str) -> None:
    tmp = registered.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(mine, encoding="utf-8")
    tmp.replace(registered)  # written whole, only once the address is listening


async def main() -> None:
    global last_used, quitting, commands, wake
    quitting, commands, wake = asyncio.Event(), asyncio.Lock(), asyncio.Event()
    STATE.mkdir(parents=True, exist_ok=True)
    # Whoever's name is in `registered` is the helper speak.py talks to. Two can start at
    # the same moment; the later one takes the address, and the earlier one sees its name
    # gone, leaves, and touches nothing on the way out.
    if cfg.TCP:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        registered, mine = cfg.PORT_FILE, f"{port} {KEY}"
        register(registered, mine)
    else:
        import fcntl  # not on Windows, which takes the branch above
        registered, mine = cfg.OWNER_FILE, f"{os.getpid()} {secrets.token_hex(8)}"
        with open(STATE / "helper.lock", "w") as lock:
            # Taking the socket and writing the name must not interleave with another
            # helper doing the same, or the name and the socket end up with different ones.
            fcntl.flock(lock, fcntl.LOCK_EX)
            SOCK.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(handle, path=str(SOCK))
            os.chmod(SOCK, 0o600)
            register(registered, mine)
    cfg.STARTING.unlink(missing_ok=True)
    keeper = asyncio.create_task(keep_lines()) if LINES else None
    if sys.argv[1:2] == ["--warm"]:  # started by a prompt: its reply is on the way
        keep_warm(sys.argv[2] if len(sys.argv) > 2 else "")

    def still_mine() -> bool:
        return cfg.read(registered) == mine

    loop = asyncio.get_running_loop()
    last_used = loop.time()
    async with server:
        while loop.time() - last_used < IDLE_EXIT and not quitting.is_set() and still_mine():
            try:
                await asyncio.wait_for(quitting.wait(), 2)
            except asyncio.TimeoutError:
                pass
    if keeper is not None:
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)
    if still_mine():
        registered.unlink(missing_ok=True)
        if not cfg.TCP:
            SOCK.unlink(missing_ok=True)
    else:
        log("a newer helper took over; this one is leaving")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
