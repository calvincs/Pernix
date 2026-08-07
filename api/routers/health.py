"""Pernix — Health and settings endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from config import Settings, settings

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health():
    from api.app import BUILD_ID
    from maintenance import get_maintenance
    from sessions.manager import get_manager

    manager = get_manager()
    maint = get_maintenance()

    return {
        "status": "healthy",
        "model": settings.llm_model or "(not set)",
        "version": "2.9.0",
        "build": BUILD_ID,
        "sessions_active": manager.active_count(),
        "maintenance": maint.get_stats(),
    }


@router.get("/api/health/detailed")
async def health_detailed(request: Request):
    # Restrict detailed diagnostics to localhost to prevent info disclosure
    client_host = request.client.host if request.client else ""
    if not is_local_client(client_host):
        raise HTTPException(403, detail="Detailed health info restricted to localhost")

    from core.llm.client import get_llm_client
    from core.tools.registry import get_registry
    from db.models import get_db_stats
    from maintenance import get_maintenance
    from sessions.manager import get_manager

    manager = get_manager()
    maint = get_maintenance()
    db_stats = get_db_stats()
    registry = get_registry()

    # Provider health (redact error details)
    provider_health = {}
    try:
        client = get_llm_client()
        health_results = await client.check_health()
        for name, status in health_results.items():
            provider_health[name] = {
                "healthy": status.healthy,
                "latency_ms": status.latency_ms,
                "has_error": bool(status.error),
                "models_available": status.models_available,
            }
    except Exception:
        provider_health["has_error"] = True

    from core.snooze import get_snooze

    snooze = get_snooze()

    return {
        "status": "healthy",
        "model": settings.llm_model or "(not set)",
        "version": "2.9.0",
        "providers": provider_health,
        "sessions": {
            "active": manager.active_count(),
        },
        "database": db_stats,
        "tools": {
            "registered": len(registry.all_tools()),
        },
        "maintenance": maint.get_stats(),
        "snooze": snooze.get_stats(),
    }


_REDACTED_FIELDS = {
    "notify_webhook_url",
    "db_path",
    "memory_dir",
    "workspace_dir",
    "skills_dir",
    "ssl_cert_path",
    "ssl_key_path",
    "auth_token",
}

_LOCKED_FIELDS = {
    "shell_security_mode",
    "shell_allowlist",
    "shell_env_mode",
    "shell_env_denylist",
    "shell_env_allowlist",
    "auto_approve_dangerous",  # runtime-only: set via --dangerous flag at startup, never via API
    "auth_token",
}


@router.get("/api/settings")
async def get_settings():
    import os
    from dataclasses import asdict

    data = {k: v for k, v in asdict(settings).items() if k not in _REDACTED_FIELDS}
    # Indicate whether API keys are set, but never send the actual values
    data["openrouter_api_key_set"] = bool(os.environ.get("OPENROUTER_API_KEY"))
    data["openai_api_key_set"] = bool(os.environ.get("OPENAI_API_KEY"))
    data["tavily_api_key_set"] = bool(os.environ.get("TAVILY_API_KEY"))
    data["voice_stt_api_key_set"] = bool(os.environ.get("VOICE_STT_API_KEY"))
    data["ssl_cert_path_set"] = bool(settings.ssl_cert_path)
    data["ssl_key_path_set"] = bool(settings.ssl_key_path)
    data["auth_token_set"] = bool(settings.auth_token)
    return data


def is_local_client(host: str) -> bool:
    """True when the request genuinely originates from this host's loopback.

    Includes the IPv4-mapped-IPv6 form a dual-stack listener reports for
    IPv4 loopback connections ("::ffff:127.0.0.1"). Docker-bridge sources
    (e.g. 172.17.0.1) are deliberately NOT local — traffic through a
    published port is remote to the container.
    """
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("::ffff:127.")


_SETTING_BOUNDS = {
    "shell_timeout": (1, 600),
    "tool_timeout": (1, 3600),
    "context_budget": (1000, 2_000_000),
    "max_tokens": (100, 200_000),
    "max_tool_rounds": (1, 100),
    "llm_max_concurrent": (1, 20),
    "openrouter_max_concurrent": (1, 20),
    "max_concurrent_workers": (1, 20),
    "max_fetch_size": (1024, 10_000_000),
    "browser_timeout": (5, 120),
    "scout_timeout": (5, 300),
    "compaction_threshold": (0.1, 0.95),
    "context_critical_threshold": (0.5, 0.99),
    "plan_review_timeout": (10, 600),
    "max_pending_messages": (1, 100),
    "notify_webhook_timeout": (1, 60),
    "port": (1024, 65535),
    "post_mortem_retention_days": (7, 3650),
    "candor_max_obs_per_turn": (10, 10_000),
    "rlm_max_iterations": (3, 100),
    "rlm_max_depth": (1, 3),
    "rlm_max_subcalls": (5, 500),
    "rlm_max_concurrent_subcalls": (1, 8),
    "rlm_timeout_seconds": (60, 3600),
    "rlm_run_retention_days": (1, 365),
    # Session kernel. kernel_idle_seconds must stay under the 1800s session
    # reap in practice, but the hard ceiling is a day; below 60s the kernel
    # would be reaped between tool rounds and never persist anything.
    "kernel_idle_seconds": (60, 86_400),
    "kernel_snapshot_max_bytes": (1_048_576, 4 * 1024 * 1024 * 1024),
    "kernel_max_concurrent": (1, 64),
    "large_result_bind_threshold": (1000, 10_000_000),
    "dream_hypotheses_per_cycle": (1, 20),
    "dream_validation_replays_per_day": (0, 50),
    "dream_report_interval_days": (1, 90),
    "dream_journal_retention_days": (2, 365),
    "dream_rlm_probe_interval_days": (1, 90),
    "canary_retention_days": (1, 365),
    "canary_baseline_runs": (1, 20),
    "canary_regression_delta": (0.01, 1.0),
    "canary_max_concurrent": (1, 4),
    "adaptive_max_entries_per_kind": (1, 100),
    "adaptive_max_auto_applies_per_day": (0, 50),
    "adaptive_edit_cooldown_hours": (0, 720),
    "adaptive_tripwire_window_turns": (5, 200),
    "telos_serendipity_budget": (0.05, 0.5),
    "telos_eig_floor": (0.0, 1.0),
    "telos_hypotheses_per_question": (1, 10),
    "telos_max_gated_backlog": (1, 100),
    "telos_max_eval_tokens": (1000, 200_000),
    "telos_question_max_attempts": (1, 10),
    "telos_soup_context_entries": (4, 40),
    "telos_budget_share_max": (0.1, 0.9),
    "telos_claims_floor_per_window": (0, 20),
    "telos_divergence_max": (0.01, 1.0),
}


_RESTART_FIELDS = {"network_enabled", "ssl_mode", "ssl_cert_path", "ssl_key_path", "cors_origins"}


@router.post("/api/settings")
async def update_settings(body: dict):
    from dataclasses import fields

    from config import _NO_PERSIST

    locked = set(_LOCKED_FIELDS)
    if settings.network_enabled:
        locked |= {"llm_base_url", "openrouter_base_url"}
    valid_fields = {f.name for f in fields(settings)} - _NO_PERSIST - locked
    updated = []
    for key, value in body.items():
        if key not in valid_fields:
            continue
        # Validate ssl_mode enum
        if key == "ssl_mode" and value not in ("self_signed", "custom"):
            continue
        # Validate voice enums — a typo'd mode would silently kill the mic button
        if key == "voice_mode" and value not in (
            "off",
            "local_whisper",
            "remote_whisper",
            "model_direct",
            "web_speech",
        ):
            continue
        if key == "voice_whisper_model" and value not in ("tiny", "base", "small", "medium", "large-v3"):
            continue
        # Language hint: "" (autodetect) or a 2-3 letter ISO code, normalized
        # lowercase — anything else would surface as a transcription failure.
        if key == "voice_language":
            v = str(value).strip().lower()
            if v and not (v.isascii() and v.isalpha() and len(v) in (2, 3)):
                continue
            value = v
        current = getattr(settings, key)
        try:
            # Reject empty strings for URL fields that have non-empty defaults.
            # voice_remote_url is exempt: its default is empty and clearing it
            # (to turn remote transcription off) is a legitimate edit.
            if (
                isinstance(current, str)
                and current
                and value == ""
                and key.endswith("_url")
                and key != "voice_remote_url"
            ):
                continue
            if isinstance(current, bool):
                if isinstance(value, bool):
                    setattr(settings, key, value)
                else:
                    setattr(settings, key, str(value).lower() in ("true", "1", "yes"))
            elif isinstance(current, list):
                if isinstance(value, list):
                    setattr(settings, key, value)
            else:
                setattr(settings, key, type(current)(value))
            # Bounds check for numeric settings
            if key in _SETTING_BOUNDS:
                lo, hi = _SETTING_BOUNDS[key]
                val = getattr(settings, key)
                if val < lo or val > hi:
                    setattr(settings, key, current)  # revert
                    continue
            updated.append(key)
        except (ValueError, TypeError):
            pass
    settings.save()

    # If a provider URL changed, tear down the cached router so the next LLM
    # request builds a fresh one using the updated URL — no restart required.
    if {"llm_base_url", "openrouter_base_url"} & set(updated):
        try:
            from core.llm.client import reset_router

            await reset_router()
        except Exception:
            pass  # Non-critical — restart will always recover

    # If openrouter_models changed, refresh the model registry so
    # GET /api/models returns the updated list immediately.
    if "openrouter_models" in updated:
        try:
            from core.llm.client import get_llm_client

            client = get_llm_client()
            await client.refresh_registry()
        except Exception:
            pass  # Non-critical — next startup will pick it up

    restart_required = bool(_RESTART_FIELDS & set(updated))

    # Validate SSL config when network is being enabled
    if restart_required and settings.network_enabled:
        from core.certs import validate_ssl_config

        errors = validate_ssl_config(settings.ssl_mode, settings.ssl_cert_path, settings.ssl_key_path)
        if errors:
            return {"updated": updated, "restart_required": True, "ssl_errors": errors}

    return {"updated": updated, "restart_required": restart_required}


@router.get("/api/settings/tool-approvals")
async def get_tool_approvals():
    """Return the persisted dangerous-tool approval scopes from tool_approvals.json."""
    import json
    from pathlib import Path

    p = Path("data/tool_approvals.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return {}


@router.delete("/api/settings/tool-approvals")
async def clear_tool_approvals():
    """Wipe all persisted dangerous-tool approval scopes."""
    from pathlib import Path

    p = Path("data/tool_approvals.json")
    if p.exists():
        p.unlink()
    return {"cleared": True}


@router.post("/api/settings/apikey")
async def set_api_key(body: dict):
    """Set or clear an API key. Persists to both os.environ and .env so the
    key survives a restart. Never returned to the client.
    """
    import os

    from config import write_env_var

    key_name = body.get("key", "")
    value = body.get("value", "")
    allowed = {"OPENROUTER_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY", "VOICE_STT_API_KEY"}
    if key_name not in allowed:
        return {"error": f"Key {key_name} not allowed"}
    if value:
        os.environ[key_name] = value
    elif key_name in os.environ:
        del os.environ[key_name]
    # Round-trip .env on disk so the key isn't lost on restart. Failures are
    # surfaced — the caller should know if persistence didn't take.
    try:
        write_env_var(key_name, value or None)
    except OSError as e:
        return {"updated": key_name, "is_set": bool(value), "persisted": False, "error": f"Failed to write .env: {e}"}
    return {"updated": key_name, "is_set": bool(value), "persisted": True}


@router.get("/api/settings/auth-token")
async def get_auth_token(request: Request):
    """Return the auth token. Localhost only."""
    client_host = request.client.host if request.client else ""
    if not is_local_client(client_host):
        raise HTTPException(403, detail="Auth token access restricted to localhost")
    return {"token": settings.auth_token or "", "network_enabled": settings.network_enabled}


@router.post("/api/settings/auth-token/regenerate")
async def regenerate_auth_token(request: Request):
    """Regenerate the auth token. Localhost only."""
    client_host = request.client.host if request.client else ""
    if not is_local_client(client_host):
        raise HTTPException(403, detail="Auth token access restricted to localhost")
    import secrets

    settings.auth_token = secrets.token_urlsafe(32)
    settings.save()
    return {
        "token": settings.auth_token,
        "message": "Token regenerated. Existing remote sessions will need the new token.",
    }


@router.get("/api/settings/access-qr")
async def access_qr(request: Request):
    """Return an SVG QR code for remote access. Requires auth (via middleware)."""
    if not settings.network_enabled or not settings.auth_token:
        raise HTTPException(404, detail="Network mode not enabled")

    # Build access URL: use first cors_origin if configured, else LAN IP
    if settings.cors_origins:
        base = settings.cors_origins[0].rstrip("/")
        access_url = f"{base}/?token={settings.auth_token}"
    else:
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "localhost"
        access_url = f"https://{lan_ip}:{settings.port}/?token={settings.auth_token}"

    try:
        import io as _io

        import qrcode as _qr
        import qrcode.image.svg as _qr_svg

        qr = _qr.QRCode(box_size=10, border=2)
        qr.add_data(access_url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=_qr_svg.SvgPathImage)
        buf = _io.BytesIO()
        img.save(buf)
        from starlette.responses import Response

        return Response(content=buf.getvalue(), media_type="image/svg+xml")
    except ImportError as e:
        import logging

        logging.getLogger("pernix.api").error("QR code generation failed: %s", e)
        raise HTTPException(501, detail=f"qrcode package not available: {e}")
    except Exception as e:
        import logging

        logging.getLogger("pernix.api").error("QR code generation error: %s", e)
        raise HTTPException(500, detail=f"QR generation failed: {e}")


@router.get("/api/env-vars")
async def list_env_vars():
    """Return names of relevant API key env vars (existence check only)."""
    import os

    CHECK_VARS = [
        "TAVILY_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
    ]
    return {"vars": [v for v in CHECK_VARS if v in os.environ]}


# ---------------------------------------------------------------------------
# Snooze trigger (testing/ops)
# ---------------------------------------------------------------------------


@router.post("/api/admin/snooze-cycle")
async def trigger_snooze_cycle(request: Request):
    """Run one snooze cycle now, skipping only the cadence gate.

    Localhost-only, like restart. The active-work and idle gates still
    apply — this cannot preempt live sessions. Returns the gate outcome
    and post-cycle stats so triggered testing can assert on both.
    """
    client_host = request.client.host if request.client else ""
    if not is_local_client(client_host):
        raise HTTPException(403, detail="Snooze trigger restricted to localhost")

    from core.snooze import get_snooze

    snooze = get_snooze()
    outcome = await snooze.run_cycle(force=True)
    result = {"outcome": outcome, "stats": snooze.get_stats()}
    if outcome == "skipped_idle":
        result["idle_blockers"] = snooze.idle_blockers()
    return result


# ---------------------------------------------------------------------------
# Server restart
# ---------------------------------------------------------------------------

_restart_pending = False
_restart_task = None  # strong ref for the delayed-shutdown task


@router.post("/api/admin/restart")
async def restart_server(request: Request):
    """Signal the server to restart. Creates a flag file, then triggers graceful shutdown."""
    client_host = request.client.host if request.client else ""
    if not is_local_client(client_host):
        raise HTTPException(403, detail="Restart restricted to localhost")

    import asyncio
    import os
    import signal
    from pathlib import Path

    global _restart_pending
    if _restart_pending:
        return {"status": "already_restarting"}
    _restart_pending = True

    Path("data/.restart").touch()

    scheme = "https" if settings.network_enabled else "http"
    port = settings.port

    async def _delayed_shutdown():
        await asyncio.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    # Module-level strong ref: asyncio only weakly references a running task,
    # so a discarded handle can be collected before the SIGTERM ever fires and
    # the restart silently never happens.
    global _restart_task
    _restart_task = asyncio.create_task(_delayed_shutdown())
    return {"status": "restarting", "url": f"{scheme}://localhost:{port}"}
