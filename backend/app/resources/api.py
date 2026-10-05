"""Settings API for external service connections. Human control plane only: it needs
the same local token as execution approvals, which agents never receive."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.execution.api import local_human
from app.resources import kinds, service
from app.resources.service import ConnectionInput

router = APIRouter(prefix='/api/resources', tags=['Resources'], dependencies=[Depends(local_human)])


def runtime():
    if service.current is None:
        raise HTTPException(503, '服务连接尚未初始化')
    return service.current


def failure(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(404, str(error))
    return HTTPException(400, str(error))


@router.get('')
async def list_connections():
    return {'connections': await runtime().connections(),
            'types': [{'type': key, 'label': value['label'], 'port': value['port'], 'can_provision': kinds.provisionable(key)}
                      for key, value in kinds.TYPES.items()]}


@router.post('', status_code=201)
async def create_connection(body: ConnectionInput):
    try:
        return await runtime().save(body)
    except (ValueError, LookupError) as error:
        raise failure(error) from error


@router.put('/{identifier}')
async def update_connection(identifier: str, body: ConnectionInput):
    try:
        return await runtime().save(body, identifier)
    except (ValueError, LookupError) as error:
        raise failure(error) from error


@router.delete('/{identifier}')
async def delete_connection(identifier: str):
    try:
        return await runtime().delete(identifier)
    except LookupError as error:
        raise failure(error) from error


class TestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = Field(default=None, max_length=64)
    connection: ConnectionInput | None = None


@router.post('/test')
async def test_connection(body: TestRequest):
    if body.connection is None and body.id is None:
        raise HTTPException(400, '需要连接信息')
    try:
        return await runtime().test(body.connection, body.id)
    except LookupError as error:
        raise failure(error) from error


@router.get('/docker')
async def docker_containers():
    from app.resources.docker import DockerUnavailable
    try:
        return {'containers': await runtime().docker_candidates()}
    except DockerUnavailable as error:
        return {'containers': [], 'unavailable': str(error)}


class DockerImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    container: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=40)


@router.post('/docker/import', status_code=201)
async def docker_import(body: DockerImport):
    from app.resources.docker import DockerUnavailable
    try:
        return await runtime().docker_import(body.container, body.name)
    except (DockerUnavailable, ValueError, LookupError) as error:
        raise failure(error) from error


@router.get('/projects/{project_id}')
async def project_status(project_id: str):
    try:
        return await runtime().project_status(project_id)
    except LookupError as error:
        raise failure(error) from error


@router.post('/projects/{project_id}/prepare')
async def prepare_project(project_id: str):
    """Try again now to create the project's database (e.g. after starting MySQL)."""
    try:
        return await runtime().project_status(project_id, retry=True)
    except LookupError as error:
        raise failure(error) from error


class Restore(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)


@router.post('/projects/{project_id}/backups')
async def backup_project(project_id: str):
    from app.resources.backup import BackupUnavailable
    try:
        created = await runtime().backup_project(project_id)
    except (LookupError, BackupUnavailable) as error:
        raise failure(error) from error
    except Exception as error:
        raise HTTPException(400, f'备份失败：{kinds.friendly(error)}') from error
    if created is None:
        raise HTTPException(400, '项目还没有数据库数据，无需备份')
    return created


@router.post('/projects/{project_id}/restore')
async def restore_project(project_id: str, body: Restore):
    try:
        return await runtime().restore_project(project_id, body.name)
    except (LookupError, ValueError) as error:
        raise failure(error) from error
    except Exception as error:
        raise HTTPException(400, f'恢复失败：{kinds.friendly(error)}') from error
