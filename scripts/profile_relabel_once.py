"""One-time: relabel profile entries that are not facts about the person.

Reflect and snooze filed facts from automated sessions (job prompts, run
state, system limits) as entry_type="profile". This pass finds candidates
(the profile write gate rejects them, or they mention jobs/runs/system
state), asks the background model to confirm each batch, and retypes the
confirmed ones to "note" — content and file unchanged, so search still finds
them; they just stop counting as the user's profile.

Dry run by default (prints counts only); --apply to retype.
Run inside the container: python scripts/profile_relabel_once.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.memory.profile import _SYSTEM_RE, observation_rejection  # noqa: E402
from core.memory.store import get_memory_store  # noqa: E402

PROMPT = """Each numbered line was stored as a fact about the USER (the person using an AI agent).
Decide for each line whether it is a durable fact about that person — their preferences,
expertise, communication style, habits, how they like to work with the agent.

NOT about the person: the content of a job/cron prompt or charter, the state of a system,
key, quota, run or file, what an automated run did, facts about the agent itself, or the
subject of a task.

Lines:
{lines}

Output JSON only: {{"not_person": [line numbers that are NOT about the person]}} /no_think"""


async def classify(batch: list[str]) -> set[int]:
    from core.llm.client import get_llm_client
    from core.llm.jsonx import extract_json

    lines = "\n".join(f"{i + 1}. {t[:300]}" for i, t in enumerate(batch))
    resp = await get_llm_client().chat(
        messages=[{"role": "user", "content": PROMPT.format(lines=lines)}],
        model=settings.background_model or settings.llm_model,
        max_tokens=800,
        response_format={"type": "json_object"},
    )
    data = extract_json(resp.content or "")
    out = set()
    for n in (data.get("not_person") if isinstance(data, dict) else None) or []:
        try:
            if 1 <= int(n) <= len(batch):
                out.add(int(n) - 1)
        except (TypeError, ValueError):
            pass
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    store = get_memory_store()
    conn = store._connect()
    try:
        rows = conn.execute("SELECT file_name, epoch, content FROM memory_fts WHERE entry_type = 'profile'").fetchall()
    finally:
        conn.close()
    # Narrow on purpose: a broad word list ("run", "agent") pulled in real
    # preferences, and the model then agreed with too many of them.
    cands = [r for r in rows if observation_rejection(r[2]) or _SYSTEM_RE.search(r[2])]
    print(f"profile entries={len(rows)} candidates={len(cands)}")
    confirmed = []
    for i in range(0, len(cands), 20):
        batch = cands[i : i + 20]
        for j in await classify([c[2] for c in batch]):
            confirmed.append(batch[j])
    print(f"confirmed not-about-the-person={len(confirmed)}")
    if not args.apply:
        return 0
    n = sum(1 for fn, ep, _ in confirmed if store.retype_entry(fn, int(ep), "note"))
    print(f"retyped to note={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
