"""Generated fixture for the `youtube-captions-digest` canary.

Covers the youtube-whisper skill's caption-first path without the network:
the fixture is what `yt-dlp --write-auto-subs --write-info-json` would have
left on disk, and the agent is told to work from those files only.

The caption file is shaped like real YouTube auto-captions, because that is
where a careless cleaner breaks:

  - an empty cue (timing line, no text);
  - a cue whose only text is inline timestamp / <c> tags;
  - rolling duplicates — each cue repeats the previous cue's last line
    before adding a new one — so the key sentence is present in two cues
    and must appear exactly once in the clean transcript.

The video id, title, channel and key sentence all move with the seed. The
expected values never leave this module except inside the gate commands.
"""

from __future__ import annotations

import json
import random
import shlex
import string

_TOPICS = ("tide pools", "bridge cables", "sourdough", "glacier melt", "night trains", "beehives", "old radios")
_CHANNELS = ("Field Bench", "Slow Workshop", "Northline Notes", "Tinker Hours", "The Long Table")
_COLORS = ("amber", "cobalt", "crimson", "olive", "saffron", "teal", "violet")
_ANIMALS = ("badger", "falcon", "heron", "lynx", "otter", "wombat")
_FILLER = (
    "so today we are looking at how this works",
    "the first thing you notice is the sound",
    "it took about three weeks to set up",
    "and that is where it gets interesting",
    "we will come back to this at the end",
    "thanks for sticking around this far",
)


def _video_id(rng: random.Random) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choice(alphabet) for _ in range(11))


def _ts(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def generate(seed: int) -> dict:
    rng = random.Random(seed)
    vid = _video_id(rng)
    topic = rng.choice(_TOPICS)
    title = f"What {rng.randrange(3, 13)} Years of {topic.title()} Taught Me"
    channel = rng.choice(_CHANNELS)
    key = f"the {rng.choice(_COLORS)} {rng.choice(_ANIMALS)} counted {rng.randrange(20, 99)} lanterns"

    lines = rng.sample(_FILLER, 4)
    at = rng.randrange(1, 3)  # the key sentence sits between two filler lines
    spoken = lines[:at] + [key] + lines[at:]

    cues: list[str] = ["WEBVTT", "Kind: captions", "Language: en", ""]
    t = 0.0
    # An empty cue, then a cue that is nothing but inline timing tags.
    cues += [f"{_ts(t)} --> {_ts(t + 0.5)} align:start position:0%", "", ""]
    t += 0.5
    cues += [f"{_ts(t)} --> {_ts(t + 0.5)} align:start position:0%", f"<{_ts(t + 0.1)}><c> </c><{_ts(t + 0.3)}><c> </c>", ""]
    t += 0.5
    # Rolling captions: every cue shows the previous line again, then the new one.
    previous = ""
    for line in spoken:
        start, end = t, t + 2.5
        words = line.split()
        tagged = words[0] + "".join(f"<{_ts(start + 0.2 * (i + 1))}><c> {w}</c>" for i, w in enumerate(words[1:]))
        body = [previous, tagged] if previous else [tagged]
        cues += [f"{_ts(start)} --> {_ts(end)} align:start position:0%", *body, ""]
        previous = line
        t = end
    vtt = "\n".join(cues) + "\n"

    info = {"id": vid, "title": title, "channel": channel, "duration": int(t) + 1, "language": "en"}

    out = f"summaries/{vid}"
    prompt = (
        f"The fixtures/ directory holds what a YouTube download left behind for video {vid}:\n"
        f"fixtures/{vid}.info.json (metadata) and fixtures/{vid}.en.vtt (auto-captions).\n"
        "Load the youtube-whisper skill and follow its caption-first path on these LOCAL\n"
        "files only: do not download anything and do not run whisper or any other\n"
        "transcription. If the youtube-whisper skill is not available, say so and stop.\n\n"
        "Write, inside this workspace (not the location the skill normally uses):\n"
        f"  ./{out}/transcript_clean.txt — the spoken text with timing tags, empty cues\n"
        "    and rolling duplicate lines removed, each sentence once;\n"
        f"  ./{out}/summary.md — the video's title and channel, then a short summary.\n"
    )

    check_once = (
        "import sys; "
        f"t=' '.join(open('{out}/transcript_clean.txt').read().lower().split()); "
        f"n=t.count({key!r}); "
        "sys.exit(0 if n==1 else print('key sentence count', n) or 1)"
    )

    return {
        "prompt": prompt,
        "files": {
            f"fixtures/{vid}.en.vtt": vtt,
            f"fixtures/{vid}.info.json": json.dumps(info, indent=2) + "\n",
        },
        "gates": [
            {
                "name": "outputs_exist",
                "command": f"test -s {out}/transcript_clean.txt && test -s {out}/summary.md",
                "watch_paths": [out],
            },
            {
                "name": "key_sentence_once",
                "command": f"python3 -c {shlex.quote(check_once)}",
                "watch_paths": [f"{out}/transcript_clean.txt"],
            },
            {
                "name": "title_in_summary",
                "command": f"grep -qiF {shlex.quote(title)} {out}/summary.md",
                "watch_paths": [f"{out}/summary.md"],
            },
        ],
    }
