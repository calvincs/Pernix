"""Guard that every Settings field is either persistable or in _NO_PERSIST.

Prevents the footgun where a new private/runtime field is added to Settings
without being added to _NO_PERSIST, making it writable via POST /api/settings
(or, conversely, accidentally persisted to disk).

If this test fails: decide whether the new field is user-configurable state
(leave it out of _NO_PERSIST and document in SPEC) or runtime-only
(add it to _NO_PERSIST in config.py).
"""

from dataclasses import fields

from config import _NO_PERSIST, Settings

# Fields that legitimately exist on Settings but should not appear in the
# persisted settings.json or be settable via POST /api/settings.
# This is the authoritative list; _NO_PERSIST in config.py must match.
_EXPECTED_NO_PERSIST = {
    "db_path",
    "host",
    "port",
    "workspace_dir",
    "memory_dir",
    "skills_dir",
    "workflows_dir",
    "candor_store_dir",
    "telos_dir",
}


def test_no_persist_matches_expected():
    """_NO_PERSIST in config.py must match the expected set.

    If this fails, you're likely adding a new runtime-only field and forgot
    to update both places, OR promoting a runtime field to persisted state.
    """
    assert _NO_PERSIST == _EXPECTED_NO_PERSIST, (
        f"_NO_PERSIST diverged from expected set. "
        f"Added: {_NO_PERSIST - _EXPECTED_NO_PERSIST}. "
        f"Removed: {_EXPECTED_NO_PERSIST - _NO_PERSIST}."
    )


def test_no_persist_fields_exist_on_settings():
    """Every name in _NO_PERSIST must be a real Settings field."""
    field_names = {f.name for f in fields(Settings)}
    missing = _NO_PERSIST - field_names
    assert not missing, f"_NO_PERSIST references nonexistent fields: {missing}"


def test_private_fields_are_in_no_persist():
    """Any field starting with underscore must be in _NO_PERSIST.

    Private/runtime fields should never be exposed via the settings API or
    written to disk. This guards the convention.
    """
    private_fields = {f.name for f in fields(Settings) if f.name.startswith("_")}
    leaked = private_fields - _NO_PERSIST
    assert not leaked, f"Private fields not in _NO_PERSIST (would be writable via API): {leaked}"


def test_callable_fields_are_in_no_persist():
    """Callable-typed fields can't be JSON-serialized; they must be excluded."""
    import typing
    from types import FunctionType

    for f in fields(Settings):
        t = f.type
        origin = typing.get_origin(t) if not isinstance(t, str) else None
        if t is FunctionType or origin in (typing.Callable,):
            assert f.name in _NO_PERSIST, f"Callable field {f.name!r} is not in _NO_PERSIST"


# ---------------------------------------------------------------------------
# write_env_var — round-tripping API keys to .env
# ---------------------------------------------------------------------------


def test_write_env_var_roundtrip(tmp_path):
    """API keys set via /api/settings/apikey must persist to .env so they
    survive a restart. Without this, the user sets TAVILY_API_KEY in the UI,
    sees search work in the current session, restarts the server, and finds
    search broken again with no diagnostic. (2026-04-27 harness audit.)
    """
    from config import write_env_var

    p = tmp_path / ".env"
    p.write_text("# my keys\nFOO=bar\nTAVILY_API_KEY=old\nOTHER=keep\n")

    write_env_var("TAVILY_API_KEY", "new-secret", p)

    out = p.read_text()
    assert "FOO=bar" in out, "unrelated key dropped"
    assert "# my keys" in out, "comment dropped"
    assert "OTHER=keep" in out, "tail key dropped"
    assert "TAVILY_API_KEY=new-secret" in out, "key not updated"
    assert "TAVILY_API_KEY=old" not in out, "old value not replaced"


def test_write_env_var_appends_new_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("EXISTING=1\n")
    from config import write_env_var

    write_env_var("OPENROUTER_API_KEY", "or-key", p)

    out = p.read_text()
    assert "EXISTING=1" in out
    assert "OPENROUTER_API_KEY=or-key" in out


def test_write_env_var_removes_on_empty(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\nTAVILY_API_KEY=secret\n")
    from config import write_env_var

    write_env_var("TAVILY_API_KEY", None, p)

    out = p.read_text()
    assert "TAVILY_API_KEY" not in out
    assert "FOO=bar" in out


def test_write_env_var_creates_missing_file(tmp_path):
    p = tmp_path / ".env"  # doesn't exist
    from config import write_env_var

    write_env_var("FOO", "bar", p)

    assert p.read_text() == "FOO=bar\n"
