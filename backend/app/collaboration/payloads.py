"""Validate delivery arguments before any publication side effects."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class RevisionReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class TransitionPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(min_length=1)
    state: Literal['queued', 'blocked', 'review', 'completed', 'failed', 'cancelled', 'rejected']
    result: str = Field(default='', description='Plain text delivery details, not a JSON object or list')
    evidence: dict[str, str] | None = None
    artifacts: list[str] | None = None
    based_on: list[RevisionReference] | None = None
    summary: str | None = Field(default=None, max_length=160)


# Every action has a closed schema: malformed nested values and runtime-owned
# fields are rejected before a tool reads or mutates project state. Types are checked
# in Pydantic's lax mode, so "true" is a boolean and "3" an integer (Blog5: ready sent
# as a string was refused five times under strict mode).
from typing import Annotated, ClassVar
from pydantic import create_model, model_validator

Text = Annotated[str, Field(min_length=1)]
Offset = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(ge=1)]
Role = Literal['leader', 'product', 'backend', 'frontend', 'designer', 'testing']


# A field API callers may omit but the runtime requires from a model: advertised as
# required in the tool schema (app.agents.tools.validation.model_tool_schemas).
MODEL_REQUIRED = {'model_required': True}


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class DocumentEdit(StrictPayload):
    operation: Literal['replace_text', 'replace_section_body', 'insert_section', 'delete_section']
    section_id: Text = Field(description='Exact section ID from the outline; "root" is the text before the first heading')
    old_text: str = Field('', description='replace_text only: text that occurs once in the section')
    new_text: str = Field('', description='Markdown. replace_section_body: the new body without its heading; insert_section: the new section with its heading')
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
    document_id: Text | None = Field(None, description='An existing document; omit for a new one')
    path: Text | None = Field(None, description='A new document, e.g. "docs/backend/api-<module>.md"')
    title: Text | None = None
    base_version: Offset = Field(0, description='The version you read; 0 for a new document')
    summary: Annotated[str, Field(min_length=1, max_length=2000, description='One line saying what changed')]
    content: Annotated[str, Field(max_length=500000)] | None = Field(None, description='Whole text: a new document, or replacing an unfilled draft')
    edits: list[DocumentEdit] = Field(default_factory=list, max_length=100, description='Changes to an existing document, applied in order')
    based_on: list[RevisionReference] = Field(default_factory=list)
    allow_merge: bool = False
    external: bool = False
    ready: bool = Field(False, description='Boolean true or false, without quotes. true: the document is complete; tasks '
                                           'waiting on it start now')

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
    # Change fields accepted beside `changes` without being advertised (change_defaults).
    ACCEPTED_EXTRA: ClassVar[tuple] = ('ready', 'based_on')
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
            version = int(version)  # compared with each change below, before types are coerced
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
Needs = Annotated[list[Need], Field(max_length=10, description='Deliverables that must exist before this task starts, '
                                                                'e.g. [{"document": "docs/backend/api-<module>.md"}] or [{"service": "api"}]')]
SerialReason = Annotated[str, Field(min_length=1, max_length=500)]
Read = schema('Read', document_id=(Text, ...), version=(Version | None, Field(None, description='Omit for the latest')),
              section_id=(Text | None, Field(None, description='A section ID from the outline; omit to get the outline')),
              offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=12000)], 6000),
              force=(bool, Field(False, description='Return the text even when your desk already shows it')),
              whole=(bool, Field(False, description='true: the whole document')),
              subtree=(bool, Field(False, description='With section_id, true: the section and all its subsections')))
Identifier = schema('Identifier', identifier=(Text, ...))
# What a model sends: which documents a delivery is built on is recorded by the platform
# (app.collaboration.basis), so the model-facing schemas carry no based_on.
Transition = schema('Transition', identifier=(Text, ...),
                    state=(Literal['queued', 'blocked', 'review', 'completed', 'failed', 'cancelled', 'rejected'], ...),
                    result=(str, Field('', description='Plain text delivery details, not a JSON object or list')),
                    evidence=(dict[str, str] | None, None), artifacts=(list[str] | None, None),
                    summary=(Annotated[str, Field(max_length=160)] | None, None))


class GoalRequirements(StrictPayload):
    document: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    none: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode='after')
    def one_of(self):
        if (self.document is None) == (self.none is None):
            raise ValueError('Give exactly one of document (the requirements document path) or none (why this goal needs none)')
        return self
ACTION_PAYLOADS = {
    **{name: Empty for name in ('catalog', 'snapshot', 'dependencies', 'recover')},
    'read': Read,
    'publish': Publish,
    'subscribe': schema('Subscribe', document_id=(Text, ...), enabled=(bool, True), section_id=(Text | None, None)),
    'send': schema('Send', recipient=(Role, ...), content=(Text, ...), key=(Text, ...), summary=(str | None, None), reply_to=(Text | None, None), request_id=(Text | None, None)),
    'request': schema('Request', recipient=(Role, ...), goal=(Text, ...), key=(Text, ...), summary=(str | None, None),
                      dependencies=(list[Text], []), artifacts=(list[Text], []),
                      priority=(Annotated[int, Field(ge=0, le=3)], 1), deadline=(float | None, None),
                      documents=(Paths, []), services=(Services, []), needs=(Needs, []), serial_reason=(SerialReason | None, None)),
    'inspect_request': schema('InspectRequest', identifier=(Text, ...), detail=(Literal['summary', 'full'], 'summary')),
    'inspect_message': schema('InspectMessage', identifier=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=8000)], 4000)),
    'wait': schema('Wait', dependencies=(list[Text], ...)),
    'transition': Transition,
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
        name=(Annotated[str, Field(min_length=1, max_length=40)], Field(description='Lowercase service name, e.g. "api" or "web"')),
        kind=(Literal['http', 'tcp', 'other'], 'http'),
        port=(Annotated[int, Field(ge=1, le=65535)] | None, Field(None, description='Omit to keep its port or get a free one')),
        host=(Annotated[str, Field(max_length=100)], '127.0.0.1'), base_url=(Annotated[str, Field(max_length=300)] | None, None),
        health_path=(Annotated[str, Field(max_length=200)] | None, Field(None, description='e.g. "/health"')),
        argv=(Annotated[list[Annotated[str, Field(max_length=2000)]], Field(min_length=1, max_length=40)] | None,
              Field(None, description='Start command as an array: the executable, then each argument, e.g. ["npm", "run", "dev"]')),
        cwd=(Annotated[str, Field(max_length=300)], Field('.', description='Directory the command runs in, relative to the project root')),
        note=(Annotated[str, Field(max_length=300)] | None, None),
        remove=(bool, Field(False, description='true deletes the service'))),
    'register_command': schema('RegisterCommand',
        name=(Annotated[str, Field(min_length=1, max_length=40)], ...),
        argv=(Annotated[list[Annotated[str, Field(max_length=2000)]], Field(min_length=1, max_length=40)],
              Field(description='The command as an array: the executable, then each argument, e.g. ["uv", "run", "pytest", "-q"]')),
        cwd=(Annotated[str, Field(max_length=300)], Field('.', description='Directory it runs in, relative to the project root')),
        purpose=(Annotated[str, Field(max_length=200)] | None, None)),
}


Criteria = Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1, max_length=8)]
Evidence = dict[Annotated[str, Field(min_length=1, max_length=300)], Annotated[str, Field(min_length=1, max_length=4000)]]
GOAL_ACTIONS = {'create_goal', 'get_goal', 'update_goal', 'complete_goal', 'create_task', 'create_tasks', 'submit_task', 'complete_task'}
# The fields of one goal task; create_tasks takes a list of them so a whole plan is one call.
TASK_FIELDS = dict(
    recipient=(Role, ...),
    goal=(Annotated[str, Field(min_length=1, max_length=8000)], Field(description='The result to achieve, not how')),
    criteria=(Criteria, ...),
    key=(Annotated[str, Field(min_length=1, max_length=160)], Field(description='Stable short name, e.g. "backend-1"; a retry with the same key returns the same task')),
    dependencies=(list[Text], Field([], description='IDs of tasks that must be accepted first, e.g. ["leader:backend-init"]')),
    artifacts=(list[Text], Field([], description='Files or directories it delivers, e.g. ["backend/"]')),
    summary=(Annotated[str, Field(min_length=1, max_length=160)] | None, Field(None, description='One sentence for the group chat')),
    documents=(Paths, Field([], description='Shared documents it publishes, e.g. ["docs/backend/api-<module>.md"]')),
    services=(Services, Field([], description='Services it delivers running, e.g. ["api"]; only on the task that first delivers them')),
    needs=(Needs, []), serial_reason=(SerialReason | None, None),
    verification=(Literal['self', 'end_to_end'], Field('self', description='end_to_end only on the one final verification task')),
    module=(Annotated[str, Field(min_length=1, max_length=200)] | None, Field(None, description=
        'The one module this task builds: a level-1 heading of the requirements document, or "init" for '
        'project initialization. Required for backend and frontend tasks of a goal with a requirements document')))
ACTION_PAYLOADS.update({
    'create_goal': schema('CreateGoal', objective=(Annotated[str, Field(min_length=1, max_length=4000)], ...), criteria=(Criteria, ...), key=(Annotated[str, Field(min_length=1, max_length=120)], ...), reminder_seconds=(Annotated[int, Field(ge=60, le=86400)], 180),
                          scale=(Literal['small', 'medium', 'large'] | None, Field(None, description='small 3/3, medium 6/5, large 8/8 criteria per goal/task', json_schema_extra=MODEL_REQUIRED)),
                          requirements=(GoalRequirements | None, Field(None, json_schema_extra=MODEL_REQUIRED, description=
                              'Where the requirements are: {"document": "docs/product/requirements.md"} (a document the user gave, '
                              'or the one the product task will deliver), or {"none": "<why this goal needs no requirements document>"}'))),
    'get_goal': schema('GetGoal', identifier=(Text, ...), offset=(Offset, 0), limit=(Annotated[int, Field(ge=1, le=20)], 10)),
    'update_goal': schema('UpdateGoal', identifier=(Text, ...), state=(Literal['active','paused','blocked','cancelled'], ...), reason=(Annotated[str, Field(min_length=1, max_length=2000)], ...),
                          scale=(Literal['small', 'medium', 'large'] | None, None)),
    'complete_goal': schema('CompleteGoal', identifier=(Text, ...), evidence=(Evidence, ...)),
    'create_task': schema('CreateTask', goal_id=(Text, Field(description='The id create_goal returned')), **TASK_FIELDS),
    'create_tasks': schema('CreateTasks', goal_id=(Text, Field(description='The id create_goal returned')),
                           tasks=(Annotated[list[schema('TaskSpec', **TASK_FIELDS)], Field(min_length=1, max_length=20)], Field(description=
                               'Every task to create now, in order: a task may depend on an earlier one of the same call by its id "leader:<key>"'))),
    'submit_task': schema('SubmitTask', identifier=(Text, Field(description='Your task ID')),
                          result=(Text, Field(description='What you delivered and the evidence, as plain text')),
                          artifacts=(list[Text] | None, Field(None, description='Project-relative paths you deliver, e.g. ["backend/app/"]'))),
    'complete_task': schema('CompleteTask', identifier=(Text, ...), result=(Text, ...), evidence=(Evidence, ...)),
})


def validate_payload(action, payload):
    return ACTION_PAYLOADS[action].model_validate(payload).model_dump(exclude_unset=True, exclude_none=True)


WorkerRequest = schema('WorkerRequest', recipient=(Role, ...), goal=(Text, ...), key=(Text, ...), summary=(str | None, None),
                       artifacts=(list[Text], []), dependencies=(list[Text], []))
