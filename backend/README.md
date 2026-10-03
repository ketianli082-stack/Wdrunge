# Backend

Python API for the Wdrunge desktop/web frontend. The chat endpoint accepts a JSON `POST` and returns Server-Sent Events. A Leader agent can answer directly or function-call a specialist when the user request needs project work.

## Run

```sh
cd backend
cp .env.example .env
# Set DASHSCOPE_API_KEY in .env
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 17420
```

Open `http://127.0.0.1:17420/docs` for the API schema. The frontend runs separately with `cd frontend && npm run dev`.

The agent calls Alibaba Cloud Model Studio through its OpenAI-compatible API. The default model is `qwen3.7-max`. Set `DASHSCOPE_API_KEY` in `.env`; `QWEN_MODEL` and `DASHSCOPE_BASE_URL` can be changed for another model or region. The default URL uses the China (Beijing) endpoint. After the first conversation turn completes, a separate `QWEN_TITLE_MODEL` call (default `qwen-flash`) extracts a title of at most 15 characters and emits a `title` event.

Run Uvicorn without `--reload` while an agent may edit backend files; automatic reload would interrupt the active SSE request.

FastAPI builds `/openapi.json` from the Pydantic request/response models. To update the checked-in schema and generated frontend types after changing an endpoint:

```sh
cd backend
uv run python -m scripts.export_openapi
cd ../frontend
npm run api:generate
```

The repository's `docs/` directory is reserved for project design documents; it is unrelated to FastAPI's `/docs` route.

## Projects, conversations, and persistence

The backend is the source of truth for chat data. A project owns one validated
workspace and contains multiple independent conversations. SQLite is stored in
the operating system's Wdrunge application-data directory by default; set
`WDRUNGE_DATABASE_PATH` to override it. Run `uv run alembic upgrade head` when
deploying a new database schema.

Relationships use indexed logical keys rather than SQLite `FOREIGN KEY`
constraints. `ChatStore` validates parents and performs related writes in one
transaction. Visible messages, assignment updates, read receipts, reactions,
tool activities, Leader checkpoints, worker session mappings, and LangGraph
checkpoints survive backend restarts.

The primary API flow is:

```text
POST /api/projects
GET  /api/projects
POST /api/projects/{project_id}/conversations
GET  /api/conversations/{conversation_id}
POST /api/conversations/{conversation_id}/messages/stream
```

The message stream accepts only the new content and a client-generated
idempotency key. The server loads prior model messages and the project's
workspace from SQLite.

## Legacy streaming contract

`POST /api/chat/stream` with `Content-Type: application/json`:

```json
{"conversation_id":"chat-123","workspace_path":"/absolute/path/to/project","messages":[{"role":"user","content":"What time is it?"}]}
```

The response uses `text/event-stream`. `status` events contain an agent ID and `idle`, `running`, or `failed` state. `chat_message` contains short public team updates; all assignments from one Leader batch share one multiline message with structured `mentions`. `read_receipt` marks one member's @mention as read; `message_reaction` adds that member's `done` reaction under the assignment message. `activity` reports tool calls without their arguments. `usage` reports cumulative model tokens and each member's latest prompt context occupancy. `delta` contains the Leader's answer; `done` marks completion; `error` contains a safe message if generation fails after streaming starts. Each request includes the full conversation history, an absolute existing `workspace_path`, and the same `conversation_id` for that chat. The frontend saves the workspace with each conversation locally. `POST /api/workspaces/validate` checks a directory before the conversation starts. File tools resolve paths inside that selected workspace. Separate user requests run one at a time, while independent members within a batch run concurrently.

Each delegation uses `context_mode`: `fresh` starts a clean worker thread, `continue` resumes that worker's thread in the same chat, and `auto` resumes an existing thread and applies layered context compression before each model call. If there is no saved thread, `auto` starts fresh. If there is no saved thread, `continue` starts fresh. Every delegation also carries a private `handoff` with the goal, constraints, and relevant artifact paths. A fresh worker receives its role prompt, the current task, and this handoff; it does not receive the old chat transcript. Every role may call `checkpoint_context` alone at a stage boundary to save a structured goal, constraints, artifacts, completed work, and pending work. Subsequent Leader calls in that chat use the checkpoint and later messages instead of older history.

`POST /api/chat/stream` remains available for compatibility, but it accepts a
client-provided transcript and does not provide project-scoped persistence. New
clients should use the conversation message endpoint above.

Set `QWEN_CONTEXT_WINDOW` to the actual context limit of the configured model. The default `131072` is an application assumption for the draft UI, not a verified property of every Qwen model. Context percentage uses the latest reported input-token count divided by that configured limit. Cumulative role token counts (including memory summaries) use persisted provider-reported usage and survive restarts; no number is invented when usage is missing.

`GET /api/health` returns `{"status":"ok"}`.

`GET /api/team` lists all six ready agents, their current status, tool names, and skill files.

## Team modules

- `app/agents/base.py` defines the reusable `Agent` and `AgentSpec` classes.
- `app/agents/leader.py` lets the Leader reply directly, function-call `delegate_task` or `delegate_tasks`, and save a `checkpoint_context`.
- `app/agents/worker.py` runs each specialist through a LangGraph tool loop with per-chat memory checkpoints.
- `app/agents/team.py` registers the six agents, executes delegation calls, and returns each worker's result to the Leader.
- `app/agents/prompts/` contains each role's system prompt; `app/agents/skills/` contains optional role skill instructions.
- `app/agents/tools/` contains one Python module per tool. File tools are restricted to each role's assigned project paths.

The Leader can select any subset of members and call a member more than once over successive turns. Independent assignments in `delegate_tasks` run concurrently; dependent work waits for their results. For example, designer and frontend can exchange refinements over several calls. Greetings need no delegation. All members are initialized as `idle` on API startup and become `running` only during their assignment.

## Project collaboration

The project collaboration panel displays managed Markdown documents, directed
messages, durable requests and delivery acceptance. Agents use `collaborate` to
read an outline, read versioned section bodies, subscribe, publish exact edits,
send idempotent messages, request assistance and suspend on dependencies.

`GET /api/projects/{id}/collaboration` is the durable project snapshot.
`POST /api/projects/{id}/collaboration/commands` executes a typed action envelope;
`POST .../pause` pauses new scheduling. This is a local single-user API, like the
existing chat API; the `member` field is a user-selected attribution, not remote
multi-user authentication. Agent identities are injected by the runtime.

A publication validates whole-document versions and explicit `based_on` document
versions. Multiple documents can publish as one logical batch. Committed SQLite
revision snapshots retain full history. A pending journal is saved before atomic
file replacement; recovery completes interrupted materialization before publishing
notifications. Files externally changed after journaling are preserved and block
publication for manual reconciliation. External edits to published documents are
reported by the catalog and can be imported with `external: true` and a summary.
Ordinary agent file writes cannot overwrite registered documents.

Reminders enter before each model call; worker receipts are persisted only after
the response checkpoint. Deliveries can repeat after a crash and carry stable
IDs. They are scoped to the member, context generation and source conversation.
Document subscriptions are project-wide. Request execution uses bounded background
slots, releases blocked slots, and survives browser disconnects. Agent results go
to `review`; the sender or user must accept them before they count as completed.
Dependent workers may wake for review to inspect and accept a child delivery.
Starting a persisted delegation queues durable work and awaits a reviewable result
without occupying a worker slot. Leader can accept it and continue dependent work;
paused/conflicted requests return their pending status. Disconnects leave requests
running; the project panel and next conversation can recover them. Legacy stateless chat
retains the original synchronous delegation behavior.

Context efficiency: `subscribe` optionally takes `section_id`; `search` returns
bounded literal keyword matches, and `diff` pages a section's version difference.
The CommonMark parser excludes quoted/code/HTML headings. Section reads prefer
whole Markdown blocks; oversized blocks carry `fragment: true` and a continuation
cursor. Repeated reads are suppressed only after successful model processing and
checkpointing, scoped to a context and version; `force: true` rereads explicitly.
Reminder aggregation preserves every included revision summary and event ID.

Scheduling: requests accept priority 0–3 (highest first, FIFO within a priority)
and an optional Unix deadline. A missed deadline produces one durable Leader
escalation and raises priority once; it does not silently cancel work. `schedule`
updates these fields. `dependencies` exposes nodes and directed prerequisite edges.
Cancellation propagates to dependent requests and children; unrelated work remains
active. The scheduler cancels affected running jobs, and tool/model boundaries check
request state before continuing. Already completed filesystem writes are retained.

Knowledge maintenance: `decision` records versioned proposals and published
revision references. Only Leader can accept/reject/supersede a decision; accepted
decisions require current published evidence. `facts` uses scoped keys such as
`auth.token_field` so competing accepted values can be flagged without guessing.
`maintenance` reports changed dependencies, aged documents, normalized duplicate
section bodies and contradictory explicit decision facts. It is not a general
semantic proofreader and never rewrites documents automatically. `handoff` creates
a bounded project bootstrap with current references, accepted decisions and open
requests; `save: true` retains a stage snapshot. Leader context checkpoints also
save these snapshots. Document changes notify affected requests even without a
subscription, and mark their basis as needing review.

Concurrent execution: `WDRUNGE_ROLE_CONCURRENCY` defaults to 2 instances per role
per project; `WDRUNGE_MAX_CONCURRENCY` defaults to 4 active jobs across this database.
Claims use SQLite write transactions and 60-second execution leases, renewed every
10 seconds. Restart only reclaims expired leases, so another live scheduler is not
interrupted. Every execution gets an instance ID and uses a persistent snapshot
under `.wdrunge-runs/<workspace_id>/work`; suspended work retains local edits and
pulls new dependency artifacts into unchanged files before resuming.

Snapshots exclude Git internals, secrets directories, environments, dependency
packages and build outputs. Existing `node_modules` directories are shared through
links for verification; agent file tools cannot write through those external links.
Snapshot limits are 128 MiB and 10,000 source files; oversized workspaces fail with
an explicit message. This is source-file isolation, not a security sandbox for
arbitrary executable project code. Snapshots are retained for recovery and manual
inspection; no background cleanup deletes user work.

Artifact delivery checks dependencies and required paths before publishing. Text
changes use conservative three-way merging; overlapping edits, binary conflicts,
concurrent creation/deletion and managed-document bypasses block delivery while
retaining the run workspace. Multi-file publication has a recovery journal. A
retry must reconcile conflicts; cancellation never rolls back already published
files. For managed documents, `allow_merge: true` permits stale-version patches
only when the edited sections are unchanged and explicit dependencies still match.
This proves textual independence, not semantic compatibility across sections;
keep coupled changes in one batch and declare `based_on` requirements.

Current section bodies have a project-indexed search projection rebuilt on startup
when needed. WAL reads use separate read transactions without taking the writer
mutex. Message `request_id` targets a specific role instance's request, avoiding
cross-talk between concurrent tasks. External requests remain project/conversation
scoped. Cancelled verification commands terminate their process groups.


Delivery documents: the “交付文档” panel browses the project's actual workspace,
including unregistered files and empty folders. `/api/projects/{id}/delivery/tree`
returns a bounded directory snapshot; `/events` pushes native filesystem updates
via SSE (100 ms debounce, 1-second reconciliation). Clients reconnect automatically
and refresh the open preview on changes. Disconnects release the file watcher.
`/preview?path=...` renders Markdown, UTF-8 text (up to 2 MiB), images or PDFs;
`/file?path=...` serves the resource, with `download=true` for downloads. The preview
covers the 3D scene without unmounting it; closing returns to the scene.

This browser excludes hidden paths, symlinks, dependency/build directories and
limits listings to 10,000 entries. Paths stay within the selected project. HTML and
unsupported resources are downloads, not executable pages. Raw responses disable
caching and use a sandbox CSP. File previews reflect current disk contents;
registered document revision history remains under team chat → “任务与协作” →
“项目记录与文档维护”. The delivery tab only shows the live file tree and previews;
assignments, replies, acceptance/retry controls and collaboration details belong
to team chat.

Specialist graphs default to 1,000 LangGraph steps per execution; override with
`WDRUNGE_GRAPH_RECURSION_LIMIT`. Steps include model, tool and acknowledgment nodes,
so this is not a count of tool calls. A failed request can be explicitly requeued
by its sender or Leader (also via “重试原任务” in the panel). Retried requests retain
their ID, dependencies and unpublished workspace; they are not duplicated or
silently restarted. Dependencies resume once their prerequisites are reviewable.

Conversation activity and team messages also have a cursor-based durable feed:
`GET /api/conversations/{id}/events?after=0&limit=200`. The UI consumes this feed
while Leader SSE is open, so background workers remain visible during delegation.
Pages replay safely, preserve streaming assistant text and retain tool start times.
Leader collaboration/acceptance tools appear on the same timeline.

Collaboration notifications are public group mentions with bounded summaries:
`send` and `request` accept `summary` (1–160 characters); delivery/acceptance
`transition` accepts a reply summary. Full `content`, `goal` and `result` stay in
collaboration records. Notification persistence and chat insertion share one SQLite
transaction; idempotent sends do not duplicate chat. Legacy calls without summaries
use a short generic notice and never copy private arguments into the public bubble.
Read receipts reflect consumed reminders/started tasks; Done appears only after
acceptance, including acceptance through the UI. Old records remain available in
the collaboration details panel.

Role prompts route intent and define direct contract/design handoffs. Designer
owns style/layout guidance and assets; frontend owns page implementation. Testing
also has `run_service_check`: it starts standard FastAPI/Vite on a free localhost
port, probes a requested HTTP path, captures bounded logs and stops its own process
group on success, failure or cancellation. Wdrunge smoke tests use a temporary DB.
This does not install dependencies, automate a browser or deploy a production app;
unsupported startup layouts must be reported and coordinated explicitly.


### Debug record reset

The sidebar's **清除所有记录** button asks for confirmation, then calls
`POST /api/debug/records/reset` with `{"confirmation":"CLEAR_ALL_RECORDS"}`.
In this local, single-process debugging setup, reset blocks new API work, drains
in-flight requests and scheduler tasks (including cancellation cleanup), clears
application/collaboration/checkpoint rows in one transaction, resets model memory
and token counters, and restarts the scheduler. A disconnected caller does not
interrupt an accepted reset. The UI then opens an empty project list.

All projects, conversations, messages, logs, subscriptions, document revision
metadata and collaboration requests are removed. Actual workspace files (including
deliverables and isolated workspaces), configuration and database schema remain.
Set `WDRUNGE_ENABLE_DEBUG_RESET=0` to disable the endpoint outside debugging.
Do not enable this local debug operation across multiple API worker processes.

Collaboration transitions validate `result` as plain text before publishing files
or changing state. Malformed tool payloads return a correction error to the agent
instead of terminating the whole request.

All agent tools now reject unknown arguments and incorrect types. Every
`collaborate` action validates its nested payload before project I/O; coordinator
and worker tool boundaries return failed tool results so the model can correct a
call. Cancellation still propagates. Legacy request results stored as JSON
objects/lists load as JSON text, preserving their delivery evidence for acceptance.
Unexpected tool exceptions remain logged for diagnosis.

Token snapshots include live scheduler instances after each model usage report,
without double counting them on completion/cancellation. The UI polls every three
seconds. Concurrent contexts are separate windows; a role displays the largest
active context instead of summing unrelated windows. No estimated tokens are shown
before the provider returns usage.

`write_project_file` now accepts either complete `content` or partial
`edits: [{old_text, new_text}]`, never both. Each nonempty old_text must match
exactly once in the current file. Edits apply sequentially in memory and commit
atomically only after the entire batch succeeds. Per-path locks serialize agent
writes; unrelated edits use the latest file contents. Optional `expected_hash`
rejects a stale full-file version. `read_project_file` accepts `offset`, `limit`
and `include_hash=true` to read larger files in parts and obtain that SHA-256.
Managed Markdown still requires `collaborate publish`. These are exact text
replacements, not Git patch syntax. External editors do not participate in the
process-local lock; a hash/content check detects changes observed before replace.


### Asynchronous Leader coordination

Persistent project delegations enqueue work and return immediately. Leader ends
its current turn after handling ready decisions and displays “等待消息” when work
remains. The scheduler observes the durable Leader inbox without making model
calls while idle. Mentions, delivery and escalation events start serialized
background turns independent of the browser's SSE connection. Each turn uses the
saved human/assistant history plus current task handoff and unconsumed reminders.
After replying/accepting, Leader returns to waiting if tasks remain, or idle.

Read receipts are emitted after a successful model response, including tool-call
responses. Inbox consumption remains deferred until the final reply is persisted:
a crash can replay unfinished coordination without misrepresenting “read” as
“resolved.” Restart restores history and pending events but pauses unfinished
work. Reopening the app also pauses work in a still-running backend. Only the
current conversation's “继续任务” button resumes its saved tasks; other conversations
remain paused. Sending a new message does not release old paused tasks. Failures
retry with backoff up to three times, then wait for user intervention; a new user
turn resets this failure state. Project pause and debug reset stop automatic work.


While unfinished tasks remain and Leader is resting, 180 seconds without a wake
triggers a short group progress inquiry and one runtime progress snapshot per
assigned role. Further checks are spaced by 180 seconds, and an actual wake resets
the clock. Timestamps persist across restarts; paused projects and active Leader
turns skip checks. Completed work does not generate progress inquiries.

These exchanges have message kind `progress` and no model role. They appear in the
UI and work record but never enter model history, collaboration inboxes, handoffs
or agent checkpoints. Replies explicitly report a runtime snapshot (task states
and latest tool activity), not an invented completion percentage or an extra
model conversation. A reviewable delivery or explicit blocker found by a check
creates a separate durable attention event so Leader can make a real decision.

### Tool-call reliability and context boundaries

Failed tool calls retain the legacy `error` text and now include `error_details`
with `category`, `code`, `retry_policy` and `next_action`. Parameter validation
adds bounded `fields` entries with the exact field path, actual JSON type,
expected schema and an illustrative example. Submitted file bodies and secret
values are not copied into validation diagnostics. Arrays and objects remain
strictly typed; JSON-encoded strings are rejected rather than silently coerced.

Leader tool calls are not stored in LangGraph checkpoints, so each call's arguments and result (8,000 characters each) are logged once per conversation and call ID in the `tool_calls` table, capped at the newest 5,000 rows and cleared by record reset. `refresh_api_contract` and `refresh_frontend_client` are shown to models only when the task workspace has `backend/scripts/export_openapi.py` or a frontend `api:generate` script; other projects use their own commands.

Worker ToolMessages mark structured failures as `status=error`, including tools
that return failure dictionaries instead of raising. Leader validation and
collaboration tools use the same feedback contract. Explicit command diagnostics
distinguish refused connections, occupied ports, OS permission denial, timeouts
and other nonzero exits while retaining the original logs/exit status. Browser
feedback identifies the failing stage/action and separates service navigation
failures, page verification failures and tool/runtime defects. Vite HMR WebSockets
use Playwright's synchronous `connect_to_server` method; other origins remain
blocked. These recovery hints do not grant permissions or trigger automatic retries.

Models now receive one `collaboration_*` tool per action, with the action's actual
Pydantic schema. For example, `collaboration_publish` takes `changes` directly and
`collaboration_read` takes `document_id`, `version` and optional `section_id`.
There is no `action`/`payload` wrapper on these model-facing tools. The original
`collaborate` entry point remains available to API clients and saved calls. Worker
models do not receive Leader-only recovery, or `wait` outside a durable request.
All calls still pass through the same runtime identity, lease and path checks.

For document edits, `replace_section_body` preserves the following heading's line
boundary even if the replacement omits its final newline. Both body replacement
and exact-text replacement reject changes that unexpectedly remove or reparent
existing headings, including unclosed Markdown fences swallowing later sections.
Failures identify the zero-based edit index, operation and section; no revision or
file is committed when a batch fails. Edits still execute sequentially: publish a
structural change and reload its outline before edits that depend on the new
structure. Replacement text must be explicit; omitting it cannot silently delete
content. Updates to existing documents require an explicit positive base version.

Tool errors name invalid fields without echoing the full submitted document.
Missing/non-text files and forbidden managed-document file writes raise tool
errors. Project checks and code generators return `passed`, `exit_code` and
`output`; unavailable operations return an error. The activity timeline therefore
records these failures rather than treating a normal function return as success.

Read deduplication belongs to the model history containing the body. Leader
checkpointing now rotates its read context when discarding tool messages. Worker
read contexts already rotate with fresh sessions. Handoffs include document paths
and remain valid JSON within the context budget: whole records are omitted with
explicit `remaining` counts, rather than cutting an ID or JSON object in half.
Worker graph caching also accounts for changes in available collaboration tools.

#### Root-cause audit, 2026-10-01

The audit used read-only local activity records and retained LangGraph tool
calls/results, plus offline replay of the actual document edits. Checkpoint
samples span multiple code versions and do not cover every Leader call; they are
not an estimate of the current release's steady-state failure rate.

- In the activity sample after 21:00 Hong Kong time, publication failed 9/16 times
  and section reads failed 3/95 times. Five publication failures were malformed
  parameters; two lost a target section during preceding edits; two tried matching
  text in a section whose boundary had changed earlier in the same batch.
- The model-facing legacy schema exposed `payload` as an arbitrary object, while
  runtime validation enforced nested fields and enums. Recorded guesses included
  missing `changes`, unsupported `replace`/`insert_after`, misplaced document IDs
  or base versions, and missing message idempotency keys. Typed action schemas and
  explicit examples address the shared cause, rather than adding blind retries.
- Offline replay reproduced replacement text joining the next heading. A later
  saved revision joined a closing code fence to a heading; its outline fell from
  52 entries to 34. The three failed read targets remained in raw Markdown but
  were no longer indexed as headings. `force=true` only bypasses read caching and
  cannot repair this structure.
- Retained file-tool calls included six managed-Markdown rejections previously
  returned as ordinary strings. Two malformed file edits sent the edit array as
  a JSON string; the old exception formatter echoed so much input that truncation
  obscured the actual type error. Failure status and bounded diagnostics now
  handle these cases explicitly.
- Two worker calls attempted Leader-only publication recovery. Recovery does not
  fix schema or matching failures; it is now omitted from worker model tools and
  its limited purpose is documented.
- Code inspection additionally found checkpoint/read-cache lifetime mismatch and
  raw handoff JSON truncation. These are regression-tested correctness fixes;
  the recorded three section-read failures were caused by document structure,
  not attributed to these context defects.
- Three retained graph-level failures were provider `Arrearage` responses. These
  are model-account availability errors, separate from tool execution; no local
  document-tool change can resolve provider account billing.

Tests cover model-visible schemas, runtime typed dispatch, legacy compatibility,
atomic publication, Markdown boundaries, diagnostic truncation, command failure
status, context reset and complete handoff records. No paid provider calls are
required for these regressions. Existing damaged document revisions are retained;
code changes do not rewrite previously published project documents.

## Persistent usage and layered context memory

All six roles now share automatic folding, historical summarization and clean-context handoffs; each can also call `checkpoint_context` explicitly. Usage totals survive process restarts, and compression ranges are retained for inspection. See [context-memory.md](../docs/backend/context-memory.md) for budgets, persistence, failure behavior and accounting scope.

开发执行工具、系统沙箱与单次/持久授权说明见 [开发工具与执行权限](../docs/execution-tools.md)。

### Goal 与长期任务

持久化 Goal、Task 工具、验收条件和 reminder 驱动的恢复已接入协作运行时。
详见 [Goal 与长期任务](../docs/goals.md)。界面入口为群聊的“任务与协作”。
