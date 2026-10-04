---
name: youtube-captions-digest
generated: true
timeout: 900
tags: [generated, holdout, skill]
covers: [skill:youtube-whisper]
flaky: false
last_reviewed: 2026-10-02
---

GENERATED — the youtube-whisper skill's caption-first path, offline.
`generate.py` writes a video's `.info.json` (title, channel) and an English
`.vtt` caption file under `fixtures/`, shaped like real YouTube
auto-captions: an empty cue, a cue holding nothing but inline timestamp
tags, and rolling duplicates where each cue repeats the previous line. A
seeded key sentence is spoken across two rolling cues.

The agent must load the skill and follow its caption path on the local
files only — no download, no whisper — writing
`./summaries/<id>/transcript_clean.txt` and `./summaries/<id>/summary.md`.
Gates: both files exist and are non-empty, the key sentence appears exactly
once in the clean transcript (rolling duplicates collapsed), and the
video's title is in the summary.

The skill lives in `data/skills/`, which is not in the repository. Where it
is missing the prompt tells the agent to stop, so the run records a
gate_fail rather than passing on improvisation.
