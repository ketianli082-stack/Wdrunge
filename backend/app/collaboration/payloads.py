"""Validate delivery arguments before any publication side effects."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class RevisionReference(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class TransitionPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    identifier: str = Field(min_length=1)
    state: Literal['queued', 'blocked', 'review', 'completed', 'failed', 'cancelled', 'rejected']
    result: str = Field(default='', description='Plain text delivery details, not a JSON object or list')
    artifacts: list[str] | None = None
    based_on: list[RevisionReference] | None = None
    summary: str | None = Field(default=None, max_length=160)
