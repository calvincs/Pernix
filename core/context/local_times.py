"""Local-time notes for tool results.

Models convert UTC by hand and get the hour and the weekday wrong: every weather
run in the 2026-10-31 grades mislabelled a timestamp. The harness does the sum.
"""

import re
from datetime import datetime, timezone

_UTC_STAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|\+00:00)(?![\w:])")
MAX_NOTES = 6


def local_time_note(content: str) -> str:
    """Append the local time of each distinct UTC timestamp in `content`."""
    if not isinstance(content, str):
        return content
    lines = []
    for stamp in dict.fromkeys(_UTC_STAMP_RE.findall(content)):
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        local = moment.astimezone(timezone.utc).astimezone()
        lines.append(f"{stamp} = {local.strftime('%A %Y-%m-%d %H:%M %Z')}")
        if len(lines) == MAX_NOTES:
            break
    if not lines:
        return content
    return content + "\n\n[local times: " + "; ".join(lines) + "]"
