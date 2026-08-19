"""Classifier contract for scripts/dream_fix_audit.py.

REMOVE needs BOTH signals — a named market quantity and point-in-time
evidence. Either alone is a keeper: the VIX-source recommendation conflict
names VIX but records no level, and it is exactly the class dream is for.
"""

from scripts.dream_fix_audit import ORIGIN_REF_RE, classify


def test_market_snapshot_corrections_are_remove():
    assert (
        classify(
            "STALE-INFO CORRECTION (human-approved via adaptive review, dream:ab12cd34ef56): "
            "The market context and specific price points (S&P 500 at 7,259.22 and NASDAQ at "
            "25,326.13 as of May 6) are stale and contradicted by newer operational data."
        )
        == "REMOVE"
    )
    assert classify("Oil prices rallied above $114/barrel, contradicting Brent at $105.") == "REMOVE"
    assert classify("The S&P 500 hit an all-time high while consumer sentiment dropped.") == "REMOVE"


def test_operational_corrections_are_keep():
    # Names a quantity, but the finding is about SOURCES — no snapshot evidence.
    assert (
        classify(
            "CONTRADICTION RESOLVED: The VIX data source recommendation conflicts between "
            "FRED and Yahoo Finance/CBOE across entries."
        )
        == "KEEP"
    )
    assert (
        classify(
            "The daily brief job schedule described in older entries is contradicted by newer "
            "entries listing 'daily-morning-brief' as the active job."
        )
        == "KEEP"
    )
    assert (
        classify(
            "One record mandates using Friday session memory for weekend briefs while another "
            "recommends live web search."
        )
        == "KEEP"
    )


def test_dollar_amount_without_market_quantity_is_keep():
    # Snapshot evidence alone (a price in a cost estimate) is not a market snapshot.
    assert classify("Build cost estimates conflict: $8-25M versus $2M-$5M plus hardware.") == "KEEP"


def test_origin_ref_extraction():
    m = ORIGIN_REF_RE.search("(human-approved via adaptive review, dream:0a1b2c3d4e5f): text")
    assert m and m.group(1) == "0a1b2c3d4e5f"
    assert ORIGIN_REF_RE.search("no ref in this note") is None


def test_djia_alias_is_covered():
    # Found live: a DJIA closing-value note fell to KEEP because the regex
    # knew "Dow" but not "DJIA".
    assert classify("Inconsistent DJIA closing values, with one entry citing 51,561.93 on June 4.") == "REMOVE"


def test_exact_dupes_collect_keeps_oldest(monkeypatch):
    """Grouping contract for scripts/memory_exact_dupes.py: same-file,
    whitespace-normalized identity; oldest copy survives."""
    import core.memory.format as fmt
    from scripts.memory_exact_dupes import collect

    class _Entry:
        def __init__(self, epoch, content, source="distill"):
            self.epoch, self.content, self.source = epoch, content, source

    class _File:
        name = "market.snapshots"

    class _Store:
        def list_files(self):
            return [_File()]

        def read_file(self, name):
            return "nonempty"

    entries = [
        _Entry(100, "Dow at 49,499.27  (-0.31%)"),
        _Entry(101, "Dow at 49,499.27 (-0.31%)"),  # whitespace-only difference
        _Entry(102, "S&P closed at 7,230.12"),
    ]
    monkeypatch.setattr(fmt, "parse_entries_from_markdown", lambda name, md: entries)

    groups = collect(_Store())
    assert len(groups) == 1
    assert groups[0]["keep_epoch"] == 100
    assert groups[0]["delete_epochs"] == [101]
