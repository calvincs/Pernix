# Spaces

A **space** is a named, colored group of long-lived sessions that share their work: a home folder in the workspace, their own memory buckets, optional directive overrides, bound scheduled jobs, one shared Python kernel, and a shared RLM run history. Spaces sit above the session list in the sidebar and their sessions never roll off into the time buckets.

Use a space when you want focused, ongoing work — a research project, a codebase, a client — where every session should see the same context and leave its artifacts in the same place.

Spaces group; they do not isolate. Ordinary sessions can read a space's files and memories, and space sessions see everything ordinary sessions see.

---

## Creating and managing spaces

- **Create**: the `+` next to "Spaces" in the sidebar — or, before you have any spaces, the **+ New space** row at the bottom of the session list (the Spaces header only appears once a space exists). Pick a label and color. The label derives an immutable **slug** (e.g. "Research Lab" → `research-lab`) that names everything on disk — renaming the label later never moves files.
- **New session in a space**: the `+` on the space's header row. Sessions created this way stay grouped under the space, pinned-first then newest-first.
- **Move a session in or out**: the move button (four arrows) on a session row opens a space picker; on touch it is **Move to space…** in the row's `⋯` sheet. Moving never changes the session's recency ordering.
- **Edit**: the cog on the space header — label, color, and the directive editor. On touch the cog and the × move behind one `⋯` on the header (**Space settings**, **Delete space**); the `+` stays.
- **Delete**: the × on the space header (the same `⋯` sheet on touch). By default everything is kept (sessions return to the normal list, memory files and the workspace folder stay, jobs unbind). Tick the checkbox in the dialog to also delete its sessions, memory files, workspace folder, and bound jobs.
- **Rail and indent**: every open space draws a thin colored rail down its left edge, from its first row to the **Show all**/**Show fewer** row, with everything inside — buckets, sessions, workers — indented beside it. A **Sessions** heading separates spaces from the ordinary list below, once at least one space and one session outside a space both exist.

What a space owns on disk:

| Thing | Where |
|---|---|
| Workspace home | `data/workspace/spaces/<slug>/` |
| Memory buckets | `data/memories/pernix.space.<slug>.*.md` |
| Directive overrides | `data/agent/spaces/<slug>/{SOUL,RULES,SESSIONS}.md` |
| Shared kernel state | `data/kernels/space-<id>/` |
| RLM runs | `data/workspace/spaces/<slug>/rlm/<run-id>/` |

## Suggested spaces

Off by default. Turn it on in **Settings → Autonomy & idle work → Space suggestions** and Pernix will look over your ordinary chats while it is idle and offer to group the work you keep coming back to. It only ever offers.

**What triggers one.** A background scan reads the last `space_suggest_window_days` (default 30) of ordinary chats — archived ones included, machine sessions (cron, workers, canaries) excluded — and makes one background-model call that groups them by the *kind of work* you keep doing, not by the tool used or the day it happened. A group then has to survive a mechanical gate: at least `space_suggest_min_sessions` chats (default 5) spread over at least `space_suggest_min_days` distinct calendar days (default 3), nothing that reads as greetings or small talk, nothing you have already declined, and nothing already waiting for you. The scan runs at most once every `space_suggest_scan_interval_hours` (default 24) and only when at least ten new chats have appeared since the last one, so a quiet week produces no scan at all. It keeps at most two suggestions per scan and never leaves more than five pending.

**The two kinds.** Either *a new space* — five chats about fact-checking, none of them grouped, so Pernix offers to make "Fact checking" — or *chats that belong in a space you already have*, offered as a move rather than a new space.

**Where a suggestion shows.** One row with a sparkle, under your spaces in the sidebar and above the **Sessions** heading (at the top of the list when you have no spaces yet): *Suggested · Fact checking* for a new space, or *3 chats belong in Pernix* for a move, with the chat count at the right end. It is not a session row and never turns into one. The bell gets a notification at the same time, so you find it even if the sidebar is collapsed.

**What the review sheet lets you change.** Clicking the row opens a sheet — "Make this a space?" or "Move these into …?" naming the target space — with one sentence saying why the group was picked, and:

- the **name** and **color** of the space to be created (a move has neither: the target space already has both),
- a **checklist of the chats**, all ticked; untick one and it stays where it is,
- one tab per **drafted directive**, when the work would clearly benefit from a standing instruction. The tab names the file (SOUL, RULES or SESSIONS) and gives one sentence of rationale; the current default is shown read-only, and the editable box below it holds that same default with the drafted section appended at the end. Edit it, or tick **Use the default instead** to drop the addition entirely. An addition is always appended — a draft never rewrites or replaces a default.

The footer has the action (**Create space**, or **Move N sessions**), **Not now** — which closes the sheet and leaves the suggestion pending — and **Don't suggest this**.

**What accepting does on disk.** For a new space: creates the space, makes its workspace home at `data/workspace/spaces/<slug>/`, and writes **only** the directive files you kept, to `data/agent/spaces/<slug>/`. A file whose tab you dropped is never written, so that file stays undefined and keeps falling back to the default. Then it moves the chats you left ticked into the space; unticked chats are untouched. Moving never changes a chat's recency ordering, exactly as moving one by hand does not. For a move into an existing space nothing is created — only the chats move. If the name you chose collides with an existing space's slug, the sheet says so and stays open so you can pick another.

**What declining remembers.** **Don't suggest this** marks the suggestion declined and nothing else — no chat is moved, renamed or deleted. Pernix remembers the topic and the chats that were in it, and will not propose that habit again, nor a near synonym of it, nor a group that overlaps those chats by half or more. That holds until you clear it: the same settings section lists **Declined** suggestions with their date and chat count, with **Clear** per row and **Clear all** at the bottom. A cleared topic can be suggested again.

**Expiry.** A suggestion you neither accept nor decline expires after `space_suggest_ttl_days` (default 14) and leaves the sidebar. Expiring is not declining — it is not remembered — so if you keep doing that kind of work, a later scan can raise it again.

**Settings.** All six apply hot; the section sits on the Autonomy & idle work tab.

| Setting | Default | What it does |
|---|---|---|
| `space_suggest_enabled` | `false` | Suggest spaces from recurring work. Off means the scan never runs and the rung is absent from the idle ladder. |
| `space_suggest_window_days` | `30` | How far back a scan looks. |
| `space_suggest_min_sessions` | `5` | Chats a group needs before it can be suggested. |
| `space_suggest_min_days` | `3` | Distinct calendar days those chats must span — a burst on one afternoon is not a habit. |
| `space_suggest_scan_interval_hours` | `24` | How often a scan may run at all. |
| `space_suggest_ttl_days` | `14` | How long a pending suggestion waits before it expires. |

The same section has **Scan now**, which runs a scan immediately and shows what it *would* propose — label, kind, how many chats, why, and which directives were drafted. Nothing is stored until you press **Keep these as suggestions**.

**The standing rule: nothing is created, moved or written without your click.** A scan's only side effect is a pending suggestion row and a notification. The space, its workspace folder, its directive files and every chat move happen when you press the button in the sheet, and never before.

## Workspace home — a default, not a jail

Space sessions run bash in `spaces/<slug>/` and resolve relative paths there first, so their files stay together. Nothing is walled off: absolute paths and existing files anywhere in the workspace still work in both directions, and `/tmp` stays available. A file that already exists in the global workspace is edited where it lives; only genuinely new files default into the space folder.

## Directive overrides

A space may replace SOUL.md, RULES.md, or SESSIONS.md **per file** — an undefined file falls back to the default. The editor (space settings → **Directives**) shows the current default read-only; **Customize** copies it into an editable buffer, **Revert to default** removes the override. The files are plain markdown under `data/agent/spaces/<slug>/` and can be hand-edited; changes apply on the next turn. Both the main agent and the scout read the space's versions. The agent birthdate always comes from the default SOUL.md.

## Memory

Memories formed in a space route to `pernix.space.<slug>.*` files automatically (an explicitly named file is always honored, global names included). Searches from any session in the space surface the space's entries first — scores are never inflated, only ordering changes — and searches elsewhere still find them on merit. Background consolidation never merges files across spaces or between a space and the global buckets. The Explorer's Knowledge → Memory tab badges space buckets with the space's color.

## Scheduled jobs

Bind a job to a space in the Explorer's Automation → Jobs tab, in its add form, via `POST /api/jobs` with `space_id`, or with the `schedule_job` tool — a job scheduled *from* a space session inherits that space automatically (pass `space_id="none"` to opt out). Each firing creates a fresh `Cron:` session inside the space, which inherits the space's directives, memory routing, home folder and kernel. Machine-created cron run sessions still prune after 7 days (pin one to keep it); sessions you created yourself are never auto-pruned.

## Shared kernel and RLM

All sessions in a space share **one** Python REPL kernel: variables set in one session are live in the others. If two sessions run code at the same moment, the second waits on the kernel's busy lock and may see a "shared space kernel busy" message — the state is safe, retry after the sibling finishes. Deleting a member session never destroys the shared kernel; deleting the space (cascade) does.

RLM runs launched from space sessions store their artifacts under the space's folder and are listed together (`GET /api/rlm/runs?space_id=…`); any session in the space can `continue_from` a sibling's run.
