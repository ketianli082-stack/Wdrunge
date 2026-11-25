# Evaluation guide

Use a fresh disposable workspace and record exact prompts, initial conditions, model settings, elapsed time, token usage, tool calls, delivery checks and final user-visible status. Repeat live cases when practical and disclose variability.

- [Ordinary task](sample_inputs/01_ordinary_task.md): historical input, available for rerun.
- [Changed request](sample_inputs/02_changed_request.md): proposed manual follow-up, not a recorded live result.
- [Failure situations](sample_inputs/03_failure_cases.md): existing simulated regression entry points.
- [Results](results/README.md) and [traces](traces/README.md): historical evidence and pending original logs.

A live task passes only if requested functionality is demonstrated by appropriate build, service or browser checks. A changed request must preserve unrelated existing behavior. Failure checks should verify detection, bounded recovery or safe stopping, observable feedback and avoidance of uncontrolled repeated actions. Report failures and skips; distinguish simulated and live checks. Historical comparisons are descriptive unless starting conditions and model settings are matched.

Commands are in the root README. Redact secrets from all logs.
