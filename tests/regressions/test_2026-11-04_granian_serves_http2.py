"""2026-11-04: serve with Granian (HTTP/2 over TLS); uvicorn stays as a deprecated fallback.

Uvicorn speaks HTTP/1.1 only, and Chrome allows six connections per host on
HTTP/1.1. Each Pernix tab holds up to three open streams, so two tabs filled
the pool and every other request waited (the 60 s reload of 2026-11-04).
Granian negotiates HTTP/2, which carries every stream of every tab over one
connection.

Found while switching, and fixed here:
- Granian waits for open requests before the ASGI lifespan shutdown and
  cancels what is still open after the grace. Live SSE streams are never done
  on their own, so a stop took the full 15 s and then skipped the lifespan
  shutdown entirely (no turn drain, no browser or MCP close). run.py now ends
  every stream the moment the signal arrives: stop went from 15.3 s to 0.6 s.
- /api/jobs/events forwarded the `_shutdown` wake-up as an ordinary event and
  kept looping, so under either server it held every stop for the full grace.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest

import run
from core.logging_setup import ACCESS_LOGGERS, daily_handler

ROOT = Path(__file__).resolve().parents[2]


# ── which server ─────────────────────────────────────────────────────────────


def test_auto_picks_granian_whenever_tls_is_on():
    assert run._choose_server("auto", tls=True) == "granian"


def test_auto_keeps_uvicorn_only_for_plain_http(caplog):
    with caplog.at_level(logging.WARNING, logger="pernix"):
        assert run._choose_server("auto", tls=False) == "uvicorn"
    assert "deprecated" in caplog.text, "the fallback has to say it is going away"


def test_an_explicit_choice_wins_and_uvicorn_always_warns(caplog):
    assert run._choose_server("granian", tls=False) == "granian"
    with caplog.at_level(logging.WARNING, logger="pernix"):
        assert run._choose_server("uvicorn", tls=True) == "uvicorn"
    assert "deprecated" in caplog.text


def test_granian_runs_embedded_so_dangerous_and_log_filters_stay_in_process():
    # A worker process would be a fresh interpreter wherever multiprocessing
    # spawns, losing the in-memory --dangerous flag and the redaction filters.
    src = (ROOT / "run.py").read_text()
    body = re.search(r"def _serve_granian\(.*?\n(?=def )", src, re.S).group(0)
    assert "from granian.server.embed import Server" in body
    assert "HTTPModes.auto" in body


def test_requirements_pin_granian_and_mark_uvicorn_deprecated():
    req = (ROOT / "requirements.txt").read_text()
    assert re.search(r"^granian==", req, re.M)
    assert re.search(r"^uvicorn\[standard\]==.*DEPRECATED", req, re.M)


# ── shutdown ─────────────────────────────────────────────────────────────────


def test_the_signal_ends_every_stream_before_stopping_the_server():
    src = (ROOT / "run.py").read_text()
    body = re.search(r"def _on_signal\(\).*?server\.stop\(\)", src, re.S).group(0)
    assert body.index("end_all_streams()") < body.index("server.stop()")


def test_the_lifespan_shutdown_still_ends_streams_first():
    src = (ROOT / "api" / "app.py").read_text()
    shutdown = src[src.index('logger.info("Pernix shutting down")') :]
    assert shutdown.index("end_all_streams()") < shutdown.index("close_admission()")


@pytest.mark.asyncio
async def test_the_jobs_stream_ends_on_the_shutdown_wakeup():
    from api.routers import jobs
    from core.events import get_event_bus

    resp = await jobs.job_events()
    gen = resp.body_iterator
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    for q in list(get_event_bus()._subscribers):
        q.put_nowait({"type": "_shutdown"})
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(first, timeout=2)


# ── logging ──────────────────────────────────────────────────────────────────


def _granian_record(path: str, qs: str = "", status: int = 200) -> logging.LogRecord:
    fmt = '%(addr)s - "%(method)s %(path)s %(protocol)s" %(status)d ?%(query_string)s'
    args = {
        "addr": "1.2.3.4",
        "method": "GET",
        "path": path,
        "protocol": "HTTP/2",
        "status": status,
        "query_string": qs,
    }
    return logging.LogRecord("granian.access", logging.INFO, "", 1, fmt, args, None)


def test_tokens_are_redacted_from_granian_records():
    rec = _granian_record("/", "token=SECRET123&x=1")
    assert run._TokenRedactFilter().filter(rec) is True
    line = rec.getMessage()
    assert "SECRET123" not in line
    assert "token=REDACTED&x=1" in line


def test_polls_are_quiet_in_granian_records_too():
    assert run._PollFilter().filter(_granian_record("/api/health")) is False
    assert run._PollFilter().filter(_granian_record("/api/sessions/abc123/events")) is False
    assert run._PollFilter().filter(_granian_record("/api/sessions/abc123/messages")) is True
    assert run._PollFilter().filter(_granian_record("/api/health", status=500)) is True


def test_both_servers_access_lines_land_in_access_log(tmp_path):
    assert set(ACCESS_LOGGERS) == {"uvicorn.access", "granian.access"}
    access = daily_handler(tmp_path / "access.log", access=True)
    app = daily_handler(tmp_path / "pernix.log", access=False)
    for name in ACCESS_LOGGERS:
        rec = logging.LogRecord(name, logging.INFO, "", 1, "line", (), None)
        assert access.filter(rec) and not app.filter(rec)
    other = logging.LogRecord("pernix.api", logging.INFO, "", 1, "line", (), None)
    assert app.filter(other) and not access.filter(other)


# ── TLS key ──────────────────────────────────────────────────────────────────


def test_a_traditional_key_is_converted_to_pkcs8(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    src = tmp_path / "custom.key"
    src.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    out = Path(run._pkcs8_key(str(src)))
    assert out != src
    assert out.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert out.stat().st_mode & 0o077 == 0, "a converted private key must not be group/world readable"
    # A PKCS#8 key is used as it is.
    assert run._pkcs8_key(str(out)) == str(out)
