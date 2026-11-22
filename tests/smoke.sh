#!/bin/bash
# Smoke test on a real machine: voice setup, the hooks, the online-voice helper, the
# offline voice and stopping it, all through scripts/run.sh as Claude Code calls it.
# Run from anywhere: bash tests/smoke.sh. Needs Python 3 and mpv on PATH. On a machine
# with no sound card (CI), point MPV_HOME at a folder whose mpv.conf says ao=null.
set -u
cd "$(dirname "$0")/.."

export CLAUDE_PLUGIN_DATA="${SMOKE_DATA:-${RUNNER_TEMP:-$PWD/.smoke}/tldr-voice-data}"
export CLAUDE_CODE_ENTRYPOINT=cli
export TLDR_VOICE_MIC_STOP=0

failures=0
fail() { echo "::error::$*"; failures=$((failures + 1)); }
ok() { echo "ok: $*"; }
step() { echo; echo "== $*"; }
run() { sh scripts/run.sh "$@"; }

windows=false
[ "${OS:-}" = "Windows_NT" ] && windows=true
if $windows; then
  if command -v py >/dev/null 2>&1; then py() { command py -3 "$@"; }; else py() { python "$@"; }; fi
else
  py() { python3 "$@"; }
fi
# A value from speak.py itself, so the test reads the same paths the plugin uses.
speak_value() { py -c "import sys; sys.path.insert(0, 'scripts'); import speak; print($1)"; }

alive() {  # is this process, or on Linux/macOS anything in its process group, still running?
  if $windows; then
    MSYS_NO_PATHCONV=1 tasklist /FI "PID eq $1" /NH 2>/dev/null | grep -qw "$1"
  else
    # Zombies don't count: a stopped orphan may wait a while for its parent to reap it.
    ps -eo pgid=,stat= | awk -v g="$1" '$1 == g && $2 !~ /^Z/ { found = 1 } END { exit !found }'
  fi
}

step "machine"
uname -a
py --version
if command -v mpv >/dev/null; then mpv --version | head -1; else fail "mpv is not on PATH"; fi
echo "uv: $(command -v uv || echo 'not installed (setup uses pip)')"
STATE=$(speak_value "speak.STATE.as_posix()")
echo "state folder: $STATE"
echo "data folder:  $CLAUDE_PLUGIN_DATA"

step "voice setup (the background job SessionStart starts, run in the foreground)"
if run setup; then ok "setup finished"; else fail "setup failed"; fi
VENV_PY=$(speak_value "speak.VENV_PY.as_posix()")
run status

step "UserPromptSubmit hook prints the TL;DR reminder"
out=$(echo '{}' | run prompt)
if echo "$out" | grep -q "tldr-voice is on"; then ok "reminder printed"; else fail "no reminder: $out"; fi
helper_up() { speak_value "speak.daemon_send({'cmd': 'ping'}, start=False)" | grep -q True; }
# What the helper did with the last line, once it shows in its log (waits up to $1 seconds).
spoken_how() {
  for _ in $(seq 1 $(($1 * 2))); do
    how=$(grep -oE "first audio after [0-9.]+s|Piper fallback" "$STATE/edge.log" 2>/dev/null | head -1)
    [ -n "$how" ] && { echo "$how"; return; }
    sleep 0.5
  done
}
up=false
for _ in $(seq 1 40); do helper_up && { up=true; break; }; sleep 0.25; done
if $up; then ok "the hook's warm-up started the helper"; else fail "warm-up didn't start the helper"; fi

step "MessageDisplay hook speaks the TL;DR (online voice through the helper)"
rm -f "$STATE/edge.log"
# Plain words only: the plugin drops markdown symbols (Windows' uname has an underscore).
line="Hello from the smoke test. Grüße aus Köln!"
printf '{"index":0,"delta":"TL;DR: %s\\nMore text."}' "$line" | run display
[ $? -eq 0 ] && ok "hook exited 0" || fail "display hook failed"
spoken=$(spoken_how 30)
case "$spoken" in
  "first audio"*) ok "online voice: $spoken" ;;
  "Piper fallback") echo "::warning::the online voice failed here; the offline voice spoke instead" ;;
  *) fail "nothing was spoken"; cat "$STATE/edge.log" 2>/dev/null ;;
esac
if [ "$(cat "$STATE/last_spoken.txt" 2>/dev/null)" = "$line" ]; then
  ok "spoken text kept its umlauts"
else
  fail "spoken text differs: $(cat "$STATE/last_spoken.txt" 2>/dev/null)"
fi

step "the same line from the streaming hook and the end-of-reply hook is spoken once"
# The two hooks can run at the same moment; whichever claims the line first speaks it.
claims=$(for _ in 1 2 3 4 5 6; do speak_value "speak.claim('one line, six hooks')" & done; wait)
if [ "$(echo "$claims" | grep -c True)" = 1 ]; then ok "one of six got to speak"; else fail "claims: $(echo $claims)"; fi
times_spoken() { grep -cE "first audio after|Piper fallback" "$STATE/edge.log" 2>/dev/null; }
before=$(times_spoken)
printf '{"last_assistant_message":"TL;DR: %s\\nMore text."}' "$line" | run reply-end
sleep 4
[ "$(times_spoken)" = "$before" ] && ok "the end-of-reply hook left the streamed line alone" || fail "the line was spoken again"
printf '{"last_assistant_message":"Some text.\\n\\nMore.\\n\\nEven more.\\n\\nTL;DR: quoted further down."}' | run reply-end
[ "$(cat "$STATE/last_spoken.txt")" = "$line" ] && ok "a TL;DR quoted further down is not spoken" || fail "spoke a quoted line"
printf '{"agent_id":"a1","last_assistant_message":"TL;DR: from a subagent."}' | run reply-end
[ "$(cat "$STATE/last_spoken.txt")" = "$line" ] && ok "a subagent's line is not spoken" || fail "spoke a subagent's line"

step "the helper outlives the hook that started it"
sleep 3
if helper_up; then ok "helper still answers"; else fail "helper is gone after the hook exited"; fi
run stop >/dev/null || fail "stop failed"

step "the helper exits when setup asks it to (before rebuilding the environment)"
speak_value "speak.stop_helper()" >/dev/null
if helper_up; then fail "helper still running after quit"; else ok "helper exited"; fi

step "two helpers started at the same moment: one stays, and it answers"
"$VENV_PY" -I scripts/edge_daemon.py & h1=$!
"$VENV_PY" -I scripts/edge_daemon.py & h2=$!
sleep 6  # the one that lost the address notices within a few seconds and leaves
left=0
kill -0 $h1 2>/dev/null && left=$((left + 1))
kill -0 $h2 2>/dev/null && left=$((left + 1))
if [ $left -eq 1 ] && helper_up; then ok "one helper left, and it answers"; else fail "$left helpers left, or none answers"; fi
speak_value "speak.stop_helper()" >/dev/null

step "the online voice speaks while the offline voice is still missing"
MODEL=$(speak_value "speak.piper_model().as_posix()")
mv "$MODEL" "$MODEL.away"
touch "$STATE/setup.failed"  # as after a failed download: setup waits ten minutes to retry
echo '{}' | run prompt >/dev/null
rm -f "$STATE/edge.log"
printf '{"index":0,"delta":"TL;DR: The online voice does not wait for the offline one.\\n"}' | run display
case "$(spoken_how 30)" in
  "first audio"*) ok "online voice spoke" ;;
  "Piper fallback") echo "::warning::the online voice failed here, with no offline voice to take over" ;;
  *) fail "nothing was spoken"; cat "$STATE/edge.log" 2>/dev/null ;;
esac
if run status | grep -q "offline voice missing"; then ok "status says what is missing"; else fail "status: $(run status)"; fi
mv "$MODEL.away" "$MODEL"
rm -f "$STATE/setup.failed"
run stop >/dev/null

step "no connection to the online voice: the offline voice takes over"
speak_value "speak.stop_helper()" >/dev/null
# A proxy address where nothing listens: every connection to the service is refused.
# The helper takes the setting from the hook that starts it, here the prompt hook.
echo '{}' | wss_proxy=http://127.0.0.1:9 run prompt >/dev/null
rm -f "$STATE/edge.log"
printf '{"index":0,"delta":"TL;DR: The offline voice takes over.\\n"}' | wss_proxy=http://127.0.0.1:9 run display
case "$(spoken_how 15)" in
  "Piper fallback") ok "offline voice took over" ;;
  *) fail "no fallback"; cat "$STATE/edge.log" 2>/dev/null ;;
esac
run stop >/dev/null
speak_value "speak.stop_helper()" >/dev/null

step "offline voice without a shell (the Windows path), played to the end"
if "$VENV_PY" scripts/speak.py play-piper "This is the offline voice. Grüße!"; then
  ok "Piper played through mpv"
else
  fail "play-piper failed"
fi

step "offline voice as the plugin starts it, then stopped mid-sentence"
run engine piper
run test "This is a long sentence for the stop test, and it keeps going for a while so it is still playing when we stop it. It has a second sentence too."
sleep 2
pid=$(cat "$STATE/speaking.pid" 2>/dev/null)
if [ -z "$pid" ]; then
  fail "no speaking.pid"
else
  if alive "$pid"; then
    ok "still speaking 2 s later (pid $pid)"
  elif $windows; then
    fail "the offline voice ended early or never started"
  else
    echo "note: already finished (no audio device for this player here)"
  fi
  run stop
  sleep 1
  if alive "$pid"; then fail "still running after stop"; else ok "stopped"; fi
  if $windows && MSYS_NO_PATHCONV=1 tasklist /FI "IMAGENAME eq mpv.exe" /NH | grep -qi mpv.exe; then
    fail "an mpv process outlived stop"
  fi
fi
run engine edge

step "mute and unmute"
run off && [ -f "$STATE/disabled" ] && ok "muted" || fail "off didn't mute"
run on && [ ! -f "$STATE/disabled" ] && ok "unmuted" || fail "on didn't unmute"

echo
if [ "$failures" -eq 0 ]; then
  echo "All smoke checks passed."
else
  echo "$failures smoke check(s) failed."
  echo "-- setup.log"; cat "$STATE/setup.log" 2>/dev/null
  echo "-- edge.log"; cat "$STATE/edge.log" 2>/dev/null
  exit 1
fi
