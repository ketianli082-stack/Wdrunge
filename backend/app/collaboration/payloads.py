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
    evidence: dict[str, str] | None = None
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
    section_id: Text = Field(description='Exact outline ID')
    old_text: str = ''
    new_text: str = ''
    include_children: bool = False

    @model_validator(mode='after')
    def valid_edit(self):
        if self.operation in {'replace_text', 'replace_section_body', 'insert_section'} and 'new_text' not in self.model_fields_set:
            raise ValueError(f'{self.operation} requires explicit new_text; use an empty string only for intentional removal')
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
    ready: bool = Field(False, description='Mark the document complete enough for tasks waiting on it; they start now '
                                           'instead of when you submit your task')

    @model_validator(mode='after')
    def valid_change(self):
        if self.document_id and ('base_version' not in self.model_fields_set or self.base_version < 1):
            raise ValueError('Existing documents require the base_version read from their outline')
        if not self.document_id and not self.path:
            raise ValueError('A new document requires path')
        if self.document_id and self.content is not None and self.edits:
            raise ValueError('Send content (replacing a draft) or edits, not both')
        if self.external and (self.content is not None or self.edits):
            raise ValueError('Import external content separately from edits')
        if not self.document_id and self.edits:
            raise ValueError('A new document requires content, not edits')
        if not self.document_id and self.base_version != 0:
            raise ValueError('New documents require base_version=0; to update a managed path use its document_id')
        return self


class Publish(StrictPayload):
    changes: Annotated[list[DocumentChange], Field(min_length=1, max_length=20)]
    summary: Annotated[str, Field(min_length=1, max_length=2000)] | None = Field(
        None, description='Default summary for changes that omit one')
    base_version: Offset | None = Field(None, description='Default base_version when there is exactly one change')

    @model_validator(mode='before')
    @classmethod
    def change_defaults(cls, data):
        """Models often put change fields (summary, base_version, ready, based_on) beside
        changes instead of inside each change; apply them where unambiguous rather than
        failing the whole call. ready and based_on are accepted here without being
        advertised in the schema, which every call resends."""
        if not isinstance(data, dict) or not isinstance(data.get('changes'), list):
            return data
        data = dict(data)
        extra = {key: data.pop(key) for key in ('ready', 'based_on') if key in data}
        version = data.get('base_version')
        if isinstance(version, str) and version.isdigit():
            version = int(version)
        defaults = {key: value for key, value in {'summary': data.get('summary'), **extra}.items() if value is not None}
        if not defaults and version is None:
            return {**data, 'changes': fold_edits(data['changes'])}
        changes = data['changes']
        if version is not None and len(changes) != 1:
            raise ValueError('Top-level base_version is ambiguous with several changes; put base_version in each change')
        folded = []
        for change in changes:
            if isinstance(change, dict):
                change = {**defaults, **change}
                if version is not None:
                    if 'base_version' in change and change['base_version'] != version:
                        raise ValueError('Top-level base_version differs from the change base_version')
                    change.setdefault('base_version', version)
            folded.append(change)
        return {**data, 'changes': fold_edits(folded), 'summary': None, 'base_version': None}


def fold_edits(changes):
    """Models often split edits to one document into several changes. Edits apply in
    order, so changes made from the same version fold into one; anything else is left
    for publish to reject with guidance."""
    folded, by_document = [], {}
    for change in changes:
        key = change.get('document_id') if isinstance(change, dict) else None
        first = by_document.get(key)
        if (first is not None and change.get('content') is None and first.get('content') is None
                and not change.get('external') and not first.get('external')
                and change.get('base_version') == first.get('base_version')
                and bool(change.get('allow_merge')) == bool(first.get('allow_merge'))
                and isinstance(change.get('edits', []), list) and isinstance(first.get('edits', []), list)):
            first['edits'] = [*first.get('edits', []), *change.get('edits', [])]
            if change.get('ready'):
                first['ready'] = True
            summaries = [text for text in (first.get('summary'), change.get('summary')) if text]
            if summaries:
                first['summary'] = '; '.join(dict.fromkeys(summaries))[:2000]
            based_on = [*first.get('based_on', []), *change.get('based_on', [])]
            if based_on:
                first['based_on'] = [r for i, r in enumerate(based_on) if r not in based_on[:i]]
            continue
        if isinstance(change, dict) and key:
            change = by_document[key] = dict(change)
        folded.append(change)
    return folded


def schema(model_name, /, **fields):
    return create_model(model_name, __base__=StrictPayload, **fields)


Empty = schema('Empty')
Need = schema('Need', document=(Annotated[str, Field(min_length=1, max_length=300)] | None, None),
              service=(Annotated[str, Field(min_length=1, max_length=40)] | None, None),
              min_version=(Annotated[int, Field(ge=1)] | None, None))
Paths = Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(max_length=30)]
Services = Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=10)]
Needs = Annotated[list[Need], Field(max_length=10, description='Start when these exist')]
SerialReason = Annotated[str, Field(min_length=1, max_length=500)]
Read = schema('Read', document_id=(Text, ...), version=(Version | None, None), section_id=(Text | None, None),
              offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=12000)], 6000), force=(bool, False),
              whole=(bool, False))
Identifier = schema('Identifier', identifier=(Text, ...))
ACTION_PAYLOADS = {
    **{name: Empty for name in ('catalog', 'snapshot', 'dependencies', 'recover')},
    'read': Read,
    'publish': Publish,
    'subscribe': schema('Subscribe', document_id=(Text, ...), enabled=(bool, True), section_id=(Text | None, None)),
    'send': schema('Send', recipient=(Role, ...), content=(Text, ...), key=(Text, ...), summary=(str | None, None), reply_to=(Text | None, None), request_id=(Text | None, None)),
    'request': schema('Request', recipient=(Role, ...), goal=(Text, ...), key=(Text, ...), summary=(str | None, None),
                      based_on=(list[RevisionReference], []), dependencies=(list[Text], []), artifacts=(list[Text], []),
                      priority=(Annotated[int, Field(ge=0, le=3)], 1), deadline=(float | None, None),
                      documents=(Paths, []), services=(Services, []), needs=(Needs, []), serial_reason=(SerialReason | None, None)),
    'inspect_request': schema('InspectRequest', identifier=(Text, ...), detail=(Literal['summary', 'full'], 'summary')),
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
    'environment': Empty,
    'check_delivery': schema('CheckDelivery', artifacts=(Paths | None, None)),
    'journal': schema('Journal',
        kind=(Literal['done', 'next', 'blocked', 'decision', 'evidence'], ...),
        text=(Annotated[str, Field(min_length=1, max_length=1500)], ...)),
    'register_service': schema('RegisterService',
        name=(Annotated[str, Field(min_length=1, max_length=40)], ...),
        kind=(Literal['http', 'tcp', 'other'], 'http'),
        port=(Annotated[int, Field(ge=1, le=65535)] | None, None),
        host=(Annotated[str, Field(max_length=100)], '127.0.0.1'), base_url=(Annotated[str, Field(max_length=300)] | None, None),
        health_path=(Annotated[str, Field(max_length=200)] | None, None),
        command=(Annotated[list[Annotated[str, Field(max_length=2000)]], Field(max_length=40)] | None, None),
        cwd=(Annotated[str, Field(max_length=300)], '.'), note=(Annotated[str, Field(max_length=300)] | None, None),
        remove=(bool, False)),
    'register_command': schema('RegisterCommand',
        name=(Annotated[str, Field(min_length=1, max_length=40)], ...),
        argv=(Annotated[list[Annotated[str, Field(max_length=2000)]], Field(min_length=1, max_length=40)], ...),
        cwd=(Annotated[str, Field(max_length=300)], '.'), purpose=(Annotated[str, Field(max_length=200)] | None, None)),
}


Criteria = Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1, max_length=8)]
Evidence = dict[Annotated[str, Field(min_length=1, max_length=300)], Annotated[str, Field(min_length=1, max_length=4000)]]
GOAL_ACTIONS = {'create_goal', 'get_goal', 'update_goal', 'complete_goal', 'create_task', 'submit_task', 'complete_task'}
ACTION_PAYLOADS.update({
    'create_goal': schema('CreateGoal', objective=(Annotated[str, Field(min_length=1, max_length=4000)], ...), criteria=(Criteria, ...), key=(Annotated[str, Field(min_length=1, max_length=120)], ...), reminder_seconds=(Annotated[int, Field(ge=60, le=86400)], 180),
                          scale=(Literal['small', 'medium', 'large'] | None, Field(None, description='small 3/3, medium 6/5, large 8/8 criteria per goal/task'))),
    'get_goal': schema('GetGoal', identifier=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=20)], 10)),
    'update_goal': schema('UpdateGoal', identifier=(Text, ...), state=(Literal['active','paused','blocked','cancelled'], ...), reason=(Annotated[str, Field(min_length=1, max_length=2000)], ...),
                          scale=(Literal['small', 'medium', 'large'] | None, None)),
    'complete_goal': schema('CompleteGoal', identifier=(Text, ...), evidence=(Evidence, ...)),
    'create_task': schema('CreateTask', goal_id=(Text, ...), recipient=(Role, ...), goal=(Annotated[str, Field(min_length=1, max_length=8000)], ...), criteria=(Criteria, ...), key=(Annotated[str, Field(min_length=1, max_length=160)], ...), dependencies=(list[Text], []), artifacts=(list[Text], []), based_on=(list[RevisionReference], []), summary=(Annotated[str, Field(max_length=160)] | None, None),
                          documents=(Paths, []), services=(Services, []), needs=(Needs, []), serial_reason=(SerialReason | None, None),
                          verification=(Literal['self', 'end_to_end'], Field('self'))),
    'submit_task': schema('SubmitTask', identifier=(Text, ...), result=(Text, ...), artifacts=(list[Text] | None, None), based_on=(list[RevisionReference] | None, None)),
    'complete_task': schema('CompleteTask', identifier=(Text, ...), result=(Text, ...), evidence=(Evidence, ...)),
})


def validate_payload(action, payload):
    return ACTION_PAYLOADS[action].model_validate(payload).model_dump(exclude_unset=True, exclude_none=True)


WorkerRequest = schema('WorkerRequest', recipient=(Role, ...), goal=(Text, ...), key=(Text, ...), summary=(str | None, None),
                       artifacts=(list[Text], []), dependencies=(list[Text], []))
