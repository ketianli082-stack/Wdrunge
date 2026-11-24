# Final integration

This integration preserves every tracked file under `backend/` from `agent-enhanced` at `a02d55d`. It merges `feature/frontend-english-theme` at `9d99fbc`, which already contains the earlier frontend polish, website selection, batched annotations and static-resource refresh. The delivery branch is `feature/visual-feedback-zlj`; publishing to `main` is a separate decision.

## Compatibility

- The backend's execution response remains `running`, `stopping`, `paused`, `pending_tasks` and `active_goals`. Generated frontend types match the enhanced OpenAPI snapshot.
- The frontend reuses the collaboration snapshot for current-conversation/current-turn task detail. It uses those details only when they agree with the authoritative pending-task count; missing or lagging snapshots have an explicit fallback.
- Stopped or idle execution overrides stale task snapshots. Testing-tool activity alone is not delivery acceptance; the page-ready indication requires formally completed verification and related tasks.
- Annotation drafts, multi-selection, drag selection, floating comments, one-message batch submission, failed-send retention, navigation handling and cached-resource refresh remain frontend features using existing chat APIs.
- Chat pinning and priority are browser-local presentation preferences. They do not change backend scheduling.

## Verification

Run the frontend checks from `frontend/`:

```sh
npm run build
npm run test:messages
npm run test:characters
```

With Vite running, the browser regressions are `node scripts/visual-feedback.test.mjs` and `node scripts/preview-refresh.test.mjs`. They use isolated fixture pages and mocked model/backend requests; they do not establish real-model coding success rates.

Backend checks use the unchanged enhanced suite: `python -m pytest -q` from `backend/`. Native command sandbox and resource-limit tests depend on the supported host platform. Preserve platform-specific failures and skips when reporting results; do not describe an unsupported Windows executor as verified.

### Integration check results on 24 November 2026

- Host: Windows, Python 3.13.15, Node 24.12.0. The frontend used the already installed dependencies, including Vite 8.3.2.
- Production frontend build passed. The existing bundle-size warning remains.
- Message, execution-state compatibility and formal-acceptance checks: 20 passed.
- Character/office checks: 30 passed.
- Full browser annotation regression passed after the final compatibility changes. The cached CSS/JS refresh regression also passed. Both use isolated fixtures and no paid model calls.
- A real FastAPI smoke check with a temporary database passed: health, project/chat creation, the five-field execution response, collaboration snapshot and conversation lookup. It made no model calls or changes to the user's application database.
- Backend full suite: **487 passed, 84 failed, 25 skipped** in 202.83 seconds. The first failure was `test_auto_approval_reaches_process_execution_and_keeps_sandbox`, which reached Python's `pass_fds not supported on Windows` assertion. Other failures include process/path cases and pre-existing test assertions; they have not all been individually diagnosed. This is not a claim that every failure has the same platform cause.
- All backend files are identical to the pinned enhanced commit. The integration deliberately does not modify that backend to make the Windows suite pass. Full native execution and the remaining backend failures still require verification on a supported host before claiming a fully validated release.

To verify backend preservation against the pinned upstream commit:

```sh
git diff --exit-code a02d55d -- backend
```

The existing startup commands in the root and backend READMEs still apply. Use the existing model and service configuration; no new backend configuration or database migration is introduced by the frontend integration.
