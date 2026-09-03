"""Pernix — MCP server configuration store (data/mcp_servers.json).

The file uses the ecosystem-standard ``mcpServers`` shape (Claude Code,
Claude Desktop, Cursor, VS Code) so a config pasted from any of those tools
works verbatim, plus optional Pernix keys (enabled, safety, timeout,
tool_allowlist). Secrets never live in this file: ``${VAR}`` placeholders in
headers/env/args expand from process env (seeded from .env) at connect time,
and a value that *looks* like a literal secret is rejected with guidance to
move it to .env.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("pernix.ext.mcp")

MCP_SERVERS_PATH = Path("data/mcp_servers.json")

# Short so mcp_<server>_<tool> fits the 64-char provider limit with room for
# real tool names. Lowercase snake_case keeps the flat-name conventions
# (aliases, difflib hints, Tools tab sorting) working unchanged.
SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")

VALID_TRANSPORTS = frozenset({"stdio", "http", "sse"})
VALID_SAFETY = frozenset({"", "safe", "caution", "dangerous"})

# Provider function-name limit (OpenAI-compatible APIs reject longer).
MAX_TOOL_NAME_LEN = 64

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Heuristic for literal credentials in config values. Deliberately loose —
# it only fires on values with no ${VAR} placeholder, and a false positive
# costs the user one .env indirection, while a miss puts a secret in a
# plaintext JSON file that backups and transcripts can pick up.
_SECRET_LITERAL_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._\-]{16,}"  # Authorization: Bearer <token>
    r"|sk-[A-Za-z0-9_\-]{16,}"  # OpenAI-style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"  # Slack tokens
    r"|AKIA[0-9A-Z]{16})"  # AWS access key ids
)


@dataclass
class MCPServerConfig:
    """One configured MCP server."""

    name: str
    transport: str  # stdio | http | sse (sse = deprecated legacy transport)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    safety: str = ""  # "" = settings.mcp_default_safety
    timeout: int = 0  # per-call seconds; 0 = settings.mcp_call_timeout
    tool_allowlist: list[str] | None = None  # None = all tools

    def to_dict(self) -> dict:
        """Serialize for mcp_servers.json — omit empty optional fields so the
        file stays paste-shaped rather than schema-shaped."""
        out: dict = {"type": self.transport}
        if self.transport == "stdio":
            out["command"] = self.command
            if self.args:
                out["args"] = self.args
            if self.env:
                out["env"] = self.env
            if self.cwd:
                out["cwd"] = self.cwd
        else:
            out["url"] = self.url
            if self.headers:
                out["headers"] = self.headers
        if not self.enabled:
            out["enabled"] = False
        if self.safety:
            out["safety"] = self.safety
        if self.timeout:
            out["timeout"] = self.timeout
        if self.tool_allowlist is not None:
            out["tool_allowlist"] = self.tool_allowlist
        return out


def parse_server_entry(name: str, raw: dict) -> MCPServerConfig:
    """Validate one ``mcpServers`` entry. Raises ValueError with a message
    fit to show the user/agent verbatim."""
    if not isinstance(raw, dict):
        raise ValueError(f"Server '{name}': entry must be an object")
    if not SERVER_NAME_RE.match(name or ""):
        raise ValueError(
            f"Server name '{name}' is invalid: use 1-16 chars of lowercase "
            "letters, digits or underscore, starting with a letter (it becomes "
            f"the tool prefix mcp_{name or '<name>'}_*)"
        )

    transport = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    url = str(raw.get("url") or "").strip()
    command = str(raw.get("command") or "").strip()
    if not transport:
        # Infer, so configs pasted from other MCP clients work verbatim.
        transport = "http" if url else "stdio"
    if transport in ("streamable-http", "streamable_http", "streamablehttp"):
        transport = "http"
    if transport not in VALID_TRANSPORTS:
        raise ValueError(f"Server '{name}': unknown transport '{transport}' (use stdio, http, or sse)")

    if transport == "stdio":
        if not command:
            raise ValueError(f"Server '{name}': stdio transport requires 'command'")
    else:
        if not url:
            raise ValueError(f"Server '{name}': {transport} transport requires 'url'")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Server '{name}': url must be http(s), got '{url}'")

    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
        raise ValueError(f"Server '{name}': 'args' must be a list of strings")
    env = raw.get("env") or {}
    headers = raw.get("headers") or {}
    for label, mapping in (("env", env), ("headers", headers)):
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in mapping.items()
        ):
            raise ValueError(f"Server '{name}': '{label}' must be an object of string values")

    safety = str(raw.get("safety") or "").strip().lower()
    if safety not in VALID_SAFETY:
        raise ValueError(f"Server '{name}': safety must be safe, caution, or dangerous")

    timeout = raw.get("timeout") or 0
    try:
        timeout = max(0, min(int(timeout), 3600))
    except (TypeError, ValueError):
        raise ValueError(f"Server '{name}': timeout must be an integer (seconds)") from None

    allowlist = raw.get("tool_allowlist")
    if allowlist is not None:
        if not isinstance(allowlist, list) or not all(isinstance(t, str) for t in allowlist):
            raise ValueError(f"Server '{name}': tool_allowlist must be a list of tool names")

    cfg = MCPServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=[str(a) for a in args],
        env={k: str(v) for k, v in env.items()},
        cwd=str(raw.get("cwd") or "").strip(),
        url=url,
        headers={k: str(v) for k, v in headers.items()},
        enabled=bool(raw.get("enabled", True)),
        safety=safety,
        timeout=timeout,
        tool_allowlist=list(allowlist) if allowlist is not None else None,
    )

    for label, mapping in (("headers", cfg.headers), ("env", cfg.env)):
        for key, value in mapping.items():
            if "${" not in value and _SECRET_LITERAL_RE.search(value):
                raise ValueError(
                    f"Server '{name}': {label}['{key}'] looks like a literal secret. "
                    "Put the secret in .env (e.g. MY_TOKEN=...) and reference it "
                    'here as "${MY_TOKEN}" — mcp_servers.json is plaintext on disk.'
                )
    # The url was exempt from the scan above, but hosted MCP endpoints
    # commonly carry the credential in the query string
    # (https://host/sse?api_key=sk-...) or in userinfo. Those were accepted,
    # written to disk in plaintext, and echoed back by GET /api/mcp/servers.
    if cfg.url and "${" not in cfg.url:
        parsed = urlparse(cfg.url)
        for part in (parsed.query, parsed.username or "", parsed.password or ""):
            if part and _SECRET_LITERAL_RE.search(part):
                raise ValueError(
                    f"Server '{name}': the url carries what looks like a literal secret. "
                    'Put it in .env and reference it as "${MY_TOKEN}" — mcp_servers.json '
                    "is plaintext on disk and the url is returned by the API."
                )
    return cfg


def expand_placeholders(value: str, *, server: str) -> str:
    """Expand ``${VAR}`` from process env. Missing vars raise — a silently
    empty Authorization header is a worse failure than a named one."""
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            missing.append(var)
            return ""
        return val

    out = _PLACEHOLDER_RE.sub(_sub, value)
    if missing:
        raise ValueError(
            f"Server '{server}': environment variable(s) {', '.join(sorted(set(missing)))} "
            "are not set — add them to .env (Settings → API keys writes there too)"
        )
    return out


def expand_mapping(mapping: dict[str, str], *, server: str) -> dict[str, str]:
    return {k: expand_placeholders(v, server=server) for k, v in mapping.items()}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_server_configs(path: Path | None = None) -> dict[str, MCPServerConfig]:
    """Parse mcp_servers.json. Accepts {"mcpServers": {...}} or a bare map.

    Invalid entries are skipped with a warning rather than failing the whole
    file — one bad paste must not take every other server down at boot.
    The default path resolves at call time so tests can monkeypatch
    MCP_SERVERS_PATH.
    """
    path = path or MCP_SERVERS_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read %s: %s", path, e)
        return {}
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        logger.warning("%s: expected an object of servers", path)
        return {}
    out: dict[str, MCPServerConfig] = {}
    skipped: dict[str, object] = {}
    for name, raw in servers.items():
        try:
            out[name] = parse_server_entry(str(name), raw)
        except ValueError as e:
            logger.warning("Skipping MCP server entry: %s", e)
            skipped[str(name)] = raw
    # Remembered so save_server_configs can put them back. The saved file is
    # written from the manager's live connections, which are only the entries
    # that PARSED — so one hand-edited typo used to be deleted outright by
    # the next add/remove/toggle from the UI, with no notice.
    _remember_skipped(path, skipped)
    return out


# name -> raw entry, per settings file, for entries that failed validation.
_SKIPPED_ENTRIES: dict[str, dict[str, object]] = {}


def _remember_skipped(path: Path, skipped: dict[str, object]) -> None:
    key = str(path)
    if skipped:
        _SKIPPED_ENTRIES[key] = skipped
    else:
        _SKIPPED_ENTRIES.pop(key, None)


def skipped_server_entries(path: Path | None = None) -> dict[str, object]:
    """Raw entries the last load of this file could not parse."""
    return dict(_SKIPPED_ENTRIES.get(str(path or MCP_SERVERS_PATH), {}))


def save_server_configs(configs: dict[str, MCPServerConfig], path: Path | None = None) -> None:
    """Atomic write (tempfile + os.replace), settings.json style."""
    path = path or MCP_SERVERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, object] = {name: cfg.to_dict() for name, cfg in configs.items()}
    # Carry forward anything the last load rejected, so a rewrite triggered
    # from the UI cannot silently delete a server the user is mid-way
    # through fixing by hand. A name that has since been re-added properly
    # wins over its skipped version.
    for name, raw in skipped_server_entries(path).items():
        entries.setdefault(name, raw)
    data = {"mcpServers": {name: entries[name] for name in sorted(entries)}}
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Tool naming
# ---------------------------------------------------------------------------


def _sanitize_tool_part(remote_name: str) -> str:
    part = re.sub(r"[^a-z0-9_]+", "_", remote_name.lower()).strip("_")
    part = re.sub(r"_{2,}", "_", part)
    return part or "tool"


def pernix_tool_name(server: str, remote_name: str, taken: set[str] | None = None) -> str:
    """Map a server's tool onto a flat registry name: mcp_<server>_<tool>.

    Capped at MAX_TOOL_NAME_LEN; overflow (and collisions after
    sanitization) truncate the tool part and append a short stable hash so
    the name survives refreshes deterministically.
    """
    base = f"mcp_{server}_{_sanitize_tool_part(remote_name)}"
    name = base[:MAX_TOOL_NAME_LEN]
    if name != base or (taken is not None and name in taken):
        import hashlib

        digest = hashlib.sha1(f"{server}/{remote_name}".encode()).hexdigest()[:6]
        name = f"{base[: MAX_TOOL_NAME_LEN - 7]}_{digest}"
    return name
