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


# Every action has a closed schema: malformed nested values and runtime-owned
# fields are rejected before a tool reads or mutates project state.
from typing import Annotated
from pydantic import create_model, model_validator

Text = Annotated[str, Field(min_length=1)]
Offset = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(ge=1)]
Role = Literal['leader', 'product', 'backend', 'frontend', 'designer', 'testing']


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)


class DocumentEdit(StrictPayload):
    operation: Literal['replace_text', 'replace_section_body', 'insert_section', 'delete_section']
    section_id: Text
    old_text: str = ''
    new_text: str = ''
    include_children: bool = False

    @model_validator(mode='after')
    def valid_edit(self):
        if self.operation == 'replace_text' and not self.old_text:
            raise ValueError('replace_text requires nonempty old_text')
        if self.operation == 'insert_section' and not self.new_text:
            raise ValueError('insert_section requires new_text')
        return self


class DocumentChange(StrictPayload):
    document_id: Text | None = None
    path: Text | None = None
    title: Text | None = None
    base_version: Offset = 0
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    content: Annotated[str, Field(max_length=500000)] | None = None
    edits: list[DocumentEdit] = Field(default_factory=list, max_length=100)
    based_on: list[RevisionReference] = Field(default_factory=list)
    allow_merge: bool = False
    external: bool = False

    @model_validator(mode='after')
    def valid_change(self):
        if not self.document_id and not self.path:
            raise ValueError('A new document requires path')
        if self.document_id and self.content is not None:
            raise ValueError('Existing documents require edits, not full content')
        if self.external and (self.content is not None or self.edits):
            raise ValueError('Import external content separately from edits')
        if not self.document_id and self.edits:
            raise ValueError('A new document requires content, not edits')
        return self


def schema(name, **fields):
    return create_model(name, __base__=StrictPayload, **fields)


Empty = schema('Empty')
Read = schema('Read', document_id=(Text, ...), version=(Version | None, None), section_id=(Text | None, None),
              offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=12000)], 6000), force=(bool, False))
Identifier = schema('Identifier', identifier=(Text, ...))
ACTION_PAYLOADS = {
    **{name: Empty for name in ('catalog', 'snapshot', 'dependencies', 'recover')},
    'read': Read,
    'publish': schema('Publish', changes=(Annotated[list[DocumentChange], Field(min_length=1, max_length=20)], ...)),
    'subscribe': schema('Subscribe', document_id=(Text, ...), enabled=(bool, True), section_id=(Text | None, None)),
    'send': schema('Send', recipient=(Role, ...), content=(Text, ...), key=(Text, ...), summary=(str | None, None), reply_to=(Text | None, None), request_id=(Text | None, None)),
    'request': schema('Request', recipient=(Role, ...), goal=(Text, ...), key=(Text, ...), summary=(str | None, None),
                      based_on=(list[RevisionReference], []), dependencies=(list[Text], []), artifacts=(list[Text], []),
                      priority=(Annotated[int, Field(ge=0, le=3)], 1), deadline=(float | None, None)),
    'inspect_request': Identifier,
    'inspect_message': schema('InspectMessage', identifier=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=8000)], 4000)),
    'wait': schema('Wait', dependencies=(list[Text], ...)),
    'transition': TransitionPayload,
    'search': schema('Search', query=(Annotated[str, Field(min_length=1, max_length=200)], ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=50)], 20)),
    'diff': schema('Diff', document_id=(Text, ...), from_version=(Version, ...), to_version=(Version, ...), section_id=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=12000)], 6000)),
    'schedule': schema('Schedule', identifier=(Text, ...), priority=(Annotated[int, Field(ge=0, le=3)] | None, None), deadline=(float | None, None)),
    'decision': schema('Decision', title=(Text, ...), rationale=(Text, ...), links=(list[RevisionReference], ...), facts=(dict[str, str] | None, None),
                       identifier=(Text | None, None), base_version=(Offset, 0), state=(Literal['proposed', 'accepted', 'superseded', 'rejected'], 'proposed'), supersedes=(Text | None, None)),
    'maintenance': schema('Maintenance', stale_days=(Annotated[int, Field(ge=1)], 90)),
    'handoff': schema('Handoff', save=(bool, False), summary=(str, '')),
    'history': schema('History', document_id=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=50)], 20)),
}


def validate_payload(action, payload):
    return ACTION_PAYLOADS[action].model_validate(payload).model_dump(exclude_unset=True, exclude_none=True)
