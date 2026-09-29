"""Batch function-call schema for independent specialist assignments."""

from langchain_core.tools import tool
from pydantic import BaseModel, Field, model_validator

from app.tools.delegate_task import Delegation


class DelegationBatch(BaseModel):
    assignments: list[Delegation] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def distinct_workers(self):
        workers = [assignment.worker for assignment in self.assignments]
        if len(workers) != len(set(workers)):
            raise ValueError("Each worker can appear only once in a parallel batch")
        return self


@tool(args_schema=DelegationBatch)
def delegate_tasks(assignments: list[Delegation]) -> str:
    """Assign independent tasks to specialists in parallel. Each assignment carries a private task, public summary, handoff, and context mode. Use only when their work does not depend on another assignment's result."""
    raise RuntimeError("delegate_tasks is executed by the team coordinator")
