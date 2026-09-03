"""MCP server config store: parsing, validation, secrets, naming."""

import pytest

from core.extensions.mcp.config import (
    MCPServerConfig,
    expand_placeholders,
    load_server_configs,
    parse_server_entry,
    pernix_tool_name,
    save_server_configs,
)


def test_transport_inference_matches_pasted_configs():
    """Entries pasted from Claude Code / Cursor carry no explicit type."""
    stdio = parse_server_entry("fs", {"command": "npx", "args": ["-y", "server-filesystem"]})
    assert stdio.transport == "stdio"
    http = parse_server_entry("gh", {"url": "https://example.com/mcp"})
    assert http.transport == "http"
    # VS Code writes "streamable-http" — normalized to http.
    alt = parse_server_entry("alt", {"type": "streamable-http", "url": "https://x.example/mcp"})
    assert alt.transport == "http"


def test_validation_rejects_bad_entries():
    with pytest.raises(ValueError, match="name"):
        parse_server_entry("Bad-Name", {"url": "https://x.example"})
    with pytest.raises(ValueError, match="requires 'command'"):
        parse_server_entry("s", {"type": "stdio"})
    with pytest.raises(ValueError, match="requires 'url'"):
        parse_server_entry("s", {"type": "http"})
    with pytest.raises(ValueError, match="unknown transport"):
        parse_server_entry("s", {"type": "websocket", "url": "https://x.example"})
    with pytest.raises(ValueError, match="http"):
        parse_server_entry("s", {"url": "ftp://x.example"})
    with pytest.raises(ValueError, match="safety"):
        parse_server_entry("s", {"url": "https://x.example", "safety": "yolo"})


def test_literal_secrets_are_rejected_but_placeholders_pass():
    with pytest.raises(ValueError, match="literal secret"):
        parse_server_entry("gh", {"url": "https://x.example", "headers": {"Authorization": "Bearer ghp_" + "a" * 30}})
    cfg = parse_server_entry("gh", {"url": "https://x.example", "headers": {"Authorization": "Bearer ${GH_TOKEN}"}})
    assert cfg.headers["Authorization"] == "Bearer ${GH_TOKEN}"


def test_expand_placeholders(monkeypatch):
    monkeypatch.setenv("MCP_TEST_TOKEN", "sekret")
    assert expand_placeholders("Bearer ${MCP_TEST_TOKEN}", server="s") == "Bearer sekret"
    monkeypatch.delenv("MCP_TEST_MISSING", raising=False)
    with pytest.raises(ValueError, match="MCP_TEST_MISSING"):
        expand_placeholders("${MCP_TEST_MISSING}", server="s")


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "mcp_servers.json"
    cfgs = {
        "gh": parse_server_entry("gh", {"url": "https://x.example/mcp", "safety": "safe", "timeout": 30}),
        "fs": parse_server_entry(
            "fs",
            {"command": "npx", "args": ["-y", "p"], "env": {"A": "1"}, "tool_allowlist": ["read"]},
        ),
    }
    save_server_configs(cfgs, path)
    loaded = load_server_configs(path)
    assert set(loaded) == {"gh", "fs"}
    assert loaded["gh"].safety == "safe" and loaded["gh"].timeout == 30
    assert loaded["fs"].tool_allowlist == ["read"] and loaded["fs"].env == {"A": "1"}


def test_load_accepts_bare_map_and_skips_invalid(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text('{"good": {"url": "https://x.example"}, "BAD NAME": {"url": "https://y.example"}}')
    loaded = load_server_configs(path)
    assert set(loaded) == {"good"}


def test_pernix_tool_name_sanitizes_caps_and_collides_deterministically():
    assert pernix_tool_name("gh", "createIssue") == "mcp_gh_createissue"
    assert pernix_tool_name("gh", "repos/list!") == "mcp_gh_repos_list"
    long = pernix_tool_name("gh", "x" * 100)
    assert len(long) <= 64
    # Same inputs → same name across refreshes (hash suffix is stable).
    assert long == pernix_tool_name("gh", "x" * 100)
    taken = {"mcp_gh_echo"}
    collided = pernix_tool_name("gh", "echo", taken)
    assert collided != "mcp_gh_echo" and collided.startswith("mcp_gh_echo_")


def test_to_dict_omits_defaults():
    cfg = MCPServerConfig(name="s", transport="http", url="https://x.example")
    d = cfg.to_dict()
    assert "enabled" not in d and "safety" not in d and "timeout" not in d
    assert d == {"type": "http", "url": "https://x.example"}
