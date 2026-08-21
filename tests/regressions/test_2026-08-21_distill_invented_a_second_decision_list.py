"""Regression — 2026-08-21, box session dce9a6de7f81.

The agent saved a merged decision list to pernix.decisions (epoch
1787345969). Thirty-five seconds later the automatic distill pass wrote a
SECOND pernix.decisions entry on the same topic whose "top 6" matched
nothing in the transcript. Pinned here: entries the agent saved itself are
read back and shown to the distiller as authoritative, a candidate that
restates one is dropped, and an enumerated candidate with no verbatim
footprint in the transcript is tagged unverified-distill.
"""

import json
import re

import pytest

from core.llm.types import ChatResponse, TokenUsage
from core.memory.distill import _agent_saved_entries, _parse_entries, _restates, _trigram_grounding, distill_session
from core.memory.store import get_memory_store


def _resp(content):
    return ChatResponse(
        content=content, tool_calls=None, usage=TokenUsage(10, 20, 30), model="t", provider="fake", finish_reason="stop"
    )


async def test_distill_drops_candidates_that_restate_the_agents_own_entry(
    mock_llm_client, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    store = get_memory_store()
    saved = store.add_entry(
        content=(
            "Trade-list merge 2026-08-21 (Pernix x Claude Code) — awaiting Calvin approval. Top 6 with owners: "
            "F1 refuse!=failure (Claude), F2 reflect user-words guard (Claude), F4 daily_brief memory consolidation (Pernix), "
            "F5 dream correction lane policy (Calvin), F6 embedding sweep robustness (Claude)."
        ),
        file_name="pernix.decisions",
        entry_type="decision",
        tags="trade-list,merged-ranking",
        weight="high",
        source="user",
    )
    epoch = int(re.search(r"epoch=(\d+)", saved).group(1))

    sid = db.create_session(title="trade list")
    db.add_message(sid, "user", "Calvin asked us both: what else should we work on? Trade lists. " * 8)
    db.add_message(
        sid, "assistant", "Merged ranking written to notes/collab-backlog.md section F and saved to memory. " * 8
    )
    db.add_message(sid, "tool", f"SAVED file=pernix.decisions epoch={epoch} VERIFY=OK")
    db.add_message(
        sid, "assistant", "Turn complete. The merged list has six items with owners; status awaiting approval. " * 6
    )
    messages = db.get_messages(sid)

    found = _agent_saved_entries(messages, store)
    assert (
        found and found[0][0] == "pernix.decisions" and found[0][1] == "decision" and "Trade-list merge" in found[0][2]
    )

    fabricated = (
        "A merged Trade List was created combining Pernix and Claude observations. The top 6 prioritized items "
        "with owners: 1) fix charter refusals counted as tool failures (Claude), 2) improve memory recall for stale "
        "entries (Pernix), 3) enhance adaptive proposal visibility in UI/API (Claude), 4) add auto-resume for "
        "worker spawns (Claude), 5) clarify suspect batch status logic (Pernix), 6) better error surfacing for "
        "embedding failures (Calvin). Status: awaiting Calvin approval."
    )
    novel = "The collab backlog file lives at notes/collab-backlog.md and section F holds the 2026-08-21 merge."
    invented_list = (
        "Deployment checklist agreed this session: 1) rotate the vault keys, 2) migrate the ledger schema, "
        "3) enable quorum voting, 4) archive the legacy broker."
    )
    mock_llm_client.responses = [
        _resp(
            json.dumps(
                [
                    {
                        "type": "decision",
                        "content": fabricated,
                        "file": "pernix.decisions",
                        "tags": "trade-list",
                        "weight": "high",
                    },
                    {"type": "note", "content": novel, "file": "pernix.config", "tags": "collab", "weight": "normal"},
                    {
                        "type": "note",
                        "content": invented_list,
                        "file": "pernix.lessons",
                        "tags": "deploy",
                        "weight": "normal",
                    },
                ]
            )
        )
    ]
    with caplog.at_level("INFO", logger="pernix.memory.distill"):
        await distill_session(sid, title="trade list", messages=messages)

    decisions = store.read_file("pernix.decisions") or ""
    assert decisions.count("<!-- @epoch:") == 1  # only the agent's own entry stands
    assert "auto-resume for worker spawns" not in decisions
    assert "restates the agent's own entry in pernix.decisions" in caplog.text
    assert "section F holds the 2026-08-21 merge" in (store.read_file("pernix.config") or "")
    lessons = store.read_file("pernix.lessons") or ""
    assert "rotate the vault keys" in lessons and "unverified-distill" in lessons
    assert "1 dropped as restating the agent's own entries" in caplog.text


def test_restates_and_grounding_heuristics_directly():
    saved = [
        (
            "pernix.decisions",
            "decision",
            "Top 6 with owners: F1 refuse failure Claude, F2 reflect guard Claude, F4 memory consolidation Pernix",
        )
    ]
    assert _restates(
        "Merged top 6 list with owners: F1 refuse failure, F2 reflect guard, F4 consolidation",
        "pernix.decisions",
        "decision",
        saved,
    )
    assert (
        _restates("Unrelated: the aibox GPU has 24GB of VRAM per card", "pernix.decisions", "decision", saved) is None
    )
    # Another file needs a strong overlap: a near-copy is still a restatement, a passing mention is not.
    assert _restates(
        "Merged top 6 list with owners: F1 refuse failure, F2 reflect guard", "pernix.lessons", "note", saved
    )
    assert (
        _restates("The reflect guard idea came up during the merge discussion", "pernix.lessons", "note", saved) is None
    )

    transcript = "we agreed to rotate the signing keys and then migrate the ledger schema before enabling quorum voting"
    assert _trigram_grounding("rotate the signing keys and then migrate the ledger schema", transcript) > 0.5
    assert (
        _trigram_grounding("archive the legacy broker, retire the old queue, rebuild the index cache", transcript) < 0.1
    )
    assert _trigram_grounding("short text", transcript) == 1.0  # too short to judge
