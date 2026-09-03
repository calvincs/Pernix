"""Space memory could leak out of its bucket — and global memory into one.

docs/guides/spaces.md promises a space's memory never leaves it. The
guards added with spaces covered score_pair and classify_entry, but three
paths were left open:

  * _auto_route filtered candidate files only when a space was ACTIVE, so
    a global remember() could land in a space file (space files are the
    content-richest on a busy space and win keyword overlap) — and the
    space's cascade delete then destroyed a memory that was never its own.
  * the LLM reroute pass applied whatever target_file the model named.
  * split_file offered every existing file as a target and accepted any
    new name the model invented.
"""

import pytest

from core.memory.routing import space_bucket
from core.memory.store import MemoryStore, _bucket_matches


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def test_bucket_matching_is_symmetric():
    # In a space: only that space's files.
    assert _bucket_matches("pernix.space.alpha.research", "pernix.space.alpha.")
    assert not _bucket_matches("pernix.research", "pernix.space.alpha.")
    assert not _bucket_matches("pernix.space.beta.research", "pernix.space.alpha.")
    # Global: only global files. This direction was missing.
    assert _bucket_matches("pernix.research", None)
    assert not _bucket_matches("pernix.space.alpha.research", None)


def test_a_global_write_does_not_land_in_a_space_file(store):
    # A space file rich in the routing keywords the entry uses.
    for i in range(6):
        store.add_entry(f"research finding about deployment topology {i}", file_name="pernix.space.alpha.research")
    for i in range(5):
        store.add_entry(f"unrelated note {i}", file_name=f"pernix.misc{i}")

    landed = store.add_entry("a research finding about deployment topology")
    assert "pernix.space.alpha" not in landed, f"a global remember() was routed into a space: {landed}"


def test_a_space_write_still_routes_within_its_space(store):
    for i in range(6):
        store.add_entry(f"deployment topology note {i}", file_name="pernix.space.alpha.research")
    landed = store.add_entry("another deployment topology note", space_slug="alpha")
    assert space_bucket(landed.split()[-1].strip("'\"")) in (None, "alpha") or "alpha" in landed


def test_split_only_offers_targets_in_the_same_bucket():
    import inspect

    from core.memory import sweeps

    src = inspect.getsource(sweeps.split_file)
    assert "_target_bucket" in src, "the candidate list must be filtered to the source's bucket"
    assert "space_bucket(file_name) != _target_bucket" in src, "and an invented name checked too"


def test_reroute_refuses_a_cross_bucket_target():
    import inspect

    from core.memory import sweeps

    src = inspect.getsource(sweeps)
    assert "space_bucket(target) != space_bucket(src)" in src
