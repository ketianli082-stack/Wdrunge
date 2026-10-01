"""Function-call schema the Leader uses to assign one specialist task."""

from typing import Literal

from app.tools.validation import tool
from pydantic import BaseModel, Field


class Delegation(BaseModel):
    worker: Literal["product", "backend", "designer", "frontend", "testing"]
    task: str = Field(min_length=1, max_length=4000)
    summary: str = Field(min_length=1, max_length=80)
    context_mode: Literal["auto", "fresh", "continue"] = "auto"
    handoff: str = Field(min_length=1, max_length=4000)


@tool(args_schema=Delegation)
def delegate_task(worker: str, task: str, summary: str, handoff: str, context_mode: str = "auto") -> str:
    """Assign one task. The private task and handoff give the worker its goal, constraints, and relevant artifact paths; only the short summary appears in team chat. context_mode chooses auto, fresh, or continue. Reply directly when no specialist is needed."""
    raise RuntimeError("delegate_task is executed by the team coordinator")
