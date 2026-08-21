"""Pernix — local CPU embedding fallback (fastembed / ONNX).

Used only while the remote embedding server is degraded (see
core/llm/embeddings.py): a small, well-known model that runs on the CPU of
the box itself, pulled once into data/models/fastembed. Vectors it produces
are stored under a distinct model name ("local:<model>") so they never mix
with the remote model's space — search reads whichever model is active.

`fastembed` is an optional dependency: when it is not importable the
fallback is simply unavailable and the remote-only behaviour is unchanged.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.llm.local_embed")

_lock = threading.Lock()
_model = None
_model_name: str = ""
_unavailable_logged = False


def available() -> bool:
    """True when a fallback model is configured and fastembed is importable."""
    global _unavailable_logged
    if not settings.embedding_fallback_model:
        return False
    try:
        import fastembed  # noqa: F401
    except ImportError:
        if not _unavailable_logged:
            _unavailable_logged = True
            logger.warning(
                "embedding_fallback_model is set (%s) but fastembed is not installed — "
                "no local fallback while the remote embedding server is down",
                settings.embedding_fallback_model,
            )
        return False
    return True


def cache_dir() -> Path:
    return Path(settings.memory_dir).resolve().parent / "models" / "fastembed"


def embed(texts: list[str], model_name: str | None = None) -> list[list[float]]:
    """Embed on the CPU. Loads (and on first use downloads) the model under a
    lock; fastembed's model object is not documented thread-safe."""
    global _model, _model_name
    name = model_name or settings.embedding_fallback_model
    if not texts:
        return []
    with _lock:
        if _model is None or _model_name != name:
            from fastembed import TextEmbedding

            cache = cache_dir()
            cache.mkdir(parents=True, exist_ok=True)
            logger.info("Loading local embedding model %s (cache %s)", name, cache)
            _model = TextEmbedding(model_name=name, cache_dir=str(cache))
            _model_name = name
        return [[float(x) for x in vec] for vec in _model.embed(list(texts))]
