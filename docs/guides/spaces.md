# Spaces

A **space** is a named, colored group of long-lived sessions that share their work: a home folder in the workspace, their own memory buckets, optional directive overrides, bound scheduled jobs, one shared Python kernel, and a shared RLM run history. Spaces sit above the session list in the sidebar and their sessions never roll off into the time buckets.

Use a space when you want focused, ongoing work — a research project, a codebase, a client — where every session should see the same context and leave its artifacts in the same place.

Spaces group; they do not isolate. Ordinary sessions can read a space's files and memories, and space sessions see everything ordinary sessions see.

---

## Creating and managing spaces

- **Create**: the `+` next to "Spaces" in the sidebar. Pick a label and color. The label derives an immutable **slug** (e.g. "Research Lab" → `research-lab`) that names everything on disk — renaming the label later never moves files.
- **New session in a space**: the `+` on the space's header row. Sessions created this way stay grouped under the space, pinned-first then newest-first.
- **Move a session in or out**: the ▣ button on a session row opens a space picker. Moving never changes the session's recency ordering.
- **Edit**: the gear on the space header — label, color, and the directive editor.
- **Delete**: the × on the space header. By default everything is kept (sessions return to the normal list, memory files and the workspace folder stay, jobs unbind). Tick the checkbox in the dialog to also delete its sessions, memory files, workspace folder, and bound jobs.

What a space owns on disk:

| Thing | Where |
|---|---|
| Workspace home | `data/workspace/spaces/<slug>/` |
| Memory buckets | `data/memories/pernix.space.<slug>.*.md` |
| Directive overrides | `data/agent/spaces/<slug>/{SOUL,RULES,SESSIONS}.md` |
| Shared kernel state | `data/kernels/space-<id>/` |
| RLM runs | `data/workspace/spaces/<slug>/rlm/<run-id>/` |

## Workspace home — a default, not a jail

Space sessions run bash in `spaces/<slug>/` and resolve relative paths there first, so their files stay together. Nothing is walled off: absolute paths and existing files anywhere in the workspace still work in both directions, and `/tmp` stays available. A file that already exists in the global workspace is edited where it lives; only genuinely new files default into the space folder.

## Directive overrides

A space may replace SOUL.md, RULES.md, or SESSIONS.md **per file** — an undefined file falls back to the default. The editor (space gear → Directives) shows the current default read-only; **Customize** copies it into an editable buffer, **Revert to default** removes the override. The files are plain markdown under `data/agent/spaces/<slug>/` and can be hand-edited; changes apply on the next turn. Both the main agent and the scout read the space's versions. The agent birthdate always comes from the default SOUL.md.

## Memory

Memories formed in a space route to `pernix.space.<slug>.*` files automatically (an explicitly named file is always honored, global names included). Searches from any session in the space surface the space's entries first — scores are never inflated, only ordering changes — and searches elsewhere still find them on merit. Background consolidation never merges files across spaces or between a space and the global buckets. The Memory tab badges space buckets with the space's color.

## Scheduled jobs

Bind a job to a space in the Jobs tab's add form, via `POST /api/jobs` with `space_id`, or with the `schedule_job` tool — a job scheduled *from* a space session inherits that space automatically (pass `space_id="none"` to opt out). Each firing creates a fresh `Cron:` session inside the space, which inherits the space's directives, memory routing, home folder and kernel. Machine-created cron run sessions still prune after 7 days (pin one to keep it); sessions you created yourself are never auto-pruned.

## Shared kernel and RLM

All sessions in a space share **one** Python REPL kernel: variables set in one session are live in the others. If two sessions run code at the same moment, the second waits on the kernel's busy lock and may see a "shared space kernel busy" message — the state is safe, retry after the sibling finishes. Deleting a member session never destroys the shared kernel; deleting the space (cascade) does.

RLM runs launched from space sessions store their artifacts under the space's folder and are listed together (`GET /api/rlm/runs?space_id=…`); any session in the space can `continue_from` a sibling's run.
