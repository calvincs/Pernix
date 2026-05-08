# Upgrade Guide

Pernix tries hard to upgrade transparently — `git pull && pip install -r requirements.txt && python run.py` is the happy path, and DB migrations run automatically on startup. This page covers the cases where you need to do something extra.

For the full list of changes by date, see [changelog.md](changelog.md). For DB schema details, see `db/database.py:MIGRATIONS`.

---

## The standard upgrade path

```bash
cd /path/to/pernix
git pull
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

That's it for most upgrades. The DB migrates forward automatically. Settings, memory, sessions, skills, and certs are preserved.

If you want to be cautious, **back up `data/` first.** It's a directory tree of SQLite, markdown, and JSON — `cp -r data data.backup` works fine.

---

## DB migrations

The schema is at v16. Migrations run sequentially at startup based on the version stored in the SQLite `user_version` pragma. Each migration is forward-only — there's no automatic downgrade.

If you ever need to downgrade Pernix to an older version (and therefore an older schema), the safe path is:

1. Back up `data/sessions.db`.
2. Check out the older Pernix code.
3. **Use a fresh `data/` directory** with the older code, or restore from a backup taken at that version. Don't try to run an older Pernix against a newer DB.

Running newer Pernix against an older DB is fine — that's just a normal upgrade.

---

## Breaking changes worth knowing about

These are the upgrade points where something the user might have set up needs attention. Each is dated.

### 2026-05-05 — `data/agent/AGENTS.md` renamed to `SESSIONS.md`

If you customized your agent identity files and have an `AGENTS.md`, the file is now expected at `SESSIONS.md`. Rename it:

```bash
mv data/agent/AGENTS.md data/agent/SESSIONS.md
```

The system falls back to `INSTRUCTIONS.md` if `SESSIONS.md` is absent, so an old install with neither file just gets the empty default. Existing `AGENTS.md` files are not auto-renamed.

### 2026-05 — `search_web` now requires `TAVILY_API_KEY`

If you were relying on the DuckDuckGo fallback (which existed in earlier versions), it's gone. Set `TAVILY_API_KEY=tvly-...` in `.env` to keep web search working. Tavily has a free tier — sign up at [tavily.com](https://tavily.com).

Without the key, `search_web` returns a setup hint instead of failing silently or degrading.

### 2026-04-20 — Session state machine v2

The 5-state legacy enum (`IDLE | SCOUTING | PROCESSING | ERROR | DELETED`) was replaced with a 10-state machine. Existing sessions migrate to the new states automatically (migration v13 adds `session_state_log`; migration v16 persists `state_v2` and `watched_worker_ids`).

If you have **external integrations that read session state** via the REST API, the `state` field returned by `GET /api/sessions/{id}/status` now uses the new 10-value enum. A `compat_status` field is provided for legacy 3-value compat (kept for the CLI's benefit).

If your integration only reads via SSE events, no change — `session.state_changed` events flow as before with new state values.

### 2026-04-13 — Network mode introduced

If you ran older Pernix with `--host 0.0.0.0` to expose it to your LAN, that's no longer enough. Pernix now requires `network_enabled = true` in Settings (which forces HTTPS + Bearer token + SSRF lockdown). Plain HTTP-on-LAN is no longer supported.

Migration steps:
1. Restart with `network_enabled = true` in Settings.
2. Pernix auto-generates a self-signed cert and a Bearer token on first start.
3. Update your existing clients to use the new token (printed in the logs the first time, or visible at `GET /api/settings/auth-token` from localhost).

For a smoother mobile experience, replace the self-signed cert with mkcert — see [deployment/mkcert.md](deployment/mkcert.md).

### 2026-04 — `auto_approve_dangerous` is now CLI-only

Earlier versions allowed `auto_approve_dangerous = true` to be set via Settings or `.env`. It's now **only** settable via the `--dangerous` command-line flag at startup. This prevents a remote API client (or a prompt injection) from silently elevating its own privileges.

If you previously set `auto_approve_dangerous = true` in `data/settings.json`, the value is ignored. Use `python run.py --dangerous` if you genuinely want to bypass the gate.

For unattended cron jobs, no flag needed — cron sessions auto-skip the gate via `_is_unattended_session()`.

### 2026-04-03 — Initial v2 spec

If you were running a very early pre-release version of Pernix, there's no automatic upgrade path from that era. The data layer changed enough that a clean `git clone` of the current code with a fresh `data/` directory is the supported path. You can copy individual `data/skills/` and `data/memories/` files across if they're useful.

---

## After-upgrade housekeeping

After any upgrade, a couple of things are worth checking:

- **Did the server start cleanly?** Watch the startup logs — migration failures or schema mismatches show up there.
- **Is your model still accessible?** `GET /api/health/detailed` (localhost-only) shows provider connectivity.
- **Did your skills survive?** `data/skills/` is preserved across `--rebuild` and across normal upgrades, so they should be fine. If a skill stops loading, check the YAML frontmatter — syntax errors silently skip the skill (the error is logged).
- **Are your custom tools still present?** `data/tools/` is preserved. If a tool depended on a Python package that's no longer pinned in your `data/workspace/.venv/`, run the agent's `restore_tool_packages` to reinstall.

---

## When something goes wrong

- **Schema mismatch errors at startup** — usually means you upgraded but didn't run the new code yet, or you copied an older `sessions.db` over a newer one. Check `git log -1` confirming you're on the version you expect.
- **"Module not found" Python errors** — you forgot to `pip install -r requirements.txt` after `git pull`.
- **Settings UI shows fields you didn't have before** — that's normal; new releases add settings. Defaults are sensible.
- **Sessions all stuck in old state names** — restart again. The state-machine v2 migration runs at startup and reconciles stuck states.

If something genuinely broke and you can't proceed, the nuclear option:

```bash
# Back up first!
cp -r data data.backup

# Wipe runtime state, keep settings + .env + skills + agent identity + certs
python run.py --rebuild
```

`--rebuild` requires typing `yes` to confirm. It deletes sessions, memory, workspace, and logs but preserves everything you configured.
