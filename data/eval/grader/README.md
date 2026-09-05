# Grader hold-out set

Fixtures the reflect grader is scored against, nightly, by
`core/reflect_holdout.py` (`trust.grader_holdout` in `snooze_state`).

Each file is one turn the right answer is already known for:

```json
{
  "id": "short-slug",
  "user_request": "what the user asked, verbatim",
  "transcript_excerpt": "the attempt transcript reflect would have seen",
  "final_response": "the agent's last message",
  "tool_summary": {"tool_name": {"calls": 1, "failures": 0, "total_latency_ms": 10}},
  "expected_verdict": "pass | retry | escalate",
  "expected_failure_cause": "none | scout | agent | skill | task | env",
  "note": "why this is the right answer"
}
```

Half the set is there to catch over-strictness, not laxity: a grader that
retries finished work is as broken as one that passes a phantom file, and the
live verdict mix has drifted that way before (see the reflect calibration
notes). Add a case whenever a real mis-grade is found on the box — the fixture
is the regression test for a verdict.

**These fixtures never enter the system's memory.** `run_holdout` builds the
evidence blob in-process, calls the grader, and writes only the score. It never
creates a session, never writes a post-mortem, never touches the workspace —
which is what keeps the hold-out a hold-out.
