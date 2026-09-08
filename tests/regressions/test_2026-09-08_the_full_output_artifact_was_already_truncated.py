"""Regression — 2026-09-08, harness audit 3.2.1 H15.

The "full output" handle was never the full output. bash read back at most
5 MiB of each capture, glued stdout to stderr, ran the repeated-line collapse
over the result and only THEN handed the string to truncate_output — which can
persist nothing but the string it is given. Measured at the audit baseline: a
command emitting 11,400,026 chars with a unique end marker produced a 50,408
char preview with no marker, a 5,242,937 char "full output" artifact with no
marker, and 6,157,089 chars gone with no record that they had ever existed.
The artifact header then read "91,982 lines / 5,242,937 chars" — quoting the
CLIPPED size as the source total, so nothing in the transcript said the source
was bigger. Collapse made it worse in the other direction: 2000 identical
WARNING lines reached the artifact as 4.

The fetch side had no artifact at all, and a body cut short by the whole-
exchange deadline was labelled "[truncated at 100000 bytes]" — a 3 KB result
claiming it had hit the 100 KB cap.

Pinned here: acquisition is separated from presentation. Raw evidence is
streamed to a durable artifact BEFORE collapse or truncation, the caps stay
exactly where they were, and every clipped result states what it captured,
what the source held (or that the total is unknown), why it stopped, and
where the raw bytes are. RLM inherits that status instead of silently
upgrading a partial input to whole-source analysis.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import core.extensions.web as web
from core.tools.builtin import core_tools
from core.tools.truncation import MAX_OUTPUT, read_artifact_meta

MARKER = "ZZ_END_OF_STREAM_MARKER_ZZ"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace whose tool-output dir is inside the test's tmp_path."""
    work = tmp_path / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(work))
    monkeypatch.setattr("core.tools.truncation.TOOL_OUTPUT_DIR", tmp_path / "tool_output")
    monkeypatch.setattr("config.settings.shell_security_mode", "off", raising=False)
    return work


def _artifacts(tmp_path: Path, prefix: str) -> list[Path]:
    out = tmp_path / "tool_output"
    if not out.exists():
        return []
    return sorted(p for p in out.iterdir() if p.name.startswith(prefix) and p.suffix == ".txt")


# ---------------------------------------------------------------------------
# bash — the capture is evidence, the return value is a rendering of it
# ---------------------------------------------------------------------------


def test_an_end_marker_past_the_preview_cap_survives_in_the_raw_artifact(ws, tmp_path):
    """The decisive line of a build lands last. It must be recoverable."""
    # ~600 KB of output: twelve times the 50 KB preview cap, with the marker
    # only at the very end.
    cmd = f"for i in $(seq 1 8000); do echo \"line $i {'x' * 60}\"; done; echo {MARKER}"
    result = core_tools.bash(cmd)

    assert MARKER not in result, "precondition: the marker is past the preview cap"
    arts = _artifacts(tmp_path, "bash_stdout")
    assert len(arts) == 1, f"expected one raw stdout artifact, got {arts}"
    raw = arts[0].read_text()
    assert raw.rstrip("\n").endswith(MARKER), "the raw artifact must hold the end of the stream"
    assert str(arts[0]) in result, "the preview must name the artifact holding the evidence"

    meta = read_artifact_meta(arts[0])
    assert meta["source_complete"] is True
    assert meta["unit"] == "bytes"
    assert meta["captured"] == meta["source_total"] == arts[0].stat().st_size


def test_repeated_line_collapse_never_reaches_the_persisted_evidence(ws, tmp_path):
    """Collapse is a readability transform. The artifact is not a rendering."""
    cmd = f"for i in $(seq 1 2000); do echo 'WARNING: deprecated'; done; echo {MARKER}"
    result = core_tools.bash(cmd)

    assert "identical lines omitted" in result, "precondition: collapse ran on the preview"
    assert result.count("WARNING: deprecated") < 10

    arts = _artifacts(tmp_path, "bash_stdout")
    assert len(arts) == 1
    raw = arts[0].read_text()
    assert raw.count("WARNING: deprecated") == 2000, "raw evidence must survive the transform"
    assert MARKER in raw


def test_stdout_and_stderr_are_captured_and_reported_independently(ws, tmp_path):
    """One stream's cap must not be spent describing the other's content."""
    cmd = (
        f"for i in $(seq 1 3000); do echo \"out $i {'o' * 40}\"; done; "
        f"echo OUT_{MARKER}; "
        f"for i in $(seq 1 3000); do echo \"err $i {'e' * 40}\" >&2; done; "
        f"echo ERR_{MARKER} >&2"
    )
    core_tools.bash(cmd)

    out_arts = _artifacts(tmp_path, "bash_stdout")
    err_arts = _artifacts(tmp_path, "bash_stderr")
    assert len(out_arts) == 1 and len(err_arts) == 1
    out_raw, err_raw = out_arts[0].read_text(), err_arts[0].read_text()
    assert f"OUT_{MARKER}" in out_raw and f"ERR_{MARKER}" not in out_raw
    assert f"ERR_{MARKER}" in err_raw and f"OUT_{MARKER}" not in err_raw


def test_a_capture_past_the_artifact_cap_reports_the_true_source_total(ws, tmp_path, monkeypatch):
    """The cap stays. What changes is that the clipped size stops posing as
    the source total: the audit's artifact header said 5,242,937 chars for a
    source that held 11,400,026."""
    monkeypatch.setattr(core_tools, "_CAPTURE_ARTIFACT_CAP", 40_000)
    monkeypatch.setattr(core_tools, "_CAPTURE_READ_CAP", 40_000)
    cmd = f"for i in $(seq 1 4000); do echo \"line $i {'x' * 60}\"; done; echo {MARKER}"
    result = core_tools.bash(cmd)

    arts = _artifacts(tmp_path, "bash_stdout")
    assert len(arts) == 1
    meta = read_artifact_meta(arts[0])
    assert meta["source_complete"] is False
    assert meta["captured"] == 40_000
    assert meta["source_total"] > 250_000, meta
    assert arts[0].stat().st_size == 40_000, "the disk cap is still enforced"
    assert MARKER not in arts[0].read_text()

    # The honest statement, in the result the model reads.
    assert "source_complete=false" in result
    assert f"{meta['source_total']:,}" in result
    assert "bytes" in result
    assert str(meta["truncation_reason"]) in result and meta["truncation_reason"]


def test_a_complete_small_command_says_nothing_new(ws, tmp_path):
    """Completeness reporting is for clipped acquisitions; an ordinary command
    keeps its ordinary output."""
    result = core_tools.bash("echo hello")
    assert result.endswith("hello\n") or result.rstrip().endswith("hello")
    assert "source_complete" not in result
    assert _artifacts(tmp_path, "bash_") == []


# ---------------------------------------------------------------------------
# browse_web — the pre-extraction clip
# ---------------------------------------------------------------------------


def test_browser_html_clipped_before_extraction_is_reported_as_incomplete(ws, tmp_path):
    """5 MB of DOM is cut before trafilatura ever sees it, so the extracted
    markdown is a reading of a prefix. Nothing said so."""
    html = "<html><body>" + ("<p>filler</p>" * 100) + f"<p>{MARKER}</p></body></html>"
    clipped, meta = web._clip_html_for_extraction(html, url="http://x/", cap=200)

    assert len(clipped) == 200
    assert MARKER not in clipped
    assert meta["source_complete"] is False
    assert meta["source_total"] == len(html)
    assert meta["unit"] == "chars"
    note = web.acquisition_note(meta)
    assert "source_complete=false" in note and f"{len(html):,}" in note

    whole, meta2 = web._clip_html_for_extraction(html, url="http://x/", cap=10_000_000)
    assert whole == html and meta2["source_complete"] is True
    assert web.acquisition_note(meta2) == ""


# ---------------------------------------------------------------------------
# http_get — a partial fetch, its reason, and its unknown total
# ---------------------------------------------------------------------------


class _Drip(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    chunk_count = 40
    chunk_delay = 0.25

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for i in range(self.chunk_count):
                body = (f"chunk {i:04d} " + "d" * 64 + "\n").encode()
                self.wfile.write(b"%x\r\n" % len(body) + body + b"\r\n")
                self.wfile.flush()
                time.sleep(self.chunk_delay)
            tail = (MARKER + "\n").encode()
            self.wfile.write(b"%x\r\n" % len(tail) + tail + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def drip_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/drip"
    srv.shutdown()
    srv.server_close()


def test_a_slow_partial_fetch_says_deadline_not_cap_and_owns_an_unknown_total(ws, tmp_path, monkeypatch, drip_server):
    """A chunked response has no content-length, so the source total genuinely
    is unknown — and the audit's 3 KB deadline-truncated body claimed it had
    hit the 100 KB cap."""
    monkeypatch.setattr("config.settings.localhost_mode", True, raising=False)
    monkeypatch.setattr("config.settings.candor_enabled", False, raising=False)
    monkeypatch.setattr(web, "_HTTP_GET_DEADLINE_S", 2.0)

    out = web.http_get(drip_server, force=True)
    body = out[0] if isinstance(out, tuple) else out

    assert "chunk 0000" in body, "what was acquired is returned"
    assert MARKER not in body, "precondition: the deadline cut the body short"
    assert "deadline" in body.lower()
    assert f"truncated at {int(web.settings.max_fetch_size)} bytes" not in body
    assert "source_complete=false" in body
    assert "unknown" in body.lower(), "no content-length means no honest total"

    arts = _artifacts(tmp_path, "http_get")
    assert len(arts) == 1, f"a partial fetch needs an artifact handle, got {arts}"
    meta = read_artifact_meta(arts[0])
    assert meta["source_complete"] is False
    assert meta["source_total"] is None
    assert meta["captured"] == arts[0].stat().st_size
    assert arts[0].read_text().startswith("chunk 0000")


# ---------------------------------------------------------------------------
# artifact → RLM
# ---------------------------------------------------------------------------


def test_rlm_inherits_the_completeness_of_the_artifact_it_is_handed(ws, tmp_path):
    """Handing a partial artifact to rlm_process must not turn a prefix into
    a whole-source answer."""
    from core.extensions.rlm import source_completeness_notice

    partial = tmp_path / "tool_output" / "bash_stdout_partial.txt"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("first half only\n")
    partial.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "source": "stdout of a shell command",
                "unit": "bytes",
                "captured": 16,
                "source_total": 9_000_000,
                "source_complete": False,
                "truncation_reason": "5 MiB process-output cap",
            }
        )
    )
    notice = source_completeness_notice([partial])
    assert "INCOMPLETE SOURCE" in notice
    assert "9,000,000" in notice and "16" in notice
    assert "5 MiB process-output cap" in notice

    whole = tmp_path / "tool_output" / "bash_stdout_whole.txt"
    whole.write_text("all of it\n")
    assert source_completeness_notice([whole]) == ""


def test_the_truncation_header_never_states_a_clipped_size_as_the_total(ws, tmp_path):
    """truncate_output can only persist the string it is handed; the header it
    writes must therefore describe THAT string and defer to the acquisition
    metadata for what the source held."""
    from core.tools.truncation import truncate_output

    presented = "\n".join(f"line {i}" for i in range(20_000))
    assert len(presented) > MAX_OUTPUT
    sources = [
        {
            "source": "stdout of `make`",
            "unit": "bytes",
            "captured": 5_242_880,
            "source_total": 11_400_026,
            "source_complete": False,
            "truncation_reason": "5 MiB process-output cap",
            "artifact": "data/.tool_output/bash_stdout_1.txt",
        }
    ]
    preview, meta = truncate_output(presented, "bash", sources=sources)
    assert "11,400,026" in preview, "the source total must be stated"
    assert "source_complete=false" in preview
    assert "data/.tool_output/bash_stdout_1.txt" in preview
    assert meta["truncated"] is True
    assert meta["source_complete"] is False
