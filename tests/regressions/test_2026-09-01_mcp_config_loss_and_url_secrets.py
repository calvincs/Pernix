"""Two ways mcp_servers.json betrayed the user.

1. Entries that failed validation were skipped at load with a warning, but
   the file is rewritten from the manager's LIVE connections — which only
   contain what parsed. So one hand-edited typo was silently deleted by the
   next add/remove/toggle from the UI, while the user was still fixing it.
2. The literal-secret check covered headers and env but not the url, and
   hosted MCP endpoints commonly carry the credential in the query string.
   Such a url was accepted, stored in plaintext, and echoed back by
   GET /api/mcp/servers.
"""

import json

import pytest

from core.extensions.mcp.config import (
    load_server_configs,
    parse_server_entry,
    save_server_configs,
    skipped_server_entries,
)


def _write(path, servers):
    path.write_text(json.dumps({"mcpServers": servers}))


def test_an_unparseable_entry_survives_a_rewrite(tmp_path):
    path = tmp_path / "mcp_servers.json"
    _write(
        path,
        {
            "good": {"url": "https://example.com/sse"},
            "typo": {"url": "https://example.com/sse", "safety": "yolo"},
        },
    )
    loaded = load_server_configs(path)
    assert set(loaded) == {"good"}, "the bad entry is skipped, as before"
    assert set(skipped_server_entries(path)) == {"typo"}

    save_server_configs(loaded, path)
    on_disk = json.loads(path.read_text())["mcpServers"]
    assert set(on_disk) == {"good", "typo"}, "the entry being fixed by hand must not be deleted"
    assert on_disk["typo"]["safety"] == "yolo", "verbatim, so the user's edit is still there"


def test_a_re_added_name_wins_over_its_skipped_version(tmp_path):
    path = tmp_path / "mcp_servers.json"
    _write(path, {"srv": {"url": "https://example.com/sse", "safety": "yolo"}})
    load_server_configs(path)

    fixed = {"srv": parse_server_entry("srv", {"url": "https://example.com/sse"})}
    save_server_configs(fixed, path)
    on_disk = json.loads(path.read_text())["mcpServers"]
    assert "safety" not in on_disk["srv"] or on_disk["srv"].get("safety") != "yolo"


def test_a_clean_file_records_nothing_to_carry_forward(tmp_path):
    path = tmp_path / "mcp_servers.json"
    _write(path, {"good": {"url": "https://example.com/sse"}})
    load_server_configs(path)
    assert skipped_server_entries(path) == {}


def test_a_credential_in_the_url_query_is_refused():
    with pytest.raises(ValueError, match="literal secret"):
        parse_server_entry("hosted", {"url": "https://mcp.vendor.com/sse?api_key=sk-live-abcdefghijklmnop"})


def test_a_placeholder_url_is_still_allowed():
    cfg = parse_server_entry("hosted", {"url": "https://mcp.vendor.com/sse?api_key=${VENDOR_KEY}"})
    assert "${VENDOR_KEY}" in cfg.url


def test_an_ordinary_url_is_unaffected():
    cfg = parse_server_entry("plain", {"url": "https://mcp.vendor.com/sse?workspace=team-a"})
    assert cfg.url.endswith("workspace=team-a")
