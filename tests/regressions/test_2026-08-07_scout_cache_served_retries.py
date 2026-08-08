"""Regression: the scout cache handed a retry back the plan that just failed.

Shipped defect (architecture review 2026-08-07, §2.4): _cache_key is
deliberately coarse — sha256(message + session + phase + utilization bucket) —
with a 300s TTL. A reflect or eval retry re-sends the same user message in the
same session, so any retry starting within five minutes of the failed attempt
was served that attempt's cached plan. run_scout declined to *cache* fallback
reports but nothing declined to *serve* a cached one to a retry, and is_retry
was never plumbed in at all.

Fix: run_scout takes is_retry and skips cache reads for it. Writes are
unchanged — a fresh plan is still worth caching for the next turn.
"""

from core.scout import runner as scout_runner
from core.scout.report import ScoutReport, SessionBrief


async def test_retry_does_not_read_the_cached_plan():
    # turn_count > 1 with a 1-word message takes the bypass path, so no LLM
    # call happens on a miss — only the deterministic fallback.
    brief = SessionBrief(session_id="cache-retry-a", turn_count=5)
    scout_runner._put_cache("ok", brief, ScoutReport(approach_guidance="the plan that failed"))

    fresh = await scout_runner.run_scout("cache-retry-a", "ok", brief)
    assert fresh.approach_guidance == "the plan that failed", "precondition: the cache serves normal turns"

    retried = await scout_runner.run_scout("cache-retry-a", "ok", brief, is_retry=True)
    assert retried.approach_guidance != "the plan that failed", "retry was served the plan it is retrying"
    assert retried.from_fallback


async def test_retry_still_leaves_the_cache_intact():
    """Only the read is skipped — a later non-retry turn keeps its hit."""
    brief = SessionBrief(session_id="cache-retry-b", turn_count=5)
    scout_runner._put_cache("ok", brief, ScoutReport(approach_guidance="cached plan"))

    await scout_runner.run_scout("cache-retry-b", "ok", brief, is_retry=True)

    after = await scout_runner.run_scout("cache-retry-b", "ok", brief)
    assert after.approach_guidance == "cached plan"
