"""Pernix — the user profile: one write gate and a consolidation pass.

Two automatic writers add facts about the user: reflect (up to two
observations per turn) and snooze's insight extraction. Both went through
add_entry's general duplicate gate only, which catches near-identical text
but not the same fact said differently. Profile entries grew about 60 a day
and spread over a hundred files, so recall saw many partial or outdated
versions of one fact.

This module gives the profile a lifecycle:

  * save_fact — the one write path for automatic profile facts. A clear
    correction of a stored fact replaces it (add_or_supersede_entry); a fact
    that means the same as a stored one (embedding cosine >= the gate) is
    skipped; at most `profile_daily_cap` automatic facts are written per day.
  * consolidate_step — a bounded snooze pass. It takes one cluster of
    near-duplicate profile entries (cosine >= `profile_cluster_cosine`),
    asks the background model to merge them into fewer self-contained facts
    (newest wins on conflict), writes the result to user.profile and archives
    the originals. Archiving keeps them in the file as the undo record.
"""

from __future__ import annotations

import json
import logging
import re
import time

from config import settings

logger = logging.getLogger("pernix.memory.profile")

_DAY = 86400

# Sessions whose user turns come from the person. Everything else (worker,
# cron, canary, rlm, snooze) speaks for a job or a parent agent.
PROFILE_SESSION_TYPES = frozenset({"normal"})

_PERSON_RE = re.compile(r"\b(?:the user|user's|users?|they|their|them|he|she|his|her)\b", re.IGNORECASE)
# Subjectless or named-subject facts: "Prefers …", "Uses …", "<Name> prefers …".
_LEADING_VERB_RE = re.compile(
    r"^(?:[A-Z][a-z]+\s+)?(?:prefers|uses|wants|expects|likes|dislikes|values|communicates|writes|asks|works|"
    r"reads|runs|tends|avoids|favors|favours|is|has|cares|trusts|distrusts|acts|finds|enjoys|believes|thinks|"
    r"integrates|speaks|learns|plans|builds)\b",
    re.IGNORECASE,
)
# System state and job content — not facts about the person.
_SYSTEM_RE = re.compile(
    r"\b(?:cron|charter|quota|rate limit|daily limit|api key|key is|403|429|tool[- ]calls?|budget|allow-list|"
    r"memory file|state file|this run|each run|per run|the run's|the drive|rung|pipeline)\b",
    re.IGNORECASE,
)


def observation_rejection(text: str) -> str:
    """Why an automatic profile fact is not about the person ("" = keep).

    A user fact names the user as its subject; text about jobs, charters,
    keys, quotas or runs is system state or task content."""
    t = (text or "").strip()
    named = re.match(r"^(?!(?:Pernix|The|This|That|Each|It|Findings)\b)[A-Z][a-z]+\s+[a-z]+s\b", t)
    if not (_PERSON_RE.search(t[:200]) or _LEADING_VERB_RE.match(t) or named):
        return "no person as subject"
    if len(_SYSTEM_RE.findall(t)) >= 2:
        return "describes a system or job, not the person"
    return ""


def _profile_vectors(conn):
    """(file, epoch, content, epoch_int, unit vector) for live profile entries."""
    import numpy as np

    from core.llm.embeddings import active_model

    rows = conn.execute(
        "SELECT f.file_name, f.epoch, f.content, v.dim, v.vec FROM memory_fts f "
        "JOIN vectors v ON v.file_name = f.file_name AND v.epoch = CAST(f.epoch AS TEXT) "
        "WHERE f.entry_type = 'profile' AND v.model = ?",
        (active_model(),),
    ).fetchall()
    out = []
    for fn, ep, content, dim, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32, count=dim)
        n = float(np.linalg.norm(v)) or 1.0
        out.append((fn, str(ep), content, int(ep), v / n))
    return out


def _nearest_profile(store, content: str) -> float:
    """Highest cosine between `content` and any live profile entry (0 when
    embeddings are unavailable — the lexical gate still runs)."""
    try:
        import numpy as np

        from core.llm.embeddings import embed_document_sync, enabled

        if not enabled():
            return 0.0
        # Document-side embed: stored facts are embedded as documents, and
        # the gate compares like with like (a query prefix shifts cosines).
        q = embed_document_sync(content, "user.profile", "profile")
        if not q:
            return 0.0
        q = np.asarray(q, dtype=np.float32)
        q = q / (float(np.linalg.norm(q)) or 1.0)
        conn = store._connect()
        try:
            rows = _profile_vectors(conn)
        finally:
            conn.close()
        rows = [r for r in rows if r[4].shape == q.shape]
        if not rows:
            return 0.0
        return float(max(float(r[4] @ q) for r in rows))
    except Exception as e:
        logger.debug("profile similarity check failed: %s", e)
        return 0.0


def _written_today(store) -> int:
    start = int(time.time()) - _DAY
    conn = store._connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE entry_type = 'profile' AND source IN ('reflect', 'snooze') "
            "AND CAST(epoch AS INTEGER) >= ?",
            (start,),
        ).fetchone()[0]
    finally:
        conn.close()


def save_fact(store, content: str, *, source: str, tags: str, weight: str = "normal") -> str:
    """Write one automatic profile fact through the gate. Returns the store's
    result string, or a "Skipped: …" reason. Never raises."""
    content = (content or "").strip()
    if len(content) < 20:
        return "Skipped: too short"
    why = observation_rejection(content)
    if why:
        return f"Skipped: {why}"
    try:
        if _written_today(store) >= max(0, int(settings.profile_daily_cap)):
            return "Skipped: daily profile cap reached"
        cos = _nearest_profile(store, content)
        if cos >= float(settings.profile_gate_cosine):
            return f"Skipped: same fact already stored (cosine {cos:.2f})"
        return store.add_or_supersede_entry(
            content, file_name="user.profile", entry_type="profile", tags=tags, weight=weight, source=source
        )
    except Exception as e:
        logger.debug("profile save failed: %s", e)
        return f"Skipped: {type(e).__name__}"


# ── consolidation ───────────────────────────────────────────────────────

MERGE_PROMPT = """These {n} memory entries are facts about the same user. Several say the same thing, or an
older version of it. Rewrite them as the SMALLEST set of self-contained facts that keeps every distinct
piece of information.

Rules:
- Same fact said twice -> one fact.
- Facts that conflict -> keep the NEWEST (the date is shown) and drop the older one.
- Do not invent, generalize or add anything that is not in the entries.
- Each fact must be readable on its own, one or two sentences.
- Return no more facts than entries given.

Entries (newest first):
{entries}

Output JSON only: {{"facts": ["...", "..."]}} /no_think"""


def find_cluster(conn, *, threshold: float, max_size: int = 8) -> list[tuple[str, str, str, int]]:
    """One cluster of near-duplicate profile entries (largest first), or []."""
    import numpy as np

    rows = _profile_vectors(conn)
    if len(rows) < 2:
        return []
    dims = {r[4].shape for r in rows}
    if len(dims) != 1:
        rows = [r for r in rows if r[4].shape == rows[0][4].shape]
    mat = np.vstack([r[4] for r in rows])
    sims = mat @ mat.T
    np.fill_diagonal(sims, 0.0)
    counts = (sims >= threshold).sum(axis=1)
    seed = int(np.argmax(counts))
    if counts[seed] == 0:
        return []
    members = [seed] + [int(j) for j in np.argsort(-sims[seed]) if sims[seed, j] >= threshold]
    members = list(dict.fromkeys(members))[:max_size]
    return [(rows[i][0], rows[i][1], rows[i][2], rows[i][3]) for i in members]


def _cluster_key(cluster) -> str:
    import hashlib

    return hashlib.sha1("|".join(sorted(f"{fn}@{ep}" for fn, ep, _, _ in cluster)).encode()).hexdigest()[:16]


async def consolidate_step(store, db=None, is_cancelled=lambda: False) -> int:
    """Merge one cluster of near-duplicate profile facts. Returns entries
    retired (0 when nothing to do or the merge was rejected)."""
    import asyncio

    from core.llm.jsonx import extract_json
    from core.memory.sweeps import archive_entries

    if not settings.profile_consolidation_enabled:
        return 0

    def _cluster():
        conn = store._connect()
        try:
            return find_cluster(conn, threshold=float(settings.profile_cluster_cosine))
        finally:
            conn.close()

    cluster = await asyncio.to_thread(_cluster)
    if len(cluster) < 2 or is_cancelled():
        return 0
    # A cluster the model would not reduce is not retried for a week.
    skip_key = "profile_merge_skip:" + _cluster_key(cluster)
    if db is not None:
        until = db.get_snooze_state(skip_key)
        if until and float(until) > time.time():
            return 0
    cluster.sort(key=lambda r: r[3], reverse=True)
    lines = [
        f"{i + 1}. ({time.strftime('%Y-%m-%d', time.gmtime(ep))}) {c[:400]}" for i, (_, _, c, ep) in enumerate(cluster)
    ]
    from core.llm.client import get_llm_client

    try:
        resp = await get_llm_client().chat(
            messages=[{"role": "user", "content": MERGE_PROMPT.format(n=len(cluster), entries="\n".join(lines))}],
            model=settings.background_model or settings.llm_model,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.warning("Profile consolidation LLM call failed: %s", e)
        return 0
    data = extract_json(resp.content or "")
    facts = [str(f).strip() for f in (data.get("facts") if isinstance(data, dict) else None) or [] if str(f).strip()]
    # Reject anything that is not a strict reduction of the input.
    if not facts or len(facts) >= len(cluster) or any(len(f) < 20 or len(f) > 600 for f in facts):
        logger.info("Profile consolidation rejected (%d entries -> %d facts)", len(cluster), len(facts))
        if db is not None:
            db.set_snooze_state(skip_key, str(time.time() + 7 * _DAY))
        return 0
    if is_cancelled():
        return 0

    def _apply() -> int:
        newest = cluster[0][3]
        written: set[int] = set()
        for i, fact in enumerate(facts):
            res = store.add_entry(
                fact,
                file_name="user.profile",
                entry_type="profile",
                tags=f"user,profile,consolidated,{time.strftime('%Y-%m-%d')}",
                source="consolidate",
                epoch=newest + i,
                skip_dedup=True,
            )
            if not res.startswith("Saved"):
                logger.warning("Profile consolidation write failed: %s", res)
                return 0
            try:
                written.add(int(res.rsplit("epoch=", 1)[1].rstrip(")")))
            except (IndexError, ValueError):
                written.add(newest + i)
        by_file: dict[str, set[int]] = {}
        for fn, ep, _, _ in cluster:
            by_file.setdefault(fn, set()).add(int(ep))
        # Never archive what was just written (add_entry bumps on collision).
        for fn, eps in by_file.items():
            eps = eps - written if fn == "user.profile" else eps
            if eps:
                archive_entries(store, fn, eps)
        return len(cluster)

    retired = await asyncio.to_thread(_apply)
    if retired:
        logger.info("Profile consolidation: %d entries -> %d facts", retired, len(facts))
    return retired


def profile_count(store) -> int:
    conn = store._connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM memory_fts WHERE entry_type = 'profile'").fetchone()[0]
    finally:
        conn.close()


def dumps(obj) -> str:
    return json.dumps(obj)
