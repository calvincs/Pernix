"""Regression: a model pulled after startup wedged every session.

Shipped defect (box.ventibean.com, 2026-08-14): `qwen3.8:27b` was pulled onto
the Ollama host after the container booted, so the model registry — populated
once at startup — never learned it. derive_model_budget() is a catalog lookup
with no network, so it returned None, and the agent fell back to the manual
settings.context_budget (16,384). That is smaller than the fixed cost of a
turn on that box (system 6,046 + tools 5,848 + 2,000 margin + 4,000 history
floor + 1,024 output), so compile_context raised ContextBudgetError on every
turn until the registry was refreshed by hand. GET /api/models showed the
model at 262,144 ctx the whole time, because that endpoint queries providers
live and never writes back to the registry.

Fix, two parts:
  - ensure_model_known() refreshes the registry once (rate limited per model)
    when the active model is missing from it, so the real window is used.
  - When the fallback is taken anyway, say so: a one-shot warning per model,
    and the ContextBudgetError names the manual fallback as the source
    instead of implying the model's own window is that small.
"""

from types import SimpleNamespace

import pytest

from core.llm.registry import ModelRegistry
from core.llm.types import ModelInfo


def _stub_client(monkeypatch, models, *, on_refresh=None):
    """A get_llm_client() stub whose repopulate mutates the catalog.

    Both entry points are counted separately: the lazy path must use
    populate_registry() (keeps each provider's per-model metadata cache) and
    never refresh_registry(), which clears those caches and turns learning
    one new model into an /api/show for every model on the host.
    """
    registry = ModelRegistry()
    registry._models = dict(models)
    registry._populated = True
    calls = {"populate": 0, "refresh": 0}

    async def populate_registry():
        calls["populate"] += 1
        if on_refresh is not None:
            on_refresh(registry)

    async def refresh_registry():
        calls["refresh"] += 1
        if on_refresh is not None:
            on_refresh(registry)

    stub = SimpleNamespace(
        router=SimpleNamespace(registry=registry),
        populate_registry=populate_registry,
        refresh_registry=refresh_registry,
    )
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: stub)
    return registry, calls


@pytest.fixture(autouse=True)
def _clean_budget_module_state():
    """The warn/cooldown caches are module-level — isolate each test."""
    from core.llm import budget

    budget._warned_unknown.clear()
    budget._refresh_next_allowed.clear()
    yield
    budget._warned_unknown.clear()
    budget._refresh_next_allowed.clear()


async def test_model_pulled_after_startup_is_picked_up_by_a_refresh(monkeypatch):
    from core.llm.budget import derive_model_budget, ensure_model_known

    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.context_budget", 16_384)

    def pull_it(registry):
        registry._models["qwen3.8:27b"] = ModelInfo(id="qwen3.8:27b", provider="ollama", context_length=262_144)

    _, calls = _stub_client(
        monkeypatch, {"old:7b": ModelInfo(id="old:7b", provider="ollama", context_length=8_192)}, on_refresh=pull_it
    )

    # Before: the registry has never heard of it → manual fallback territory.
    assert derive_model_budget("qwen3.8:27b") is None

    assert await ensure_model_known("qwen3.8:27b") is True
    assert derive_model_budget("qwen3.8:27b") == int(262_144 * 0.9)
    # Via populate, which keeps the per-model metadata caches: re-learning
    # one model must not re-fetch /api/show for every model on the host.
    assert (calls["populate"], calls["refresh"]) == (1, 0)


async def test_known_model_does_not_trigger_a_refresh(monkeypatch):
    """The happy path must stay a pure catalog lookup — no provider traffic."""
    from core.llm.budget import ensure_model_known

    monkeypatch.setattr("config.settings.context_auto", True)
    _, calls = _stub_client(
        monkeypatch, {"local:9b": ModelInfo(id="local:9b", provider="ollama", context_length=65_536)}
    )

    assert await ensure_model_known("local:9b") is True
    assert calls["populate"] == 0


async def test_unknown_model_refreshes_once_then_backs_off(monkeypatch):
    """A typo'd name must not re-list every provider once per turn.

    The status-bar poll calls this too, so a name that survives one
    repopulate backs off to the long interval rather than the short one.
    """
    from core.llm import budget
    from core.llm.budget import ensure_model_known

    monkeypatch.setattr("config.settings.context_auto", True)
    _, calls = _stub_client(monkeypatch, {})

    assert await ensure_model_known("nope:1b") is False
    assert await ensure_model_known("nope:1b") is False
    assert await ensure_model_known("nope:1b") is False
    assert calls["populate"] == 1

    import time as _time

    assert budget._refresh_next_allowed["nope:1b"] - _time.monotonic() > budget._REFRESH_COOLDOWN_S


async def test_unpopulated_registry_is_left_alone(monkeypatch):
    """Startup population hasn't run — that is not this path's job to fix."""
    from core.llm.budget import ensure_model_known

    monkeypatch.setattr("config.settings.context_auto", True)
    registry, calls = _stub_client(monkeypatch, {})
    registry._populated = False

    assert await ensure_model_known("anything") is False
    assert calls["populate"] == 0


def test_unknown_model_warns_once_about_the_manual_fallback(monkeypatch, caplog):
    from core.llm.budget import derive_model_budget

    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.context_budget", 16_384)
    _stub_client(monkeypatch, {})

    with caplog.at_level("WARNING", logger="pernix.llm.budget"):
        assert derive_model_budget("ghost:27b") is None
        assert derive_model_budget("ghost:27b") is None

    warnings = [r for r in caplog.records if "not in the model registry" in r.getMessage()]
    assert len(warnings) == 1, "the per-round derivation must not spam the log"
    assert "16384" in warnings[0].getMessage()


def test_budget_error_names_the_manual_fallback(monkeypatch):
    """The error used to read as if the model itself had a 16k window."""
    from core.context.compiler import ContextBudgetError, compile_context
    from db import models as db

    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.context_budget", 16_384)
    _stub_client(monkeypatch, {})  # registry knows nothing → fallback in force

    sid = db.create_session(title="Wedged")
    db.add_message(sid, "user", "hi")

    with pytest.raises(ContextBudgetError) as exc:
        compile_context(
            sid,
            tool_schemas=[{"name": f"tool_{i}", "description": "alpha beta gamma " * 60} for i in range(40)],
            context_budget=16_384,
            max_output_tokens=32_000,
            model_name="qwen3.8:27b",
        )

    msg = str(exc.value)
    assert "qwen3.8:27b" in msg
    assert "unknown to the model registry" in msg
    assert "settings.context_budget" in msg
