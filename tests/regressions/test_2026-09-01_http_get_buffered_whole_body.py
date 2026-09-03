"""http_get read the entire response before applying max_fetch_size.

`client.get(url)` buffers and decodes the whole body, so the 100KB cap was
applied to something already in memory: one http_get of a multi-GB file
took the container's RSS with it. httpx's timeout is also per-read, so a
server dripping a byte at a time held a tool-executor thread forever.

http_get now streams, stops at the cap, refuses an oversize
Content-Length or a non-text Content-Type up front, and bounds the whole
exchange with a wall-clock deadline.
"""

import pytest

from config import settings
from core.extensions import web


class _Resp:
    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.is_redirect = False
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.read_chunks = 0

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        for c in self._chunks:
            self.read_chunks += 1
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client_returning(resp):
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            return resp

    return _C


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(web, "_validate_url", lambda url, allow_loopback=False: url)
    monkeypatch.setattr(settings, "candor_enabled", False)
    monkeypatch.setattr(settings, "fetch_routing_enabled", False)


def test_streaming_stops_reading_once_past_the_cap(offline, monkeypatch):
    cap = int(settings.max_fetch_size)
    # Far more chunks than the cap needs; a buffering client would read all.
    resp = _Resp([b"x" * 8192 for _ in range(int(cap / 8192) + 200)])
    monkeypatch.setattr("httpx.Client", _client_returning(resp))

    out = web.http_get("https://example.com/huge.bin")
    assert "[truncated at" in out
    assert len(out) <= cap + 64
    assert resp.read_chunks < int(cap / 8192) + 200, "must stop reading, not drain the whole body"


def test_an_oversize_content_length_is_refused_before_reading(offline, monkeypatch):
    cap = int(settings.max_fetch_size)
    resp = _Resp([b"never read"], headers={"content-length": str(cap * 1000)})
    monkeypatch.setattr("httpx.Client", _client_returning(resp))

    out = web.http_get("https://example.com/huge.iso")
    assert "over the fetch cap" in out
    assert resp.read_chunks == 0


def test_a_binary_content_type_is_refused(offline, monkeypatch):
    resp = _Resp([b"\x00\x01"], headers={"content-type": "application/octet-stream"})
    monkeypatch.setattr("httpx.Client", _client_returning(resp))

    out = web.http_get("https://example.com/blob")
    assert "is not text" in out
    assert resp.read_chunks == 0


def test_ordinary_text_still_comes_back_whole(offline, monkeypatch):
    resp = _Resp([b"a normal ", b"page"], headers={"content-type": "text/html; charset=utf-8"})
    monkeypatch.setattr("httpx.Client", _client_returning(resp))
    assert web.http_get("https://example.com/") == "a normal page"


def test_json_is_treated_as_text(offline, monkeypatch):
    resp = _Resp([b'{"ok": true}'], headers={"content-type": "application/json"})
    monkeypatch.setattr("httpx.Client", _client_returning(resp))
    assert web.http_get("https://example.com/api") == '{"ok": true}'
