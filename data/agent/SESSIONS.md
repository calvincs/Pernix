# Session Context

Deployment-specific context injected as `[INSTRUCTIONS]` every turn.
Tool protocols are already built into the system — do not duplicate them here.

## User Context

- Timezone: not set
- Key facts: not set (e.g. name, location, primary language, connected sources)

## Enabled Domains

Domains this installation is configured to assist with. Unlisted domains are read-only (level 1) by default.

- None configured. Add entries like: `- travel changes`, `- grocery replenishment`, `- calendar triage`

## Permission Levels

Per-domain action depth. Reference ladder: **1 Read** · **2 Suggest** · **3 Draft** · **4 Act with confirmation** · **5 Autonomous**

- No domains configured yet. Example: `- calendar triage: level 4`
- Never exceed the configured level for a domain without explicit user instruction.

## Active Intents

Long-running goals the agent is tracking on the user's behalf. Add, update, or close entries here.

- None active.

## Conventions

- Naming: not set (e.g. "prefix cron workflow names with `daily-`")
- Outputs: not set (e.g. "save reports to `reports/` in the workspace")
