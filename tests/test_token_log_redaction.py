"""Access tokens must never reach the access log.

QR codes and shared links used to carry `/?token=<secret>`, and uvicorn writes
the path verbatim: `GET /?token=<a working credential> HTTP/1.1 200`. On the
Docker deployment stdout IS the log, so `docker compose logs` handed a live
credential to anyone who could read it — and to wherever those logs are
shipped. Redaction after the fact is no remedy for a token already written
down, so the primary fix moved onboarding to the URL fragment (never sent to
the server at all); this filter covers the links already in circulation.
"""

import logging

from run import _redact_tokens, _TokenRedactFilter

SECRET = "TfYfzlph236XwnjuRX7QkjvNo6h26beqHoUByp7zYeA"


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped the way uvicorn.access emits one."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("192.168.1.246:62477", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_token_value_is_removed_from_the_rendered_line():
    record = _access_record(f"/?token={SECRET}")
    assert _TokenRedactFilter().filter(record) is True
    rendered = record.getMessage()
    assert SECRET not in rendered
    assert "token=REDACTED" in rendered


def test_filter_keeps_the_record():
    """Redaction must not double as suppression — the request still gets logged."""
    record = _access_record(f"/?token={SECRET}")
    assert _TokenRedactFilter().filter(record) is True
    assert "GET" in record.getMessage()
    assert "200" in record.getMessage()


def test_surrounding_query_params_survive():
    record = _access_record(f"/api/x?a=1&token={SECRET}&b=2")
    _TokenRedactFilter().filter(record)
    rendered = record.getMessage()
    assert "a=1" in rendered and "b=2" in rendered
    assert SECRET not in rendered


def test_record_shape_is_preserved_for_downstream_filters():
    """_PollFilter matches on the path, so args must stay a 5-tuple of the
    same shape — rewriting record.msg instead would break it."""
    record = _access_record(f"/?token={SECRET}")
    _TokenRedactFilter().filter(record)
    assert isinstance(record.args, tuple)
    assert len(record.args) == 5
    assert record.args[1] == "GET" and record.args[4] == 200


def test_unrelated_paths_are_untouched():
    for path in ("/api/health", "/static/js/app.js", "/?tokenish=keepme"):
        record = _access_record(path)
        _TokenRedactFilter().filter(record)
        assert record.args[2] == path


def test_case_insensitive_and_ampersand_forms():
    assert _redact_tokens("/?TOKEN=abc") == "/?TOKEN=REDACTED"
    assert _redact_tokens("/?a=1&token=abc") == "/?a=1&token=REDACTED"
    assert _redact_tokens("/?access_token=abc") == "/?access_token=REDACTED"


def test_fragment_form_needs_no_redaction():
    """The fragment never reaches the server, so it never reaches a log.

    Asserted so the primary fix is not silently reverted to `?token=` on the
    assumption that this filter makes the query form safe. It does not: it only
    limits the damage for links handed out before the change.
    """
    record = _access_record("/")
    _TokenRedactFilter().filter(record)
    assert record.args[2] == "/"


def test_message_only_records_are_also_scrubbed():
    """Not every logger passes args; a pre-rendered message must still be safe."""
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"redirecting to /?token={SECRET}",
        args=None,
        exc_info=None,
    )
    _TokenRedactFilter().filter(record)
    assert SECRET not in record.getMessage()


# ---------------------------------------------------------------------------
# The primary fix: the token never reaches the server in the first place.
# ---------------------------------------------------------------------------


def test_onboarding_url_puts_the_token_in_the_fragment(monkeypatch):
    """`/#token=`, never `/?token=`.

    This is the whole point: a fragment is not transmitted, so there is nothing
    for a log — ours, a proxy's, or a Referer header — to capture. Redaction is
    only a backstop for links handed out before this change.
    """
    from api.routers.health import build_access_url

    monkeypatch.setattr("config.settings.auth_token", SECRET)
    monkeypatch.setattr("config.settings.cors_origins", ["https://box.example.com:8090"])

    url = build_access_url()
    assert url == f"https://box.example.com:8090/#token={SECRET}"
    assert "?token=" not in url
    # Everything before the '#' is what the server actually receives.
    assert SECRET not in url.split("#", 1)[0]


def test_onboarding_url_falls_back_to_lan_ip_and_still_uses_a_fragment(monkeypatch):
    from api.routers.health import build_access_url

    monkeypatch.setattr("config.settings.auth_token", SECRET)
    monkeypatch.setattr("config.settings.cors_origins", [])

    url = build_access_url()
    assert "/#token=" in url
    assert SECRET not in url.split("#", 1)[0]
