#!/bin/sh
# Starts speak.py with this system's Python 3. Hooks, the voice command and the mute
# button all come through here. On Windows, Claude Code runs hooks in Git Bash, where
# `python3` is usually the Microsoft Store placeholder, so use the py launcher or python.
# ${0%/*} is this script's folder: the caller always names it as <folder>/run.sh.
here=${0%/*}
if [ "$OS" = "Windows_NT" ]; then
  if command -v py >/dev/null 2>&1; then
    py -3 "$here/speak.py" "$@"
    rc=$?
    # 103: the launcher knows no Python 3 (one it didn't register, e.g. from uv). It
    # stops before reading stdin, so python below still gets the hook's input.
    [ "$rc" -ne 103 ] && exit "$rc"
  fi
  exec python "$here/speak.py" "$@"
fi
exec python3 "$here/speak.py" "$@"
