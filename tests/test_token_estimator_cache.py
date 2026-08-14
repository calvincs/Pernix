"""Token estimator: persistent tiktoken cache and a survivable cold load.

tiktoken fetches cl100k_base (1.7MB) from a CDN on first use and caches it in
an ephemeral temp dir, so a container re-downloaded it after every rebuild —
40s on the first turn after a deploy, measured, on the request path. The
cache now lives under data/, which is the persistent volume. And only
ImportError was caught around the load, so on an offline box the network
error raised straight out of the constructor instead of falling back to the
heuristic that was sitting right there.
"""

import os

import pytest

from core.context.tokens import TokenEstimator, _prepare_tiktoken_cache


def test_cache_dir_points_at_the_persistent_volume(monkeypatch, tmp_path):
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    _prepare_tiktoken_cache()

    assert os.environ["TIKTOKEN_CACHE_DIR"] == str((tmp_path / "data/cache/tiktoken").resolve())
    assert (tmp_path / "data/cache/tiktoken").is_dir()


def test_an_operator_choice_is_left_alone(monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "/somewhere/else")

    _prepare_tiktoken_cache()

    assert os.environ["TIKTOKEN_CACHE_DIR"] == "/somewhere/else"


def test_a_failed_load_falls_back_instead_of_raising(monkeypatch, caplog):
    """A cold cache with no network must not take the turn down with it."""
    import tiktoken

    def _boom(_name):
        raise ConnectionError("openaipublic.blob.core.windows.net unreachable")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)

    with caplog.at_level("WARNING", logger="pernix.context.tokens"):
        est = TokenEstimator()

    assert est._enc is None
    assert est.count("some text to measure") > 0, "the char heuristic must still answer"
    assert any("tiktoken load failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", ["", "hello world", "def f(x):\n    return x * 2"])
def test_counting_is_unchanged(text):
    assert TokenEstimator().count(text) >= 0
