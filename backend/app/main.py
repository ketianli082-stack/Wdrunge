"""HTTP and SSE boundary for the Wdrunge agent team."""

import asyncio
import json
import logging
import os
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, model_validator

from app.agents.team import get_team

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.team = get_team()  # All six agents are ready and idle at startup.
    yield


app = FastAPI(
    title="Wdrunge API",
    description="Agent team API. Requests are validated with Pydantic; work streams as SSE.",
    version="0.1.0",
    lifespan=lifespan,
)
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
    status: Literal["idle", "running", "failed"]
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


@app.get("/api/team", response_model=TeamStatusResponse, tags=["Agent"])
async def team_status():
    """List every team member and its current state and capabilities."""
    team = get_team()
    return TeamStatusResponse(agents=team.snapshot(), usage=team.usage_snapshot())


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
