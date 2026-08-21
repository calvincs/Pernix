"""Pernix — Local embedding calls for semantic retrieval (adaptation plan 1f).

Two call shapes, deliberately different:

- Batch corpus embedding (embed_texts / embed_pending) is ASYNC and goes
  through the Ollama scheduler at PRIORITY_BACKGROUND — the plan's rule.
  It runs during snooze sweeps, never on the write path, so a memory write
  is never blocked on an HTTP call and background embeds can never preempt
  a live turn (the scheduler sorts background last).

- Per-query embedding (embed_query_sync) is a bounded SYNC call. Search runs
  in sync tool threads and inside code already holding locks; bridging onto
  the event loop's scheduler from there risks deadlock when the caller is
  loop-adjacent. One small embed with a hard 5s timeout is the pragmatic
  exception to the scheduler rule — on any failure the caller degrades to
  lexical search, silently correct.
"""

from __future__ import annotations

import hashlib
import logging
import time

from config import settings

logger = logging.getLogger("pernix.llm.embeddings")

# Serialize + bound query-time embeds without the async scheduler: a failed
# or slow Ollama shouldn't stack up threads each waiting 5s. When an embed
# fails, back off for a short window instead of paying the timeout per search.
_QUERY_TIMEOUT_S = 5.0
_FAILURE_BACKOFF_S = 60.0
_last_failure_at: float = 0.0

# A cold model is the one case the 5s query timeout makes WORSE: Ollama
# cancels a model load when the requesting client disconnects ("timed out
# waiting for llama-server to start: context canceled"), so a client that
# always gives up at 5s never lets the load finish and every later query is
# cold too. One detached long-timeout embed breaks that loop.
_WARM_TIMEOUT_S = 300.0
_WARM_COOLDOWN_S = 600.0
_last_warm_at: float = 0.0

# Sustained failure is an operator event, not a log line. On the live box
# every embed failed for a whole day — recall was lexical-only and the only
# trace was a WARNING per search that said "for 60s". After
# _NOTIFY_AFTER_S of continuous failure a notification lands, once per
# _NOTIFY_EVERY_S, and a success clears the episode.
_NOTIFY_AFTER_S = 1800.0
_NOTIFY_EVERY_S = 86400.0
_failing_since: float = 0.0  # monotonic; 0 = healthy
_failing_since_wall: str = ""
_last_notified_at: float = 0.0


def enabled() -> bool:
    return bool(settings.embedding_model)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _native_base() -> str:
    return settings.llm_base_url.rstrip("/").replace("/v1", "")


def _describe(err: Exception) -> str:
    """Error text with the response body when there is one. An HTTP 500's
    str() is just its status line; the body carried the actual cause
    ("vk::Device::allocateMemory: ErrorOutOfDeviceMemory") for a day while
    the log said "500"."""
    text = str(err)
    resp = getattr(err, "response", None)
    body = (getattr(resp, "text", "") or "").strip() if resp is not None else ""
    if body:
        text = f"{text} — {body[:200]}"
    return text


def _note_success() -> None:
    global _failing_since, _failing_since_wall
    _failing_since = 0.0
    _failing_since_wall = ""


def _note_failure(err: Exception) -> None:
    """Track the failure episode; notify the operator once it is sustained."""
    global _failing_since, _failing_since_wall, _last_notified_at
    now = time.monotonic()
    if not _failing_since:
        _failing_since = now
        from datetime import datetime, timezone

        _failing_since_wall = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return
    # `_last_notified_at` / `_last_warm_at` are 0.0 until first use, and
    # time.monotonic() is small on a freshly booted box — "now - 0.0 < window"
    # would silently swallow the first notice for up to a day.
    if now - _failing_since < _NOTIFY_AFTER_S or (_last_notified_at and now - _last_notified_at < _NOTIFY_EVERY_S):
        return
    _last_notified_at = now
    try:
        from db import models as db

        db.add_notification(
            title="Embeddings unavailable — memory recall is lexical-only",
            body=(
                f"Every embedding call to {_native_base()}/api/embed (model {settings.embedding_model}) "
                f"has failed since {_failing_since_wall}; latest error: {_describe(err)[:300]}. Memory recall, "
                "dedup and dream/candor semantic search run on keyword matching only until it recovers. "
                "Check the embedding server; a model that was evicted needs one request that waits out "
                "the cold load. This notice repeats at most once a day; a successful embed clears it."
            ),
            urgency="normal",
        )
    except Exception as e:  # never let observability break the search path
        logger.debug("embedding outage notification failed: %s", e)


def _warm_model_detached() -> None:
    """Fire one long-timeout embed on a daemon thread so a cold model's load
    actually completes; rate-limited so a dead server is not hammered."""
    global _last_warm_at
    now = time.monotonic()
    if _last_warm_at and now - _last_warm_at < _WARM_COOLDOWN_S:
        return
    _last_warm_at = now
    import threading

    def _warm() -> None:
        import httpx

        try:
            httpx.post(
                f"{_native_base()}/api/embed",
                json={"model": settings.embedding_model, "input": ["warm"]},
                timeout=_WARM_TIMEOUT_S,
            ).raise_for_status()
            logger.info("Embedding model warmed after a cold-load timeout")
        except Exception as e:
            logger.warning("Embedding warm-up failed: %s", e)

    threading.Thread(target=_warm, name="pernix-embed-warm", daemon=True).start()


def embed_query_sync(text: str) -> list[float] | None:
    """Embed one query string synchronously. None on any failure (caller
    degrades to lexical)."""
    global _last_failure_at
    if not enabled():
        return None
    if time.monotonic() - _last_failure_at < _FAILURE_BACKOFF_S:
        return None
    import httpx

    try:
        resp = httpx.post(
            f"{_native_base()}/api/embed",
            json={"model": settings.embedding_model, "input": [text]},
            timeout=_QUERY_TIMEOUT_S,
        )
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings", [])
        if embeddings:
            _note_success()
            return embeddings[0]
        raise ValueError("empty embeddings response")
    except Exception as e:
        _last_failure_at = time.monotonic()
        logger.warning("Query embedding failed (lexical-only for %ds): %s", int(_FAILURE_BACKOFF_S), _describe(e))
        _note_failure(e)
        if isinstance(e, httpx.TimeoutException):
            _warm_model_detached()
        return None


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch through the Ollama scheduler at background priority.
    None on failure."""
    if not enabled() or not texts:
        return None
    from core.llm.client import get_llm_client
    from core.llm.semaphore import PRIORITY_BACKGROUND

    router = get_llm_client().router
    sem = getattr(router, "_ollama_semaphore", None)
    provider = getattr(router, "_ollama", None)
    if sem is None or provider is None or not hasattr(provider, "embed"):
        return None
    # session_id must stay "" (the scheduler's background-caller contract):
    # a named pseudo-session opts into the 1800s wall-clock session budget,
    # and only SessionManager clears that stamp — for real sessions. With
    # session_id="_embeddings" the clock started at the first post-restart
    # embed and never reset, so every embed failed with LLMSessionTimeoutError
    # from 30 minutes of uptime until the next restart.
    await sem.acquire(session_id="", session_created_at=float("inf"), priority=PRIORITY_BACKGROUND)
    try:
        vectors = await provider.embed(settings.embedding_model, texts)
        _note_success()
        return vectors
    except Exception as e:
        logger.warning("Batch embedding failed: %s", _describe(e))
        _note_failure(e)
        return None
    finally:
        sem.release()


async def embed_pending(store, max_entries: int = 256) -> int:
    """Embed entries whose vectors are missing or stale. Returns count stored.

    Called from snooze's index-reconciliation activity. Bounded per cycle so
    a large backlog (first enable, model change) drains over a few cycles
    without monopolizing the box.
    """
    if not enabled():
        return 0
    pending = store.pending_embeddings(limit=max_entries)
    if not pending:
        return 0

    stored = 0
    batch_size = max(1, int(settings.embedding_batch_size))
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        vectors = await embed_texts([p["content"] for p in batch])
        if vectors is None:
            break  # Ollama unhappy — stop the sweep, retry next cycle
        rows = [(p["file_name"], p["epoch"], p["content_hash"], vec) for p, vec in zip(batch, vectors)]
        stored += store.store_embeddings(rows)
    if stored:
        logger.info(
            "Embedded %d memory entr%s (%d still pending)",
            stored,
            "y" if stored == 1 else "ies",
            max(0, len(pending) - stored),
        )
    return stored
