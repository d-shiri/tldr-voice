# Voice readback (tldr-voice)

Start every final reply to the user (the message after any tool work) with one line in this exact form, as the very first line:

TL;DR: <one or two short, plain sentences>

A hook reads that line aloud with text-to-speech the moment it appears, so write it to be heard: no markdown, code, file paths, URLs or symbols; say what happened or what you need from the user. If the reply asks a question, the TL;DR asks it too. Don't put a TL;DR line in short progress notes between tool calls.
