"""Structured all-role handoff for starting a clean coordination context."""

from app.agents.tools.validation import tool
from pydantic import BaseModel, Field


class ContextCheckpoint(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    artifacts: list[str] = Field(default_factory=list, max_length=30)
    completed: list[str] = Field(default_factory=list, max_length=20)
    pending: list[str] = Field(default_factory=list, max_length=20)

    def as_prompt(self) -> str:
        def lines(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) or "- 无"

        return (
            "Previous coordination checkpoint. Keep these user constraints and project facts:\n"
            f"Goal: {self.goal}\n"
            f"Constraints:\n{lines(self.constraints)}\n"
            f"Artifacts and paths:\n{lines(self.artifacts)}\n"
            f"Completed:\n{lines(self.completed)}\n"
            f"Pending:\n{lines(self.pending)}"
        )


@tool(args_schema=ContextCheckpoint)
def checkpoint_context(
    goal: str,
    constraints: list[str],
    artifacts: list[str],
    completed: list[str],
    pending: list[str],
) -> str:
    """Record a faithful stage handoff before resetting your long conversation context. Include the boss's constraints, finished work, artifact paths, and unresolved tasks. Call alone at a milestone, not for greetings or routine replies."""
    return ContextCheckpoint(goal=goal, constraints=constraints, artifacts=artifacts,
                             completed=completed, pending=pending).as_prompt()
