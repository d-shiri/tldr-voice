---
name: voice
description: Control tldr-voice, which reads the TL;DR line of each answer aloud. on, off, status, stop, test, language en|de, engine edge|piper.
argument-hint: "on | off | status | stop | test | language en|de | engine edge|piper"
disable-model-invocation: true
allowed-tools: Bash(python3 *)
---

!`python3 "${CLAUDE_SKILL_DIR}/../../scripts/speak.py" $ARGUMENTS`

The output above is from the tldr-voice control command. Relay it to the user in one short plain line. Don't run anything else. If it printed a usage line, list the options: on, off, status, stop, test, language en|de, engine edge|piper.
