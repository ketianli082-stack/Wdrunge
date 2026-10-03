"""Local human control plane, authenticated separately from agent-accessible APIs."""
import hmac
import os
from pathlib import Path
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict

from app.execution import permissions as policy
from app.execution.processes import processes
from app.execution.sandbox import capability

router = APIRouter(prefix='/api/execution', tags=['Execution'])
control_token = secrets.token_urlsafe(32)
# Token file contents this process replaced, restored if it exits without serving.
_previous_token: str | None = None


def write_control_token():
    """Native UI reads a private app-data file; never hand a control token to agents."""
    global _previous_token
    directory = user_data_path('Wdrunge', 'Wdrunge')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'execution-token'
    try:
        existing = path.read_text()
    except OSError:
        existing = None
    if existing != control_token:
        _previous_token = existing
    temporary = directory / f'.execution-token-{secrets.token_hex(8)}'
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(control_token)
    os.replace(temporary, path)
    # Browser users can copy this from the private app-data file. Do not expose it
    # via HTTP, a VITE_* variable, project files, process logs or the model context.
    return path


def restore_control_token():
    """Uvicorn runs app startup before binding its port. A duplicate start that
    fails to bind would otherwise leave its own token in the file and lock the live
    backend's UI out with 401s. Only restore while the file still holds our token."""
    path = user_data_path('Wdrunge', 'Wdrunge') / 'execution-token'
    if _previous_token is None:
        return
    try:
        if path.read_text() != control_token:
            return
        temporary = path.with_name(f'.execution-token-{secrets.token_hex(8)}')
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(_previous_token)
        os.replace(temporary, path)
    except OSError:
        pass


async def local_human(authorization: str = Header(default='')):
    if not hmac.compare_digest(authorization, f'Bearer {control_token}'):
        raise HTTPException(401, '请连接本机执行授权；桌面版自动连接，浏览器版需填写应用数据目录 execution-token 中的授权码。')


@router.get('/capabilities')
async def capabilities():
    return {**capability(), 'pending': len(policy.current.pending) if policy.current else 0}


@router.get('/permissions', dependencies=[Depends(local_human)])
async def permission_state():
    if policy.current is None:
        raise HTTPException(503, 'Permission runtime is not ready')
    return {'pending': [row for row in await policy.current.rows('approval') if row['state'] == 'pending'],
            'grants': await policy.current.rows('grant'), 'sandbox': capability(),
            'settings': policy.current.settings.model_dump(),
            'reviewer': {'model': os.getenv('WDRUNGE_REVIEW_MODEL', 'qwen-flash'),
                         'ready': bool(os.getenv('DASHSCOPE_API_KEY', '').strip()),
                         'confidence_threshold': .75},
            'recent_auto_approvals': len(await policy.current.rows('review'))}


@router.post('/settings', dependencies=[Depends(local_human)])
async def configure_approvals(body: policy.ApprovalSettings):
    await policy.permissions().configure(body)
    return body.model_dump()


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decision: Literal['once', 'always', 'deny']


@router.post('/permissions/{identifier}', dependencies=[Depends(local_human)])
async def decide(identifier: str, body: Decision):
    try:
        await policy.permissions().decide(identifier, body.decision)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {'resolved': True}


@router.post('/grants/{identifier}/revoke', dependencies=[Depends(local_human)])
async def revoke(identifier: str):
    await policy.permissions().revoke(identifier)
    return {'revoked': True}


@router.get('/processes', dependencies=[Depends(local_human)])
async def list_processes():
    return {'processes': [{**job.snapshot(), 'workspace': job.workspace} for job in processes.jobs.values()]}


@router.post('/processes/{identifier}/stop', dependencies=[Depends(local_human)])
async def stop_process(identifier: str):
    job = processes.jobs.get(identifier)
    if not job:
        raise HTTPException(404, 'Process not found')
    return await processes.stop(job)


async def initialize(store, *, publish_token=True):
    if policy.current:
        await policy.current.close()
    policy.current = policy.Permissions(store)
    await policy.current.initialize()
    if publish_token:
        write_control_token()


async def close():
    if policy.current:
        await policy.current.close()
    await processes.close()
