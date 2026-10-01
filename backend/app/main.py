"""HTTP and SSE boundary for the Wdrunge agent team."""

import asyncio
import json
import logging
import os
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field, field_serializer, model_validator

from app.record_reset import RecordsGate, RecordsGateMiddleware
from app import conversation_title
from app.collaboration.service import Collaboration
from app.collaboration.runtime import Scheduler
from app.collaboration import api as collaboration_api
from app.collaboration.delivery import router as delivery_router
from app.agents.team import get_team
from app.storage import ChatStore, StorageConflictError, StorageNotFoundError, get_store

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logger = logging.getLogger(__name__)
store: ChatStore = get_store()
_checkpoint_connection: aiosqlite.Connection | None = None
_runtime_store_identity: int | None = None
_collaboration_scheduler = None
_records_gate = RecordsGate()


async def ensure_runtime() -> None:
    """Initialize application tables and the durable LangGraph checkpointer."""
    global _checkpoint_connection, _runtime_store_identity, _collaboration_scheduler
    await store.initialize()
    if _runtime_store_identity == id(store):
        return
    if _collaboration_scheduler is not None:
        await _collaboration_scheduler.close()
        _collaboration_scheduler = None
    if _checkpoint_connection is not None:
        await _checkpoint_connection.close()
    checkpoint_path = str(store.sqlite_path) if store.sqlite_path else ":memory:"
    _checkpoint_connection = await aiosqlite.connect(checkpoint_path)
    checkpointer = AsyncSqliteSaver(_checkpoint_connection)
    await checkpointer.setup()
    team = get_team()
    if hasattr(team, "configure_persistence"):
        await team.configure_persistence(store, checkpointer)
    collaboration = Collaboration(store)
    await collaboration.initialize()
    collaboration_api.configure(collaboration)
    if hasattr(team, "configure_persistence"):
        team.collaboration = collaboration
        team._checkpointer = checkpointer
        _collaboration_scheduler = Scheduler(collaboration, team)
        await _collaboration_scheduler.start()
    _runtime_store_identity = id(store)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _checkpoint_connection, _runtime_store_identity, _collaboration_scheduler
    await ensure_runtime()
    app.state.team = get_team()  # All six agents are ready and idle at startup.
    try:
        yield
    finally:
        if _records_gate.reset_task is not None:
            await asyncio.shield(_records_gate.reset_task)
        if _collaboration_scheduler is not None:
            await _collaboration_scheduler.close()
            _collaboration_scheduler = None
        if _checkpoint_connection is not None:
            await _checkpoint_connection.close()
            _checkpoint_connection = None
        _runtime_store_identity = None
        await store.close()


app = FastAPI(
    title="Wdrunge API",
    description="Agent team API. Requests are validated with Pydantic; work streams as SSE.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(collaboration_api.router)
app.include_router(delivery_router)
app.add_middleware(RecordsGateMiddleware, gate=_records_gate)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://tauri.localhost",
        "tauri://localhost",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ResetRecordsRequest(BaseModel):
    confirmation: Literal['CLEAR_ALL_RECORDS']


class ResetRecordsResponse(BaseModel):
    cleared: bool
    workspace_files_preserved: bool


@app.post('/api/debug/records/reset', response_model=ResetRecordsResponse, tags=['Debug'])
async def reset_records(body: ResetRecordsRequest):
    """Stop local work and clear all app records, preserving project workspaces."""
    if os.getenv('WDRUNGE_ENABLE_DEBUG_RESET', '1') != '1':
        raise HTTPException(404, 'Debug reset is disabled')
    if _records_gate.resetting:
        raise HTTPException(409, '记录清除正在进行中')
    _records_gate.resetting = True
    # Keep reset alive if its HTTP caller disconnects after confirming.
    task = asyncio.create_task(_clear_all_records())
    _records_gate.reset_task = task
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(task)


async def _clear_all_records():
    global _checkpoint_connection, _runtime_store_identity, _collaboration_scheduler
    try:
        await _records_gate.drain()
        if _collaboration_scheduler is not None:
            await _collaboration_scheduler.close()
            _collaboration_scheduler = None
        if _checkpoint_connection is not None:
            await _checkpoint_connection.close()
            _checkpoint_connection = None
        _runtime_store_identity = None
        try:
            await store.clear_records()
            team = get_team()
            if hasattr(team, 'clear_runtime_memory'):
                team.clear_runtime_memory()
        finally:
            await ensure_runtime()
        return {'cleared': True, 'workspace_files_preserved': True}
    finally:
        _records_gate.resetting = False
        _records_gate.reset_task = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=120)
    workspace_path: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def last_message_is_user(self):
        if self.messages[-1].role != "user":
            raise ValueError("The last message must be from the user")
        return self


class HealthResponse(BaseModel):
    status: Literal["ok"]


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceResponse(BaseModel):
    path: str


class UtcTimestampResponse(BaseModel):
    @field_serializer("created_at", "updated_at", check_fields=False)
    def serialize_utc_timestamp(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()


class ConversationSummaryResponse(UtcTimestampResponse):
    id: str
    project_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    workspace_path: str = Field(min_length=1, max_length=4096)


class ProjectResponse(UtcTimestampResponse):
    id: str
    name: str
    workspace_path: str
    created_at: datetime
    updated_at: datetime
    conversations: list[ConversationSummaryResponse] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=240)


class PersistedMessageResponse(UtcTimestampResponse):
    id: str
    sender: str
    content: str
    kind: str
    status: str
    sequence: int
    mention: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    read_by: list[str] = Field(default_factory=list)
    completed_by: list[str] = Field(default_factory=list)
    created_at: datetime


class PersistedActivityResponse(UtcTimestampResponse):
    id: str
    agent: str
    tool: str
    description: str
    phase: str
    sequence: int
    created_at: datetime


class ConversationEventResponse(UtcTimestampResponse):
    sequence: int
    type: str
    payload: dict
    created_at: datetime


class ConversationEventsResponse(BaseModel):
    events: list[ConversationEventResponse]
    cursor: int
    has_more: bool


class ConversationDetailResponse(ConversationSummaryResponse):
    project_name: str
    workspace_path: str
    messages: list[PersistedMessageResponse]
    activities: list[PersistedActivityResponse]


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=80)


def resolve_workspace(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="Workspace must be an absolute directory path")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=422, detail="Workspace directory does not exist")
    return resolved


class TeamMemberResponse(BaseModel):
    id: str
    name: str
    status: Literal["idle", "running", "waiting", "failed"]
    tools: list[str]
    skills: list[str]


class AgentUsageResponse(BaseModel):
    id: str
    total_tokens: int
    context_tokens: int
    context_window: int
    context_percent: float


class TeamUsageResponse(BaseModel):
    total_tokens: int
    agents: list[AgentUsageResponse]


class TeamStatusResponse(BaseModel):
    agents: list[TeamMemberResponse]
    usage: TeamUsageResponse


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health():
    return HealthResponse(status="ok")


@app.post("/api/workspaces/validate", response_model=WorkspaceResponse, tags=["System"])
async def validate_workspace(body: WorkspaceRequest):
    return WorkspaceResponse(path=str(resolve_workspace(body.path)))


def storage_http_error(error: Exception) -> HTTPException:
    if isinstance(error, StorageNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, StorageConflictError):
        return HTTPException(status_code=409, detail=str(error))
    raise error


@app.get("/api/projects", response_model=list[ProjectResponse], tags=["Projects"])
async def list_projects():
    await ensure_runtime()
    return await store.list_projects()


@app.post("/api/projects", response_model=ProjectResponse, status_code=201, tags=["Projects"])
async def create_project(body: ProjectCreate):
    await ensure_runtime()
    workspace = resolve_workspace(body.workspace_path)
    name = (body.name or workspace.name).strip()
    try:
        project = await store.create_project(name, str(workspace))
    except (StorageNotFoundError, StorageConflictError) as error:
        raise storage_http_error(error) from error
    return {**project, "conversations": []}


@app.post(
    "/api/projects/{project_id}/conversations",
    response_model=ConversationSummaryResponse,
    status_code=201,
    tags=["Projects"],
)
async def create_conversation(project_id: str, body: ConversationCreate):
    await ensure_runtime()
    try:
        return await store.create_conversation(project_id, body.title)
    except StorageNotFoundError as error:
        raise storage_http_error(error) from error


@app.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    tags=["Projects"],
)
async def conversation_detail(conversation_id: str):
    await ensure_runtime()
    try:
        return await store.get_conversation_detail(conversation_id)
    except StorageNotFoundError as error:
        raise storage_http_error(error) from error


@app.get("/api/conversations/{conversation_id}/events", response_model=ConversationEventsResponse, tags=["Projects"])
async def conversation_events(conversation_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
    try:
        return await store.conversation_event_page(conversation_id, after, limit)
    except StorageNotFoundError as error:
        raise storage_http_error(error) from error


@app.get("/api/team", response_model=TeamStatusResponse, tags=["Agent"])
async def team_status():
    """List every team member and its current state and capabilities."""
    team = get_team()
    return TeamStatusResponse(agents=team.snapshot(), usage=team.usage_snapshot())


@app.post(
    "/api/conversations/{conversation_id}/messages/stream",
    response_class=StreamingResponse,
    tags=["Projects"],
    summary="Persist a message and stream the project team's response",
    responses={
        200: {
            "description": "Server-Sent Events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        404: {"description": "Conversation not found"},
        409: {"description": "Duplicate client message"},
        503: {"description": "DASHSCOPE_API_KEY is not configured"},
    },
)
async def conversation_message_stream(
    conversation_id: str,
    body: MessageCreate,
    request: Request,
):
    await ensure_runtime()
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise HTTPException(status_code=503, detail="DASHSCOPE_API_KEY is not configured")
    try:
        workspace = resolve_workspace(await store.conversation_workspace(conversation_id))
        await store.add_user_message(conversation_id, body.content, body.client_message_id)
        stored_history = await store.model_history(conversation_id)
        initial_title = (await store.get_conversation_summary(conversation_id))["title"]
    except (StorageNotFoundError, StorageConflictError) as error:
        raise storage_http_error(error) from error

    history = [
        HumanMessage(content=item["content"])
        if item["role"] == "user"
        else AIMessage(content=item["content"])
        for item in stored_history
    ]
    team = get_team()
    is_initial_turn = len(stored_history) == 1 and initial_title == "新对话"
    reply_id = uuid4().hex

    async def events():
        reply = ""
        reply_started = False
        persisted_length = 0
        completed = False
        try:
            async with aclosing(
                team.stream(history, conversation_id=conversation_id, workspace_path=workspace)
            ) as team_events:
                async for event_name, payload in team_events:
                    if await request.is_disconnected():
                        return
                    if event_name == "chat_message":
                        await store.add_chat_message(conversation_id, payload)
                    elif event_name == "read_receipt":
                        await store.add_receipt(conversation_id, payload)
                    elif event_name == "message_reaction":
                        await store.add_reaction(conversation_id, payload)
                    elif event_name == "activity":
                        await store.upsert_activity(conversation_id, payload)
                    elif event_name == "delta":
                        reply += payload.get("content", "")
                        if not reply_started:
                            await store.start_assistant_message(conversation_id, reply_id, reply)
                            reply_started = True
                            persisted_length = len(reply)
                        elif len(reply) - persisted_length >= 512:
                            await store.update_assistant_message(reply_id, reply)
                            persisted_length = len(reply)
                        payload = {**payload, "message_id": reply_id}
                    yield sse(event_name, payload)
            if reply_started:
                await store.update_assistant_message(reply_id, reply, "completed")
                if hasattr(team, 'acknowledge_leader'):
                    await team.acknowledge_leader(conversation_id)
                await store.add_event(
                    conversation_id,
                    "assistant_completed",
                    {"id": reply_id, "content_length": len(reply)},
                )
            completed = True
            yield sse("done", {"message_id": reply_id if reply_started else None})
            if is_initial_turn and reply_started:
                try:
                    title = conversation_title.clean_title(await asyncio.wait_for(
                        conversation_title.generate_title(body.content, reply), timeout=20
                    ))
                    if title:
                        await store.set_initial_title(conversation_id, title)
                        yield sse("title", {"title": title})
                except Exception:
                    logger.exception("Could not generate initial conversation title")
        except asyncio.CancelledError:
            if reply_started and not completed:
                await store.update_assistant_message(reply_id, reply, "cancelled")
            raise
        except Exception:
            logger.exception("Persistent agent team stream failed")
            if reply_started:
                await store.update_assistant_message(reply_id, reply, "failed")
            await store.add_event(conversation_id, "error", {"message": "Team request failed"})
            yield sse("error", {"message": "Team request failed; check backend logs"})
        finally:
            if reply_started and not completed and await request.is_disconnected():
                await store.update_assistant_message(reply_id, reply, "cancelled")

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post(
    "/api/chat/stream",
    response_class=StreamingResponse,
    tags=["Agent"],
    summary="Stream a leader-coordinated team response",
    description=(
        "Send the full conversation, ending with a user message, and the absolute `workspace_path` "
        "selected for this conversation. Supply the same `conversation_id` "
        "to continue worker sessions and Leader checkpoints; omit it for an isolated request. "
        "`status` events report agent state; `chat_message` contains short team updates; "
        "`read_receipt` marks an @mention as read; `activity` describes tool use without arguments; "
        "`usage` reports cumulative tokens and each agent's latest prompt context occupancy; "
        "`delta` contains the Leader's reply; `done` marks completion. "
        "Failures after streaming begins emit `error` with `{message}`."
    ),
    responses={
        200: {
            "description": "Server-Sent Events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        503: {"description": "DASHSCOPE_API_KEY is not configured"},
    },
)
async def chat_stream(
    body: ChatRequest,
    request: Request,
):
    workspace = resolve_workspace(body.workspace_path)
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise HTTPException(status_code=503, detail="DASHSCOPE_API_KEY is not configured")
    team = get_team()

    messages = [
        HumanMessage(content=item.content)
        if item.role == "user"
        else AIMessage(content=item.content)
        for item in body.messages
    ]

    async def events():
        try:
            async with aclosing(team.stream(messages, conversation_id=body.conversation_id, workspace_path=workspace)) as team_events:
                async for event, payload in team_events:
                    if await request.is_disconnected():
                        return
                    yield sse(event, payload)
            yield sse("done", {})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent team stream failed")
            yield sse("error", {"message": "Team request failed; check backend logs"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
