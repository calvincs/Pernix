# Memory and recall

Pernix has two distinct kinds of memory:

- **Conversation memory** — every message in a session, stored in `data/sessions.db`. The agent sees these directly; compaction can summarize old turns, but originals stay in the DB.
- **Long-term memory** — persistent facts, decisions, lessons, and learned preferences stored as **markdown files** under `data/memories/`.

This page is about the long-term store. The conversation side is covered in [sessions-and-chat.md](sessions-and-chat.md).

---

## What it looks like on disk

```
data/memories/
├── _index.db                    # FTS5 search index
├── user.profile.md              # learned facts about you
├── pernix.decisions.md          # design decisions
├── pernix.config.md             # config details / hard-won values
├── pernix.debugging.md          # debugging patterns
├── ...
```

Each `.md` file groups related entries by topic. Entries are short blocks (typically 2–5 sentences) prefixed with metadata — when they were learned, optional weight, optional tags. The agent reads and writes these files using its memory tools.

These are **just markdown files**. Open `user.profile.md` in any text editor and you can read what your agent has learned about you. You can edit, delete, or rearrange entries directly — an index health check reconciles the FTS5 index with the files (at startup, during Snooze, or on demand via `POST /api/memory/maintenance`).

---

## How recall works

At the start of each turn, scout searches the memory store and injects the most relevant entries into the system prompt:

1. The user's incoming message is the search query.
2. The FTS5 index returns matches scored with BM25.
4. Each entry is truncated to `scout_preload_memory_char_limit` chars (default 600) in the auto-injected baseline to control prompt budget. When the agent actively calls `recall()` (or `deep_recall`), it receives the full entry content.
5. Top matches go into the scout report; from there, the main agent's prompt.

You can disable recall entirely with `memory_recall = false`. There is no relevance-threshold setting — the baseline injects the top matches unconditionally, and the only budget levers are `memory_recall` (off entirely) and `scout_preload_memory_char_limit` (how much of each entry). If too many irrelevant entries leak in, the fix is on the store side: prune or consolidate the entries that keep matching, or let Snooze's dedup and staleness sweeps do it.

### Semantic retrieval

By default memory search is purely lexical (BM25 over the FTS5 index) — an entry about "deploy credentials" won't match a query about "release passwords". Setting **`embedding_model`** to a local Ollama embedding model (e.g. `nomic-embed-text`, in Settings → Models) turns search **hybrid**: BM25 and vector cosine similarity each contribute their top matches, fused with reciprocal-rank fusion, so both exact-term and by-meaning matches surface.

The mechanics stay true to the store's design:

- **Setting the model is the switch.** Leave it empty and every search behaves exactly as before — no vectors, no Ollama embedding calls.
- **Embedding is background work.** Writes never block on it: new or edited entries are embedded during Snooze's index-reconciliation sweep (`embedding_batch_size` texts per call, scheduled behind live turns so it can't stall your chat). Until an entry is embedded it simply doesn't participate in the vector channel yet.
- **Degrades gracefully.** If Ollama or the embedding model is unavailable, search falls back to lexical with a logged warning.
- **The vectors are a rebuildable sidecar** in `data/memories/_index.db`, like the FTS index — the markdown files remain the only source of truth. Changing the embedding model marks all vectors stale; they re-embed in the background.

### Wiki-links

Entry content can reference other memory with wiki-link syntax:

- `[[file-name]]` — links to a memory file (e.g. `[[pernix.decisions]]`)
- `[[file-name@epoch]]` — pins one specific entry in that file

At recall time, links found in matched entries are expanded **one hop**: the linked entries are appended to the results, labeled `source=link` so the agent can see they arrived by reference rather than by matching the query. This lets a lesson in `pernix.debugging.md` pull in the config entry it depends on without duplicating it. Consolidation and other Snooze maintenance preserve `[[...]]` refs when merging or moving entries — you can also write them yourself when editing the files by hand.

---

## How writes happen

The agent writes and mutates entries via memory tools:

- **`remember`** — append a new entry to the appropriate file (auto-routed if no file given), creating the file if needed
- **`ingest`** — bulk import a structured document, routing sections to the right files
- **`update_memory`** — replace the content of a specific entry by `(file, epoch)`. Metadata is preserved; the epoch stays stable. Use this to correct a wrong fact rather than appending a contradiction.
- **`forget`** — permanently delete a specific entry by `(file, epoch)`. Cannot be undone — prefer `update_memory` when you can.
- **`recall`** / **`deep_recall`** — read-side; output now includes `epoch=N` so the agent can identify which entry to update or forget

Writes happen at three points:

- **Inside a turn** — when the agent learns something it explicitly wants to remember, or when it discovers a stored entry is wrong and needs correcting.
- **Reflect's distillation** — after a successful turn, a background hook may distill the session's durable findings, decisions and lessons into long-term memory. How many entries that produces is the model's call; there's no fixed cap, and the prompt pushes for "fewer, sharper" over coverage. Each one passes the duplicate gate, and a distilled entry that reads as a correction of one already stored rewrites it in place (original epoch kept, correction date stamped) rather than being dropped for resembling it.
- **Snooze consolidation** — during idle periods, similar entries get clustered and merged.

You don't typically have to do anything to keep memory healthy — Snooze deduplicates, consolidates, and archives in the background.

---

## What the agent remembers about you

Look at `data/memories/user.profile.md`. Typical entries include:

- Demographics and role (timezone, professional context)
- Communication preferences (terse vs verbose, formality)
- Recurring projects and tools you use
- Tasks you've delegated repeatedly

You can edit this file directly. If you want the agent to forget something, just delete the entry. The change is picked up when the index health check next runs — at startup, during Snooze, or immediately via `POST /api/memory/maintenance` with `{"reindex": true}`.

The agent will not store sensitive data unless you give it to it. If you don't want a piece of information stored, tell the agent so or delete the entry afterward — there's no automated PII redaction.

---

## Snooze — idle housekeeping

When no sessions are actively processing, **Snooze** runs background maintenance. It checks every `snooze_interval_ticks` (default 10 ticks ≈ 10 minutes) whether to run, and if so, performs:

- **Deduplication** — finds near-duplicate entries and merges them. Default cadence: every 7 days per file.
- **Consolidation** — clusters semantically related entries into the same file. Default cadence: every 24 hours.
- **User profile extraction** — pulls preferences and recurring patterns into `user.profile.md`.
- **Post-mortem cleanup** — old failure analyses get summarized and archived past `post_mortem_retention_days` (default 90).

Snooze yields immediately when you start a new session — your work always takes priority.

If you want to run the index health check manually (it reindexes if the FTS5 index is stale; the LLM-backed dedup/consolidation tasks only run inside Snooze):

```bash
curl -X POST http://localhost:8090/api/memory/maintenance
```

---

## Searching memory directly

Two ways:

1. **REST:**

   ```bash
   curl -G --data-urlencode 'q=what do I know about X' \
     http://localhost:8090/api/memory/search
   ```

2. **Ask the agent.** "What do you know about X from prior sessions?" — the agent uses `recall` (or `deep_recall` for a harder dig) and reports back.

For full search syntax (phrase, AND/OR, exclude), see [../api.md](../api.md#memory).

---

## Resetting memory

- **Selective:** delete entries from individual `.md` files.
- **Full reset:** `python run.py --rebuild` wipes `data/memories/` (and sessions, workspace, logs). Settings and API keys are preserved.
- **Just the index:** delete `data/memories/_index.db` — it regenerates on next start from the markdown files.
