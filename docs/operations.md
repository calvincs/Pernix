# Operations and maintenance

This guide covers Pernix 3.2. For upgrade steps and backup scope, see
[upgrade.md](upgrade.md#upgrading-to-32); settings are in
[configuration.md](configuration.md#operational-maintenance-and-logs).

## Check the service and the work separately

`GET /api/health` reports service status, active/loaded session counts and
maintenance diagnostics. A responding server can still have degraded maintenance.
Read `maintenance.snooze`:

| Field | What it tells you |
|---|---|
| `running`, `active_rung` | Whether a cycle is running and its current activity. |
| `last_outcome` | `ran` completed normally; `partial` had an activity failure; `backstop` exceeded the whole-cycle limit; `error` failed the cycle; `yielded` gave way to work; `cancelled` was interrupted during shutdown. |
| `rung_durations_ms` | Activity timings for the current/last cycle. |
| `rung_failures` | Activity names and timeout/error types. |
| `last_successful_cycle` | Last cycle that completed normally, retained across restarts. |
| `degraded` | A partial, timed-out or failed cycle needs inspection. A later successful cycle clears it; a yield preserves the previous state. |

Some fields are absent until a cycle has run. Inspect logs alongside the stats;
a zero count after restart is not proof of a clean month. The `build` field is a
web-client cache identifier, not a backend commit fingerprint. For deployments,
check the checkout commit and the files inside the running image.

## Bounded memory maintenance

Consolidation and rerouting run on the background executor, save progress and
continue in later cycles. Large consolidation scans consider at most 2,000 file
pairs or 10 seconds per batch, sampling up to 24 fingerprints per file to find
candidates. Original entries still undergo merge validation. Rerouting considers
at most 200 settled entries or 10 seconds and avoids destination scoring when
an entry already matches its current file. A successful cycle does not mean the
whole backlog has been processed.

Deduplication, consolidation and rerouting have 60-second activity limits;
splitting has 120 seconds. A failure marks the cycle partial and lets later
activities run. The whole-cycle hang backstop remains separate. Worker
cancellation stays set even when a new cycle begins.

Failed splits store a file-revision backoff (30 minutes up to 24 hours), reduce
the batch size, and let other files run. A changed file revision can retry.
Warnings report finish reason and batch size without copying memory content
into the log. These measures bound repeated work; they cannot make a provider
return valid output every time.

## Verify a cycle after deployment

`POST /api/admin/snooze-cycle` is localhost-only. It bypasses cadence and recent
activity cooldown but still refuses active work and yields to new work. Its
response includes the outcome and stats; `skipped_idle` includes blockers.
With a container, call it from inside the container: traffic through a published
port may appear to come from the Docker bridge rather than localhost. Use your
instance's HTTP/HTTPS and certificate configuration.

Confirm later activities run, no unexpected `rung_failures` remain, and a normal
cycle records `last_successful_cycle`. Do not raise the global timeout merely
to conceal a stuck activity. See [Reflect and Snooze](internals/reflect-and-snooze.md)
for the activity ladder.

## Detached jobs

The maintenance tick reconciles persisted running jobs in pages of 100, including
the first tick after startup. New jobs record exit status and UTC completion time
atomically. Old jobs without a finish timestamp show unknown elapsed duration;
reconciliation must not manufacture a completion date. This is separate from
scheduled cron jobs and their `/api/jobs` run history.

## Log retention

- `data/logs/pernix.log`: application events, including maintenance activity
  starts, finishes and failures.
- `data/logs/access.log`: routine HTTP access records.
- Both rotate at UTC midnight on the next emitted record and retain 35 compressed
  daily archives per stream. Log-line timestamps follow the process timezone;
  UTC rotation does not change the timestamp formatter.
- Existing numbered rotations are left in place. Their disk usage is additional
  to the new archive limit. Retention cannot recover already-discarded history.

Daily retention does not cap a single busy day's size. Include the log directory
in ordinary disk monitoring and preserve relevant rotations before an audit.

## Recovery boundaries

Keep complete application backup generations and separately protect configuration,
secrets, hand-authored skills and deployment overrides. Exact skill rollback is
available for journaled applications whose current revision still matches;
newer edits and old unjournaled changes require explicit recovery. See
[using skills](guides/using-skills.md#skill-self-healing).
