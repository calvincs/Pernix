# Regression tests — one file per shipped defect

Convention (adaptation plan 1e, mirroring prime-agent's
`test/suite/regressions/` discipline): when a defect ships and gets fixed,
pin it here as `test_<date-or-issue>_<slug>.py` — one file per incident,
named for it, with a docstring stating what broke, how it manifested, and
what the fix was. The codebase's comment culture already cites incidents at
the fix site; these files make the citations executable.

This directory is for *defects that shipped*, not feature tests — feature
coverage belongs beside the feature in `tests/`.

## Related: two fakes, two jobs

- `tests/conftest.py` `FakeLLMClient` — stubs the **client** for agent-loop
  tests. No scheduling, no failover; cheap and right for most tests.
- `tests/faux_provider.py` `FauxProvider` — stands in for a **provider**
  inside a real `ProviderRouter`, for the paths a client-level fake cannot
  reach: semaphore acquisition, typed failover classification, Ollama
  fallback, message sanitization.
