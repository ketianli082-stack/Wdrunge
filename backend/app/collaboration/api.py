"""Project collaboration API and explicit user controls."""
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from app.collaboration.tools import Context, active, collaborate
from app.storage import StorageNotFoundError, StorageConflictError

router = APIRouter(prefix='/api/projects/{project_id}/collaboration', tags=['Collaboration'])
_service = None


def configure(service):
    global _service
    _service = service


class Command(BaseModel):
    member: Literal['leader', 'product', 'backend', 'frontend', 'designer', 'testing'] = 'leader'
    conversation: str = ''
    action: Literal['catalog', 'read', 'publish', 'subscribe', 'send', 'request', 'inspect_request', 'transition', 'snapshot', 'search', 'diff', 'schedule', 'dependencies', 'decision', 'maintenance', 'handoff', 'recover', 'inspect_message', 'history', 'create_goal', 'get_goal', 'update_goal', 'complete_goal', 'create_task', 'submit_task', 'complete_task', 'environment', 'register_service', 'register_command', 'journal', 'check_delivery']
    payload: dict = Field(default_factory=dict)


@router.get('')
async def snapshot(project_id: str):
    if _service is None:
        raise HTTPException(503, 'Collaboration is not initialized')
    try:
        return await _service.snapshot(project_id)
    except StorageNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/commands')
async def command(project_id: str, body: Command):
    if _service is None:
        raise HTTPException(503, 'Collaboration is not initialized')
    async with _service.transaction() as c:
        try:
            await _service._project(c, project_id)
            await _service._conversation(c, project_id, body.conversation)
        except (StorageNotFoundError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc
    token = active.set(Context(_service, project_id, body.member, body.conversation, 'user'))
    try:
        result = await collaborate.ainvoke({'action': body.action, 'payload': body.payload})
        if 'error' in result:
            raise HTTPException(409, result['error'])
        return result
    finally:
        active.reset(token)


class Pause(BaseModel):
    paused: bool


@router.post('/pause')
async def pause(project_id: str, body: Pause):
    if _service is None:
        raise HTTPException(503, 'Collaboration is not initialized')
    try:
        return await _service.set_paused(project_id, body.paused)
    except StorageNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/services/{name}/stop')
async def stop_service(project_id: str, name: str):
    """Stop the running process of a registry service (e.g. one kept after its task)."""
    if _service is None:
        raise HTTPException(503, 'Collaboration is not initialized')
    from app.execution.processes import processes
    try:
        async with _service.reading() as c:
            root = (await _service._project(c, project_id))['workspace_path']
            record = await _service._get(c, 'service', project_id, name, False)
    except StorageNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    job = processes.service_job(root, name=name, port=(record or {}).get('port'))
    if job is None:
        raise HTTPException(404, f'Service {name} is not running')
    await processes.stop(job)
    return {'stopped': name, 'process_id': job.id}


@router.get('/requests/{request_id}/journal', response_class=PlainTextResponse)
async def export_journal(project_id: str, request_id: str):
    """Export a task's work journal as Markdown, for download on request."""
    if _service is None:
        raise HTTPException(503, 'Collaboration is not initialized')
    try:
        text = await _service.journal_markdown(project_id, request_id)
    except StorageNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PlainTextResponse(text, headers={'Content-Disposition': f'attachment; filename="journal-{request_id[:40]}.md"'})
