"""Regression — 2026-09-08, harness audit 3.2.1 H16.

Five ways a search told the agent it had looked when it had not.

1. A file over ripgrep's 1 MiB `--max-filesize` was skipped in silence, so an
   oversized file containing the symbol returned the literal "No matches
   found." — byte-identical to a genuine absence. And the per-file
   `--max-count=200` made the footer state a CAP as a COUNT: "[200 matches]"
   for a file holding 500.
2. `result.returncode not in (0, 1, 2)` whitelisted exit 2, ripgrep's error
   status. An invalid regex returned "\\n\\n[0 matches]" — an audit could
   conclude a symbol was absent because its own pattern failed to compile.
3. glob showed `matches[:300]` and reported `total - 100`. 150 files: all 150
   shown, "50 more files not shown" — an omission that did not happen. 400
   files: 300 shown, "300 more not shown" — three times the 100 that were.
   Below that, the pathlib fallback broke at 500, so the total it subtracted
   from had already saturated.
4. The shared truncation cursor advised `offset=shown_lines` after a preview
   that ended mid-line. `file_read`'s offset is 0-based, so a preview of K
   complete lines plus a partial line K resumed at line K+2: the cut line was
   skipped ENTIRELY, not merely its remainder.
5. file_read's own preview had a dead end. On a 132,899-byte 3,001-line
   minified file the default branch broke before appending anything —
   "showing first 0 lines", zero source lines, `offset=0` — and following that
   advice hit the offset/limit branch, which counted its own synthetic
   "[truncated by size]" marker as a source line and broke out of the counting
   loop, reporting "[lines 1-1 of 1]" for a 3,001-line file. The cursor never
   moved.

Pinned here: scope and caps are stated, a backend error is not an empty
result, counts are returned/omitted and say "unknown" when they are, and every
cursor advances over exactly the content it skipped.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from core.tools.builtin.core_tools import file_read
from core.tools.builtin.glob_tool import glob_search
from core.tools.builtin.grep_tool import grep
from core.tools.truncation import MAX_OUTPUT, truncate_output

_HAS_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    work = tmp_path / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(work))
    monkeypatch.setattr("core.tools.truncation.TOOL_OUTPUT_DIR", tmp_path / "tool_output")
    return work


# ---------------------------------------------------------------------------
# grep — an error is not an absence, and a cap is not a count
# ---------------------------------------------------------------------------


@needs_rg
def test_an_invalid_regex_is_an_error_not_an_empty_result(ws):
    (ws / "a.py").write_text("target\n")
    out = grep("(unclosed")
    assert "Error" in out
    assert "No matches found" not in out
    assert "[0 matches]" not in out
    assert "regex" in out.lower() or "parse" in out.lower()


def test_a_partial_backend_error_keeps_its_hits_and_says_it_was_partial(ws, monkeypatch):
    """ripgrep exits 2 when it produced results AND hit errors (an unreadable
    directory, a bad encoding). Both halves have to reach the agent."""
    done = subprocess.CompletedProcess(
        args=["rg"],
        returncode=2,
        stdout="a.py:1:target\nb.py:4:target\n",
        stderr="rg: /w/locked: Permission denied (os error 13)\n",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    out = grep("target")
    assert "a.py:1:target" in out and "b.py:4:target" in out
    assert "PARTIAL" in out
    assert "Permission denied" in out
    assert "coverage is incomplete" in out.lower() or "not complete" in out.lower()


@needs_rg
def test_more_than_200_matches_in_one_file_reports_a_floor_not_a_count(ws):
    (ws / "big.py").write_text("".join(f"target line {i}\n" for i in range(500)))
    out = grep("target")
    # 200 lines came back because of the per-file cap; the file holds 500.
    assert "at least" in out.lower()
    assert "200 matches/file" in out or "per-file cap" in out.lower()
    assert "[200 matches]" not in out, "a cap artifact must never be stated as a count"


@needs_rg
def test_a_file_over_the_size_cap_is_not_reported_as_an_absence(ws):
    (ws / "huge.log").write_text("filler\n" * 200_000 + "target_symbol\n")
    assert (ws / "huge.log").stat().st_size > 1024 * 1024
    out = grep("target_symbol")
    # Nothing matched, because the only file holding it was skipped.
    assert "No matches found" in out, "precondition: the oversized file is skipped"
    # ... but the result no longer looks like a searched-and-empty workspace.
    assert "1M" in out and "skipped" in out.lower()
    assert "unknown" in out.lower(), "skipped counts are not measured; say so"


@needs_rg
def test_401_short_hits_are_all_reachable(ws, tmp_path):
    """401 > MAX_MATCHES but the full text was under MAX_OUTPUT, so
    truncate_output returned it unpersisted, output_path was absent, and the
    "Full results saved to" pointer never rendered. The 401st hit had no
    route at all."""
    for f in range(5):
        (ws / f"f{f}.py").write_text("".join(f"needle {f}-{i}\n" for i in range(90)))
    (ws / "extra.py").write_text("needle 5-0\n" * 1)  # 5*90 + 1 = 451 hits
    out = grep("needle")

    assert "Full results saved to" in out
    path = next(line for line in out.splitlines() if "Full results saved to" in line).split(": ", 1)[1].strip()
    saved = (tmp_path / "tool_output" / path.rsplit("/", 1)[-1]).read_text()
    assert saved.count("needle") == 451, f"artifact must hold every captured hit, held {saved.count('needle')}"
    assert "451" in out and "400" in out and "51" in out, "returned/omitted counts must be exact"


def test_above_the_capture_ceiling_the_uncaptured_hits_are_named(ws, monkeypatch):
    """Above 5,000 hits the artifact held 5,000 while the footer quoted the
    true total, so the pointer promised access to results it did not have."""
    from core.tools.builtin import grep_tool

    monkeypatch.setattr(grep_tool, "MAX_CAPTURED", 50)
    done = subprocess.CompletedProcess(
        args=["rg"],
        returncode=0,
        stdout="".join(f"f.py:{i}:needle\n" for i in range(1, 121)),
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    out = grep_tool.grep("needle")
    assert "120" in out, "the true total is still stated"
    assert "50" in out, "so is what was actually captured"
    assert "70" in out, "and what was never captured at all"
    assert "capture ceiling" in out.lower()


# ---------------------------------------------------------------------------
# glob — the omitted count
# ---------------------------------------------------------------------------


def _make_files(root, n, ext=".dat"):
    for i in range(n):
        (root / f"f{i:05d}{ext}").write_text("x")


def test_glob_below_the_display_limit_invents_no_omissions(ws):
    _make_files(ws, 150)
    out = glob_search("*.dat")
    body = [line for line in out.splitlines() if line.endswith(".dat")]
    assert len(body) == 150
    assert "more files not shown" not in out
    assert "150" in out


def test_glob_above_the_display_limit_omits_exactly_what_it_omitted(ws):
    _make_files(ws, 400)
    out = glob_search("*.dat")
    body = [line for line in out.splitlines() if line.endswith(".dat")]
    assert len(body) == 300
    assert "100 not shown" in out or "100 more" in out
    assert "300 more" not in out, "the old arithmetic tripled the omissions"
    assert "400" in out


def test_glob_at_the_scan_ceiling_says_the_total_is_a_floor(ws, monkeypatch):
    """The pathlib fallback broke at 500, so `total` saturated and every count
    derived from it was wrong without saying so."""
    from core.tools.builtin import glob_tool

    monkeypatch.setattr(glob_tool, "SCAN_CEILING", 120)
    _make_files(ws, 200)
    out = glob_search("*.dat")
    body = [line for line in out.splitlines() if line.endswith(".dat")]
    assert len(body) == 120
    assert "at least" in out.lower()
    assert "unknown" in out.lower() or "ceiling" in out.lower()


# ---------------------------------------------------------------------------
# the shared truncation cursor — no gaps, no duplication, always progressing
# ---------------------------------------------------------------------------


def _follow(preview: str, source: str) -> str:
    """Walk the cursor a preview advertises and rebuild what it points at."""
    import re

    got = preview.split("---\n", 1)[1]
    while True:
        m = re.search(r'file_read\(path="([^"]+)", offset=(\d+), limit=(\d+)\)', preview)
        if not m:
            break
        path, offset, limit = m.group(1), int(m.group(2)), int(m.group(3))
        chunk = file_read(path, offset=offset, limit=limit)
        head, _, body = chunk.partition("\n")
        assert head.startswith("[lines "), head
        lines = [ln.split("\t", 1)[1] if "\t" in ln else "" for ln in body.split("\n")]
        got += "\n".join(lines) + "\n"
        if "Continue with:" not in head:
            break
        nxt = re.search(r'file_read\(path="([^"]+)", offset=(\d+), limit=(\d+)\)', head)
        assert nxt and int(nxt.group(2)) > offset, f"cursor did not advance: {head}"
        preview = head
    return got


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_the_cursor_never_skips_the_line_it_cut(ws, trailing_newline):
    """Long lines, with and without a final newline. The preview budget lands
    mid-line either way; what follows it must be the rest of that line, not
    the line after it."""
    body = "".join(f"{i:04d} " + "y" * 137 + "\n" for i in range(900))
    if not trailing_newline:
        body = body[:-1]
    assert len(body) > MAX_OUTPUT
    preview, meta = truncate_output(body, "test")
    assert meta["truncated"]

    rebuilt = _follow(preview, body)
    assert rebuilt.rstrip("\n") == body.rstrip("\n"), "gaps or duplication in the reconstructed source"


def test_the_reported_total_does_not_count_a_phantom_trailing_line(ws):
    body = "".join(f"line {i}\n" for i in range(9_000))
    preview, _ = truncate_output(body, "test")
    assert "of 9,000 lines" in preview, preview.splitlines()[0]


def test_a_single_line_wider_than_the_budget_gets_a_byte_route_not_a_line_one(ws):
    """A line offset cannot address the middle of a line. Advising one after a
    mid-line cut is what skipped content; advising `offset=0` forever is what
    made the file_read preview a dead end."""
    body = "z" * 120_000 + "\nsecond line\n"
    preview, meta = truncate_output(body, "test")
    assert "cut -c" in preview
    assert "offset=0" not in preview, "a cursor that does not move is not a cursor"
    assert "offset=1, limit=200" in preview, "the next real line still has to be reachable"


# ---------------------------------------------------------------------------
# file_read — no synthetic source lines, no dead end
# ---------------------------------------------------------------------------


def test_an_oversized_first_line_still_returns_source_and_a_moving_cursor(ws):
    """The measured dead end: 'showing first 0 lines', zero source lines, and
    advice that loops back to itself."""
    (ws / "min.js").write_text("q" * 132_000 + "\n" + "".join(f"tail {i}\n" for i in range(3_000)))
    out = file_read("min.js")

    assert "showing first 0 lines" not in out
    assert "q" * 200 in out, "some of the source has to come back"
    assert "offset=0" not in out, "following this advice re-ran the same call"
    assert "offset=1" in out, "the next line is reachable"
    assert "3,001" in out or "3001" in out, "the real line count, not 1"


def test_the_offset_branch_never_counts_its_own_marker_as_a_source_line(ws):
    """`lines.append("[truncated by size]")` then `break` froze total_lines at
    1 and put a harness string where the file's content should be."""
    (ws / "min.js").write_text("w" * 132_000 + "\n" + "".join(f"tail {i}\n" for i in range(3_000)))
    out = file_read("min.js", offset=0, limit=200)

    assert "of 1]" not in out, "a 3,001-line file reported as having 1 line"
    assert "3001" in out.replace(",", "")
    marker_lines = [ln for ln in out.split("\n") if "[truncated by size]" in ln]
    assert not any("\t" in ln for ln in marker_lines), "a synthetic marker must not be numbered as source"
    assert "offset=1" in out, "the continuation has to move past the oversized line"


def test_an_ordinary_offset_read_is_untouched(ws):
    (ws / "d.txt").write_text("".join(f"line {i}\n" for i in range(50)))
    out = file_read("d.txt", offset=10, limit=5)
    assert "[lines 11-15 of 50]" in out
    assert "line 10" in out and "line 14" in out
    assert "line 15" not in out
