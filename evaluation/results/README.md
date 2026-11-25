# Results and evidence index

Recorded 24 November 2026 integration results from [final-integration.md](../../docs/final-integration.md): Windows, Python 3.13.15, Node 24.12.0, Vite 8.3.2.

| Check | Recorded result |
| --- | --- |
| Frontend production build | Passed; bundle-size warning |
| Message/execution/formal acceptance | 20 passed |
| Character/office checks | 30 passed |
| Browser annotation and cache-refresh | Passed; fixture pages and mocked requests |
| FastAPI smoke check | Passed; temporary database, no model calls |
| Backend full suite | 487 passed, 84 failed, 25 skipped; 202.83 seconds |

The first backend failure involved pass_fds on Windows. Other failures are incompletely diagnosed; not all can be attributed to the host. These are transcribed results, not new executions.

[Historical reports](../../docs/reports/) contain Hello World and Blog live-run analyses, token usage, timings and tool failure timelines. Versions and settings differ; they do not establish final-snapshot success or a controlled comparison.

Final report metrics and raw evidence are pending; see [missing items](../../MISSING_ITEMS.md).
