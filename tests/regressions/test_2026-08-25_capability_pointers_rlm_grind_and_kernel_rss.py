"""Discoverability hints from the ARC-3 campaign: capabilities existed but
sat unused because nothing pointed at them at the moment of need.

- rlm_process: 1 use in 13 sessions while 12 of them ground huge obfuscated
  files through windowed reads. Signal 12 queues a one-time pointer after 5
  same-file passes; the truncation footer names rlm_process for big files.
- Kernel RSS: a 7.4GB child crashed with MemoryError and no warning. The
  kernel now appends a watermark note to cell results past the threshold.
"""

import json
import os

from core.agent import StuckDetector


def _read_call(path, offset=0):
    return {"name": "file_read", "arguments": json.dumps({"path": path, "offset": offset})}


class _Reg:
    def exists(self, name):
        return True


def test_signal12_queues_one_rlm_hint_after_five_reads(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", True)
    d = StuckDetector()
    for i in range(5):
        d.evaluate("", [_read_call("big/game.py", offset=i * 100)], {}, _Reg())
    assert len(d.pending_hints) == 1
    assert "rlm_process" in d.pending_hints[0] and "big/game.py" in d.pending_hints[0]
    # never a second hint for the same grind
    d.pending_hints.clear()
    d.evaluate("", [_read_call("big/game.py", offset=999)], {}, _Reg())
    assert d.pending_hints == []


def test_signal12_silent_when_rlm_disabled(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", False)
    d = StuckDetector()
    for i in range(6):
        d.evaluate("", [_read_call("big/game.py", offset=i)], {}, _Reg())
    assert d.pending_hints == []


def test_truncation_footer_names_rlm_when_enabled(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", True)
    from core.tools import truncation

    big = "\n".join(f"line {i}" for i in range(20000))  # well past the 50KB cap
    preview, meta = truncation.truncate_output(big, tool_id="t1")
    assert meta.get("truncated")
    assert "rlm_process" in preview


def test_kernel_rss_watermark_appends_warning(monkeypatch):
    from core.kernel import SessionKernel

    monkeypatch.setattr("config.settings.kernel_rss_warn_bytes", 1)  # anything trips

    class _Popen:
        pid = os.getpid()

    class _Repl:
        popen = _Popen()

    k = SessionKernel.__new__(SessionKernel)
    k._repl = _Repl()
    note = k._append_rss_warning(None)
    assert note is not None and "kernel memory" in note and "job_start" in note

    monkeypatch.setattr("config.settings.kernel_rss_warn_bytes", 0)
    assert k._append_rss_warning("prior") == "prior"
