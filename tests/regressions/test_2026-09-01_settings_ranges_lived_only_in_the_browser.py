"""The accepted range for a setting existed only as hand-written UI copy.

Every numeric control in the Settings modal carried its own `min`/`max`,
typed into `static/js/components/modals/settings.js` beside the label. The
server enforces a different dict — `_SETTING_BOUNDS` in
`api/routers/health.py` — and the two drifted:

* `snooze_max_cycle_seconds` advertised min 60; the server accepts 30.
* `scout_timeout`, `compaction_threshold` and the whole `rlm_*` family
  advertised no range at all.

Out-of-range values are dropped silently by `update_settings()`, so a save
that lost a field looked exactly like a save that worked. `GET
/api/settings/schema` publishes the bounds the server actually enforces plus
the dataclass defaults, so the browser can stop guessing.

This test pins the contract the UI reads: every bounded key is present with
the server's own min/max, and every default is the dataclass default.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


async def _schema():
    from api.routers import health

    app = FastAPI()
    app.include_router(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings/schema")
    assert resp.status_code == 200
    return resp.json()


async def test_every_bounded_key_publishes_the_servers_own_range():
    from api.routers.health import _SETTING_BOUNDS

    fields = (await _schema())["fields"]
    for key, (lo, hi) in _SETTING_BOUNDS.items():
        assert key in fields, f"{key} is enforced but not published"
        assert fields[key]["min"] == lo, key
        assert fields[key]["max"] == hi, key


async def test_defaults_match_the_settings_dataclass():
    from dataclasses import fields as dc_fields

    from api.routers.health import _REDACTED_FIELDS
    from config import Settings

    published = (await _schema())["fields"]
    defaults = Settings()
    for f in dc_fields(defaults):
        if f.name in _REDACTED_FIELDS:
            continue
        assert f.name in published, f"{f.name} missing from the schema"
        assert published[f.name]["default"] == getattr(defaults, f.name), f.name


async def test_record_shape_is_uniform():
    keys = {"key", "type", "default", "min", "max", "step", "unit", "restart", "locked", "risk", "hint"}
    fields = (await _schema())["fields"]
    assert fields, "schema published nothing"
    for key, rec in fields.items():
        assert set(rec) == keys, key
        assert rec["key"] == key
        assert rec["type"] in {"bool", "int", "float", "str", "list", "dict"}


async def test_redacted_fields_are_not_published():
    from api.routers.health import _REDACTED_FIELDS

    fields = (await _schema())["fields"]
    assert not (_REDACTED_FIELDS & set(fields))


async def test_locked_and_restart_flags_track_the_server_sets():
    from api.routers.health import _LOCKED_FIELDS, _REDACTED_FIELDS, _RESTART_FIELDS

    fields = (await _schema())["fields"]
    for key in _LOCKED_FIELDS - _REDACTED_FIELDS:
        assert fields[key]["locked"] is True, key
    for key in _RESTART_FIELDS - _REDACTED_FIELDS:
        assert fields[key]["restart"] is True, key
    assert fields["scout_timeout"]["locked"] is False
    assert fields["scout_timeout"]["restart"] is False


async def test_units_and_steps_are_usable_for_a_hint_line():
    fields = (await _schema())["fields"]
    # The drift examples from the audit, now answerable from the server.
    assert (fields["snooze_max_cycle_seconds"]["min"], fields["snooze_max_cycle_seconds"]["max"]) == (30, 7200)
    assert fields["snooze_max_cycle_seconds"]["unit"] == "seconds"
    assert fields["scout_timeout"]["unit"] == "seconds"
    assert fields["compaction_threshold"]["type"] == "float"
    assert fields["compaction_threshold"]["unit"] == "fraction"
    assert fields["compaction_threshold"]["step"] == 0.05
    assert fields["max_tool_rounds"]["step"] == 1
    assert fields["context_budget"]["unit"] == "tokens"
    assert fields["rlm_max_subcalls"]["min"] == 5
