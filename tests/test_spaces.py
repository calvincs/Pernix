"""Spaces (v33): CRUD, slug rules, membership moves, never-roll-off, purge guard."""

from __future__ import annotations

import pytest

from core import spaces as spaces_lib
from db import models as db


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


def _mk(label="Research", color="#ff8800"):
    return db.create_space(label, color, spaces_lib.slugify(label))


# ---------------------------------------------------------------------------
# Slug rules
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert spaces_lib.slugify("My Cool Space!!") == "my-cool-space"
    assert spaces_lib.slugify("alpha_beta-9") == "alpha-beta-9"


def test_slugify_rejects_unusable():
    with pytest.raises(ValueError):
        spaces_lib.slugify("!!!")


def test_slug_has_no_dots():
    # A slug must be a single dot-segment of a memory file name.
    assert "." not in spaces_lib.slugify("v2.0 Research Lab")


# ---------------------------------------------------------------------------
# CRUD + immutability
# ---------------------------------------------------------------------------


def test_space_crud_roundtrip():
    sp = _mk()
    assert db.get_space(sp["id"])["label"] == "Research"
    assert db.get_space_by_slug("research")["id"] == sp["id"]

    db.update_space(sp["id"], label="Renamed", color="#112233")
    row = db.get_space(sp["id"])
    assert row["label"] == "Renamed"
    assert row["color"] == "#112233"

    db.delete_space(sp["id"])
    assert db.get_space(sp["id"]) is None


def test_slug_is_immutable_via_update():
    sp = _mk()
    db.update_space(sp["id"], slug="hijack", label="x")
    assert db.get_space(sp["id"])["slug"] == "research"


def test_list_spaces_counts_sessions():
    sp = _mk()
    db.create_session(title="a", space_id=sp["id"])
    db.create_session(title="b", space_id=sp["id"])
    db.create_session(title="loose")
    rows = db.list_spaces()
    assert rows[0]["session_count"] == 2


# ---------------------------------------------------------------------------
# Membership: move in/out without touching recency
# ---------------------------------------------------------------------------


def test_set_session_meta_space_move_preserves_updated_at():
    sp = _mk()
    sid = db.create_session(title="s")
    before = db.get_session(sid)["updated_at"]
    db.set_session_meta(sid, space_id=sp["id"])
    row = db.get_session(sid)
    assert row["space_id"] == sp["id"]
    assert row["updated_at"] == before
    db.set_session_meta(sid, space_id=None)
    assert db.get_session(sid)["space_id"] is None
    # Omitting the argument leaves membership untouched.
    db.set_session_meta(sid, title="renamed")
    assert db.get_session(sid)["space_id"] is None


def test_detach_space_sessions():
    sp = _mk()
    db.create_session(title="a", space_id=sp["id"])
    db.create_session(title="b", space_id=sp["id"])
    assert db.detach_space_sessions(sp["id"]) == 2
    assert db.list_space_session_ids(sp["id"]) == []


# ---------------------------------------------------------------------------
# Never-roll-off: enriched listing unions space sessions past the window
# ---------------------------------------------------------------------------


def test_enriched_listing_unions_space_sessions_beyond_limit():
    sp = _mk()
    space_sid = db.create_session(title="old space work", space_id=sp["id"])
    for i in range(5):
        db.create_session(title=f"newer {i}")
    rows = db.list_sessions_enriched(limit=2)
    ids = [r["id"] for r in rows]
    assert space_sid in ids, "space session must never fall out of the sidebar payload"
    # And no duplicates when it IS inside the window.
    rows_all = db.list_sessions_enriched(limit=50)
    ids_all = [r["id"] for r in rows_all]
    assert len(ids_all) == len(set(ids_all))


# ---------------------------------------------------------------------------
# Resolution helpers + cache
# ---------------------------------------------------------------------------


def test_get_session_space_resolves_from_db_row():
    sp = _mk()
    sid = db.create_session(title="s", space_id=sp["id"])
    got = spaces_lib.get_session_space(sid)
    assert got and got["id"] == sp["id"]
    assert spaces_lib.space_slug_for_session(sid) == "research"
    assert spaces_lib.get_session_space("nonexistent") is None


def test_space_cache_invalidation():
    sp = _mk()
    assert spaces_lib.get_space(sp["id"])["label"] == "Research"
    db.update_space(sp["id"], label="Fresh")
    # Stale until invalidated — then fresh.
    spaces_lib.invalidate_space_cache()
    assert spaces_lib.get_space(sp["id"])["label"] == "Fresh"


def test_kernel_key_for_session():
    sp = _mk()
    in_space = db.create_session(title="s", space_id=sp["id"])
    loose = db.create_session(title="l")
    assert spaces_lib.kernel_key_for_session(in_space) == f"space-{sp['id']}"
    assert spaces_lib.kernel_key_for_session(loose) == loose


def test_mid_turn_check_sees_processing_member():
    sp = _mk()
    sid = db.create_session(title="s", space_id=sp["id"])
    assert db.any_space_session_mid_turn(sp["id"]) is False
    db.update_session(sid, state_v2="processing")
    assert db.any_space_session_mid_turn(sp["id"]) is True


# ---------------------------------------------------------------------------
# FTS search carries space_id through to grouped results
# ---------------------------------------------------------------------------


def test_search_messages_fts_returns_space_id():
    sp = _mk()
    sid = db.create_session(title="s", space_id=sp["id"])
    db.add_message(sid, "user", "quantum flamingo research")
    hits = db.search_messages_fts("flamingo", limit=5)
    assert hits and hits[0]["session_space_id"] == sp["id"]
