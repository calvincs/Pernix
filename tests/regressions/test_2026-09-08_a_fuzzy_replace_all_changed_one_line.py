"""Harness review 3.2.1 (H14), 2026-09-08: `replace_all=True` changed exactly
one occurrence whenever the match was fuzzy, and said "Edited" anyway.

`_apply_edit` returned on the first candidate a strategy yielded. The
whitespace-normalized and indentation-flexible strategies did loop when
replace_all was set, but every candidate was built from the untouched
original lines, so each one carried exactly one replacement — measured: three
occurrences, three candidates, replacements per candidate [1, 1, 1]. The
block-anchor strategy ignored replace_all outright and yielded its best
candidate. Only exact replacement honoured the flag.

What came back was `Edited <path> [fuzzy: whitespace-normalized]
(+1/-1 lines)` with no replacement count anywhere in it — the +N/-N is a diff
line count — and through multiedit, `Applied 1/1 edits`, which reads as a
finished batch. A refactor across three call sites migrated one of them and
reported done.

There was a second shape the report did not name: the fuzzy strategies only
run when exact matching finds nothing, so a file holding one byte-identical
occurrence and two whitespace variants had the exact one replaced and the
variants silently skipped.

The strategies now find every non-overlapping match and apply them
cumulatively, the count is in the result string, block-anchor says that it
matches one block by definition, and anything ambiguous — a fuzzy match with
more than one candidate and replace_all off, two equally good anchor blocks —
is refused with the file untouched rather than resolved by guessing.
"""

import pytest

from core.tools.builtin.file_edit import _apply_edit, file_edit, multiedit


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(root))
    return root


WHITESPACE_BODY = (
    "def a():\n"
    "    result  =  compute( x , 1 )\n"
    "def b():\n"
    "    result  =  compute( x , 1 )\n"
    "def c():\n"
    "    result  =  compute( x , 1 )\n"
)
WHITESPACE_OLD = "    result = compute( x , 1 )"
WHITESPACE_NEW = "    result = compute( x , 2 )"

INDENT_BODY = (
    "HEADER\n"
    "OLDCALL(1)\n"
    "OLDCALL(2)\n"
    "MID\n"
    "OLDCALL(1)\n"
    "OLDCALL(2)\n"
    "TAIL\n"
    "OLDCALL(1)\n"
    "OLDCALL(2)\n"
)
INDENT_OLD = "    OLDCALL(1)\n    OLDCALL(2)"
INDENT_NEW = "    NEWCALL(1)\n    NEWCALL(2)"


# ── the field case: three call sites, one migrated ──────────────────────────


def test_a_whitespace_fuzzy_replace_all_changes_every_occurrence(ws):
    """Verbatim from the reproduction: 3 occurrences, 1 replaced."""
    p = ws / "ws.py"
    p.write_text(WHITESPACE_BODY)

    result = file_edit("ws.py", WHITESPACE_OLD, WHITESPACE_NEW, replace_all=True)

    assert result.startswith("Edited"), result
    assert "[fuzzy: whitespace-normalized]" in result
    assert "(3 replacements," in result, f"the count must be in the result: {result.splitlines()[0]}"
    body = p.read_text()
    assert body.count("compute( x , 2 )") == 3
    assert "compute( x , 1 )" not in body


def test_an_indentation_fuzzy_replace_all_changes_every_block(ws):
    p = ws / "ind.py"
    p.write_text(INDENT_BODY)

    result = file_edit("ind.py", INDENT_OLD, INDENT_NEW, replace_all=True)

    assert result.startswith("Edited"), result
    assert "[fuzzy: indentation-flexible]" in result
    assert "(3 replacements," in result
    body = p.read_text()
    assert body.count("NEWCALL(1)") == 3
    assert body.count("NEWCALL(2)") == 3
    assert "OLDCALL" not in body
    # The blocks sat at indent 0 and old_string was indented — the strategy's
    # reindentation must still land them at the original indent, not old's.
    assert "\nNEWCALL(1)\nNEWCALL(2)\nMID\n" in body


def test_an_exact_replace_all_still_reports_its_count(ws):
    p = ws / "exact.py"
    p.write_text("a = compute(x, 1)\nb = compute(x, 1)\nc = compute(x, 1)\n")

    result = file_edit("exact.py", "compute(x, 1)", "compute(x, 2)", replace_all=True)

    assert "(3 replacements," in result
    assert "[fuzzy:" not in result
    assert p.read_text().count("compute(x, 2)") == 3


def test_a_single_replacement_says_one_replacement(ws):
    p = ws / "one.py"
    p.write_text("alpha\nbeta\ngamma\n")
    result = file_edit("one.py", "beta", "BETA")
    assert "(1 replacement," in result
    assert p.read_text() == "alpha\nBETA\ngamma\n"


# ── no offset drift, no overlapping matches ─────────────────────────────────


def test_repeated_adjacent_blocks_are_replaced_without_overlap(ws):
    """Four identical lines and a two-line old_string: a scanner that steps by
    one line finds three matches, two of which overlap, and splicing them all
    corrupts the file. There are two non-overlapping matches here."""
    p = ws / "rep.txt"
    p.write_text("A  1\nA  1\nA  1\nA  1\n")

    result = file_edit("rep.txt", "A 1\nA 1", "B 2\nB 2", replace_all=True)

    assert result.startswith("Edited"), result
    assert "(2 replacements," in result
    assert p.read_text() == "B 2\nB 2\nB 2\nB 2\n"


def test_a_replacement_of_a_different_length_does_not_drift(ws):
    """The second and third matches must be found in the original coordinates
    even though the first replacement is shorter than what it replaced."""
    p = ws / "drift.py"
    p.write_text("KEEP\nX  1\nX  2\nKEEP\nX  1\nX  2\nKEEP\nX  1\nX  2\nEND\n")

    result = file_edit("drift.py", "X 1\nX 2", "Y", replace_all=True)

    assert "(3 replacements," in result
    assert p.read_text() == "KEEP\nY\nKEEP\nY\nKEEP\nY\nEND\n"


# ── block-anchor is one block by definition, and says so ────────────────────


def test_block_anchor_says_it_matched_a_single_block(ws):
    p = ws / "blk.py"
    p.write_text("def foo():\n    x = 1\n    y = 2\n    return x\nend\n")

    result = file_edit(
        "blk.py",
        "def foo():\n    x = 1\n    y = 999\n    return x",
        "def foo():\n    x = 10\n    return x",
        replace_all=True,
    )

    assert result.startswith("Edited"), result
    assert "[fuzzy: block-anchor-fuzzy]" in result
    assert "(1 replacement," in result
    assert "single block" in result, "replace_all must not be honoured in silence"
    assert "x = 10" in p.read_text()


def test_two_equally_good_anchor_blocks_are_refused(ws):
    """Verbatim from the reproduction: two identical blocks, replace_all=True,
    one replaced. Picking the first is a guess, so refuse."""
    body = "def one():\n    x = 1\n    y = 2\n    return x\n" "SEP\n" "def one():\n    x = 1\n    y = 2\n    return x\n"
    p = ws / "twoblk.py"
    p.write_text(body)

    result = file_edit(
        "twoblk.py",
        "def one():\n    x = 1\n    y = 999\n    return x",
        "def one():\n    ZZ = 42\n    return x",
        replace_all=True,
    )

    assert result.startswith("Error"), result
    assert "ambiguous" in result.lower()
    assert p.read_text() == body, "an ambiguous match must leave the file alone"


# ── ambiguity without replace_all is exposed, not resolved by guessing ──────


def test_several_fuzzy_matches_without_replace_all_are_refused(ws):
    p = ws / "ws.py"
    p.write_text(WHITESPACE_BODY)

    result = file_edit("ws.py", WHITESPACE_OLD, WHITESPACE_NEW)

    assert result.startswith("Error"), result
    assert "ambiguous" in result.lower()
    assert "replace_all" in result, "the caller needs to be told the way through"
    assert p.read_text() == WHITESPACE_BODY


def test_a_single_fuzzy_match_without_replace_all_still_applies(ws):
    p = ws / "one_ws.py"
    p.write_text("alpha\nif  (x   ==   1):\n    pass\nomega\n")

    result = file_edit("one_ws.py", "if (x == 1):\n    pass", "if (x == 2):\n    pass")

    assert result.startswith("Edited"), result
    assert "[fuzzy:" in result
    assert "(1 replacement," in result
    assert "x == 2" in p.read_text()


def test_several_exact_matches_without_replace_all_still_take_the_first(ws):
    """Unchanged on purpose: an exact match is precise, so first-match is a
    predictable contract. It is only the fuzzy near-match that is a guess."""
    p = ws / "exact.py"
    p.write_text("foo\nfoo\nfoo\n")
    result = file_edit("exact.py", "foo", "bar")
    assert "(1 replacement," in result
    assert p.read_text() == "bar\nfoo\nfoo\n"


# ── the mixed file: an exact match hides the variants beside it ─────────────


def test_variants_left_behind_by_an_exact_replace_all_are_surfaced(ws):
    """The fuzzy cascade only runs when exact matching finds nothing, so one
    byte-identical occurrence is enough to make the whitespace variants
    invisible. Replace what matched, then say what did not."""
    body = "value = compute(x, 1)\nvalue  =  compute(x, 1)\nvalue = compute(x, 1)\n"
    p = ws / "mixed.py"
    p.write_text(body)

    result = file_edit("mixed.py", "value = compute(x, 1)", "value = compute(x, 2)", replace_all=True)

    assert result.startswith("Edited"), result
    assert "(2 replacements," in result
    assert "near-match" in result, f"the skipped variant must be named: {result.splitlines()[0]}"
    assert "line 2" in result
    after = p.read_text()
    assert after.count("compute(x, 2)") == 2
    assert "value  =  compute(x, 1)" in after, "the variant is reported, not edited by guesswork"


def test_a_clean_exact_replace_all_says_nothing_about_variants(ws):
    p = ws / "clean.py"
    p.write_text("v = 1\nv = 1\nother\n")
    result = file_edit("clean.py", "v = 1", "v = 2", replace_all=True)
    assert "(2 replacements," in result
    assert "near-match" not in result


# ── multiedit reports real counts and rolls the whole batch back ────────────


def test_multiedit_reports_the_replacements_it_made(ws):
    p = ws / "ws.py"
    p.write_text(WHITESPACE_BODY)

    result = multiedit("ws.py", [{"old_string": WHITESPACE_OLD, "new_string": WHITESPACE_NEW, "replace_all": True}])

    assert result.startswith("Applied 1/1 edits"), result
    assert "3 replacements" in result, "'Applied 1/1 edits' alone read as a finished batch"
    assert p.read_text().count("compute( x , 2 )") == 3


def test_multiedit_rolls_back_the_whole_batch_on_an_ambiguous_edit(ws):
    body = "HEAD\n" + WHITESPACE_BODY
    p = ws / "batch.py"
    p.write_text(body)

    result = multiedit(
        "batch.py",
        [
            {"old_string": "HEAD", "new_string": "TAIL"},
            {"old_string": WHITESPACE_OLD, "new_string": WHITESPACE_NEW},
        ],
    )

    assert result.startswith("Error"), result
    assert "Edit 2" in result
    assert "no file changes written" in result
    assert p.read_text() == body, "the first edit must not survive the batch's failure"


def test_multiedit_counts_across_several_edits(ws):
    p = ws / "multi.py"
    p.write_text("a = 1\na = 1\nb = 2\nb = 2\nb = 2\n")

    result = multiedit(
        "multi.py",
        [
            {"old_string": "a = 1", "new_string": "a = 9", "replace_all": True},
            {"old_string": "b = 2", "new_string": "b = 8", "replace_all": True},
        ],
    )

    assert result.startswith("Applied 2/2 edits"), result
    assert "5 replacements" in result
    assert p.read_text() == "a = 9\na = 9\nb = 8\nb = 8\nb = 8\n"


# ── the count the strategies themselves report ──────────────────────────────


def test_apply_edit_reports_the_count_it_made():
    outcome = _apply_edit(WHITESPACE_BODY, WHITESPACE_OLD, WHITESPACE_NEW, True)
    assert outcome.strategy == "whitespace-normalized"
    assert outcome.count == 3, "one candidate per match, each carrying one replacement, was the bug"
    assert outcome.content.count("compute( x , 2 )") == 3


def test_apply_edit_reports_ambiguity_instead_of_content():
    outcome = _apply_edit(WHITESPACE_BODY, WHITESPACE_OLD, WHITESPACE_NEW, False)
    assert outcome.content is None
    assert outcome.count == 0
    assert outcome.error is not None


def test_fuzzy_matching_was_not_broadened_to_raise_the_count(ws):
    """The floor that keeps block-anchor from editing an unrelated block is
    unchanged: still a refusal to match, not a wider net."""
    body = "def foo():\n    completely_unrelated_body_line\n    return x\n"
    p = ws / "prog.py"
    p.write_text(body)

    result = file_edit(
        "prog.py",
        "def foo():\n    x = 1\n    y = 2\n    z = 3\n    return x",
        "def foo():\n    changed\n    return x",
        replace_all=True,
    )

    assert "Error" in result
    assert "not found" in result
    assert p.read_text() == body
