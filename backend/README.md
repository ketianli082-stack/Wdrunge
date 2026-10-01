# Backend

Python API for the Wdrunge desktop/web frontend. The chat endpoint accepts a JSON `POST` and returns Server-Sent Events. A Leader agent can answer directly or function-call a specialist when the user request needs project work.

## Run

```sh
cd backend
cp .env.example .env
# Set DASHSCOPE_API_KEY in .env
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for the API schema. The frontend runs separately with `cd frontend && npm run dev`.

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

Each delegation uses `context_mode`: `fresh` starts a clean worker thread, `continue` resumes that worker's thread in the same chat, and `auto` resumes while the latest reported input is below 60% of the configured context window. If usage is unavailable, `auto` starts fresh. If there is no saved thread, `continue` starts fresh. Every delegation also carries a private `handoff` with the goal, constraints, and relevant artifact paths. A fresh worker receives its role prompt, the current task, and this handoff; it does not receive the old chat transcript. The Leader may call `checkpoint_context` at a stage boundary to save a structured goal, constraints, artifacts, completed work, and pending work. Subsequent Leader calls in that chat use the checkpoint and later messages instead of older history.

`POST /api/chat/stream` remains available for compatibility, but it accepts a
client-provided transcript and does not provide project-scoped persistence. New
clients should use the conversation message endpoint above.

Set `QWEN_CONTEXT_WINDOW` to the actual context limit of the configured model. The default `131072` is an application assumption for the draft UI, not a verified property of every Qwen model. Context percentage uses the latest reported input-token count divided by that configured limit. Cumulative token counts use provider-reported usage; no number is invented when usage is missing.

`GET /api/health` returns `{"status":"ok"}`.

`GET /api/team` lists all six ready agents, their current status, tool names, and skill files.

## Team modules

- `app/agents/base.py` defines the reusable `Agent` and `AgentSpec` classes.
- `app/agents/leader.py` lets the Leader reply directly, function-call `delegate_task` or `delegate_tasks`, and save a `checkpoint_context`.
- `app/agents/worker.py` runs each specialist through a LangGraph tool loop with per-chat memory checkpoints.
- `app/agents/team.py` registers the six agents, executes delegation calls, and returns each worker's result to the Leader.
- `app/agents/prompts/` contains each role's system prompt; `app/agents/skills/` contains optional role skill instructions.
- `app/tools/` contains one Python module per tool. File tools are restricted to each role's assigned project paths.

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
