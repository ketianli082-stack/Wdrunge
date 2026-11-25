# Failure cases — targeted regressions

Existing test entry points, not newly executed results. Run from backend/.

## F1: Provider failures

```sh
uv run pytest -q tests/test_resilience_routing.py
```

Simulated provider failure and routing conditions: retry exhaustion, non-transient errors, alternate routing, failure bursts and mid-stream stalls. Expected: bounded attempts, correct fallback or safe stopping, observable error or journal entry.

## F2: Tool/service failures

```sh
uv run pytest -q tests/test_tool_call_reliability.py
```

Fixtures include startup crashes, occupied ports, sandbox socket denial and transient reviewer errors. Expected: explicit failure cause, bounded handling and no uncontrolled repeated process start. Native tests are platform-dependent.

The final report references targeted checks against 927ca26 on 24 November 2026 with simulated provider responses and temporary files/SQLite. The referenced wdrunge-reliability-evidence.txt is not in the repository. Match the original commands and outcomes to these test entry points when the log is supplied; do not infer pass counts.
