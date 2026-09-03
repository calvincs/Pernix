"""Every memory mutation rewrote the markdown in place.

`_write_locked` did seek/truncate/write with no temp file, no rename and
no fsync. A kill or power loss between the truncate and the completed
write left the file empty or partial — and the next health_check(fix=True)
rebuilt the index from the wreckage, turning a recoverable interruption
into permanent loss. Markdown is the source of truth; the index is
derived, so the file must never be observable half-written.

Writes now go to a sibling temp file, fsync, then os.replace.
"""

import pytest

from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def _seed(store, name="pernix.lessons"):
    store.add_entry("the original finding worth keeping", file_name=name)
    return store._dir / f"{name}.md"


def test_a_failure_mid_write_leaves_the_original_file_intact(store, monkeypatch):
    md = _seed(store)
    before = md.read_text(encoding="utf-8")
    assert "original finding" in before

    real_replace = __import__("os").replace

    def boom(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr("core.memory.store.os.replace", boom)
    with pytest.raises(OSError):
        store._write_locked(md, "TOTALLY DIFFERENT CONTENT")

    monkeypatch.setattr("core.memory.store.os.replace", real_replace)
    assert md.read_text(encoding="utf-8") == before, "the original must survive a failed write"
    assert not list(md.parent.glob("*.tmp")), "the temp file must not be left behind"


def test_a_successful_write_replaces_the_content_and_leaves_no_temp(store):
    md = _seed(store)
    store._write_locked(md, "replacement body\n")
    assert md.read_text(encoding="utf-8") == "replacement body\n"
    assert not list(md.parent.glob("*.tmp"))


def test_on_written_runs_only_after_the_content_landed(store):
    md = _seed(store)
    seen = {}

    def after():
        seen["content"] = md.read_text(encoding="utf-8")

    store._write_locked(md, "new body\n", on_written=after)
    assert seen["content"] == "new body\n", "the index hook must see the committed file"


def test_ordinary_entry_mutation_still_round_trips(store):
    _seed(store)
    store.add_entry("a second finding", file_name="pernix.lessons")
    raw = store.read_file("pernix.lessons")
    assert "original finding" in raw and "a second finding" in raw
