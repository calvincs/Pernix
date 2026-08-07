"""Tests for config.py."""

from config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.llm_base_url == "http://localhost:11434/v1"
    assert s.scout_enabled is True
    assert s.context_budget == 192_000
    assert s.max_tool_rounds == 50
    assert len(s.shell_allowlist) > 30


def test_settings_save_load(tmp_path, monkeypatch):
    import config

    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    s = Settings()
    s.llm_model = "test-model"
    s.scout_enabled = False
    s.save()

    s2 = Settings.load()
    assert s2.llm_model == "test-model"
    assert s2.scout_enabled is False


def test_settings_no_persist_fields(tmp_path, monkeypatch):
    import json

    import config

    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    s = Settings()
    s.db_path = "/custom/path.db"
    s.save()

    data = json.loads(path.read_text())
    assert "db_path" not in data
    assert "host" not in data


def test_settings_bool_coercion(tmp_path, monkeypatch):
    import json

    import config

    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    # Write string "false" for bool field
    path.write_text(json.dumps({"scout_enabled": "false", "eval_auto": "yes"}))
    s = Settings.load()
    assert s.scout_enabled is False
    assert s.eval_auto is True
