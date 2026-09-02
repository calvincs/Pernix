"""A zero in the wrong setting stopped a loop instead of degrading it.

Settings.load only coerced types, so a hand-edited settings.json could put
0 into a field the code divides by or sizes a scheduler from.
snooze_interval_ticks = 0 raised ZeroDivisionError on every maintenance
tick — swallowed by the tick handler, so snooze never ran again and the
log filled with tracebacks. openai_max_concurrent = 0 built a scheduler
that never granted a permit. Neither had an API bound either.
"""

import json

import pytest

from api.routers.health import _SETTING_BOUNDS
from config import Settings


def _write(tmp_path, monkeypatch, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr("config.SETTINGS_PATH", path)
    return Settings.load()


def test_a_zero_divisor_is_raised_to_its_floor(tmp_path, monkeypatch, caplog):
    s = _write(tmp_path, monkeypatch, {"snooze_interval_ticks": 0})
    assert s.snooze_interval_ticks == 1


def test_a_zero_concurrency_is_raised_to_its_floor(tmp_path, monkeypatch):
    s = _write(tmp_path, monkeypatch, {"openai_max_concurrent": 0, "llm_max_concurrent": 0})
    assert s.openai_max_concurrent == 1
    assert s.llm_max_concurrent == 1


def test_a_negative_value_is_clamped_too(tmp_path, monkeypatch):
    s = _write(tmp_path, monkeypatch, {"mcp_connect_timeout": -5})
    assert s.mcp_connect_timeout == 1


def test_legitimate_values_are_left_alone(tmp_path, monkeypatch):
    s = _write(tmp_path, monkeypatch, {"snooze_interval_ticks": 25, "openai_max_concurrent": 8})
    assert s.snooze_interval_ticks == 25
    assert s.openai_max_concurrent == 8


def test_llm_session_timeout_zero_still_means_unlimited(tmp_path, monkeypatch):
    """0 is a documented sentinel here, not a broken value."""
    s = _write(tmp_path, monkeypatch, {"llm_session_timeout": 0})
    assert s.llm_session_timeout == 0
    assert _SETTING_BOUNDS["llm_session_timeout"][0] == 0


@pytest.mark.parametrize(
    "name",
    [
        "openai_max_concurrent",
        "snooze_interval_ticks",
        "snooze_max_cycle_seconds",
        "cron_dispatch_timeout",
        "reflect_defer_idle_s",
        "mcp_call_timeout",
        "mcp_connect_timeout",
        "mcp_max_servers",
    ],
)
def test_the_api_refuses_to_write_these_out_of_range(name):
    assert name in _SETTING_BOUNDS, f"{name} can still be set to a breaking value over the API"
    lo, hi = _SETTING_BOUNDS[name]
    assert lo <= hi
