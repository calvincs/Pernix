"""Ollama reasoning mode (settings.ollama_think / ollama_think_background).

`think` was a hardcoded False on both native paths, so a reasoning-tuned
model ran with its reasoning suppressed and no way to say otherwise. It is
now per role, resolved by which settings key names the model — with Primary
winning when the two roles are the same model, because a switch you turned
on should do something.
"""

import pytest

from core.llm.providers.ollama import OllamaProvider


@pytest.fixture
def provider():
    return OllamaProvider()


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr("config.settings.llm_model", "primary:27b")
    monkeypatch.setattr("config.settings.background_model", "small:9b")
    monkeypatch.setattr("config.settings.ollama_think", False)
    monkeypatch.setattr("config.settings.ollama_think_background", False)


def test_off_by_default_for_both_roles(provider):
    assert provider._think_enabled("primary:27b") is False
    assert provider._think_enabled("small:9b") is False


def test_primary_only(provider, monkeypatch):
    monkeypatch.setattr("config.settings.ollama_think", True)

    assert provider._think_enabled("primary:27b") is True
    assert provider._think_enabled("small:9b") is False, "background must not inherit the primary switch"


def test_background_only(provider, monkeypatch):
    monkeypatch.setattr("config.settings.ollama_think_background", True)

    assert provider._think_enabled("small:9b") is True
    assert provider._think_enabled("primary:27b") is False


def test_same_model_for_both_roles_follows_primary(provider, monkeypatch):
    """The roles are indistinguishable here — the switch must still do something."""
    monkeypatch.setattr("config.settings.background_model", "primary:27b")
    monkeypatch.setattr("config.settings.ollama_think", True)

    assert provider._think_enabled("primary:27b") is True


def test_backup_model_follows_primary(provider, monkeypatch):
    monkeypatch.setattr("config.settings.ollama_think", True)

    assert provider._think_enabled("backup:70b") is True


async def test_payload_carries_the_resolved_flag(provider, monkeypatch):
    """Both native paths, not just the one that happened to be read."""
    monkeypatch.setattr("config.settings.ollama_think", True)
    monkeypatch.setattr("config.settings.context_auto", False)  # no num_ctx lookup
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "hi"}, "done": True}

    class _Client:
        async def post(self, url, json=None, **kw):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(provider, "_get_client", lambda: _Client())

    await provider.chat([{"role": "user", "content": "hi"}], model="primary:27b")
    assert sent["think"] is True

    monkeypatch.setattr("config.settings.ollama_think", False)
    await provider.chat([{"role": "user", "content": "hi"}], model="primary:27b")
    assert sent["think"] is False
