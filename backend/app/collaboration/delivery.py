"""Read-only live workspace browser, independent of managed document revisions."""
import asyncio
import hashlib
import json
import mimetypes
import os
import time
from contextlib import aclosing
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Literal
from watchfiles import awatch

from app.collaboration import api
from app.storage import StorageNotFoundError

router = APIRouter(prefix='/api/projects/{project_id}/delivery', tags=['Delivery documents'])
EXCLUDED = {'node_modules', '__pycache__', 'dist', 'target', 'venv', 'env'}
MAX_ENTRIES = 10000
PREVIEW_BYTES = 2 * 1024 * 1024


class FileEntry(BaseModel):
    path: str
    name: str
    kind: Literal['file', 'folder']
    size: int = 0
    modified: int = 0


class FileTree(BaseModel):
    entries: list[FileEntry]
    revision: str
    truncated: bool


class FilePreview(BaseModel):
    path: str
    kind: Literal['markdown', 'text', 'image', 'pdf', 'unsupported']
    content: str = ''
    size: int
    truncated: bool = False


def visible(parts):
    return bool(parts) and all(not part.startswith('.') and part not in EXCLUDED for part in parts)


def file_path(root: Path, relative: str):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not visible(path.parts):
        raise HTTPException(403, '文件路径不在可浏览的项目目录内')
    candidate = root / path
    # Symlinks are omitted from both listing and reads, including links inside
    # the project, so a directory link cannot expose ignored/private files.
    cursor = root
    for part in path.parts:
        cursor /= part
        if cursor.is_symlink():
            raise HTTPException(403, '不支持通过符号链接读取文件')
    if not candidate.resolve().is_relative_to(root):
        raise HTTPException(403, '文件路径超出项目目录')
    if not candidate.is_file():
        raise HTTPException(404, '文件已删除、移动或尚未生成')
    return candidate


def scan(root: Path) -> dict:
    entries = []
    truncated = False
    def visit(directory):
        nonlocal truncated
        try:
            children = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
        except (FileNotFoundError, PermissionError):
            return
        for child in children:
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                return
            if not visible((child.name,)) or child.is_symlink():
                continue
            try:
                info = child.stat(follow_symlinks=False)
                folder = child.is_dir(follow_symlinks=False)
                if not folder and not child.is_file(follow_symlinks=False):
                    continue
                entries.append(dict(path=Path(child.path).relative_to(root).as_posix(), name=child.name,
                    kind='folder' if folder else 'file', size=0 if folder else info.st_size,
                    modified=info.st_mtime_ns if not folder else 0))
                if folder:
                    visit(Path(child.path))
            except (FileNotFoundError, PermissionError):
                continue
    visit(root)
    revision = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()[:20]
    return {'entries': entries, 'revision': revision, 'truncated': truncated}


async def workspace(project_id):
    if api._service is None:
        raise HTTPException(503, '项目服务尚未就绪')
    try:
        async with api._service.reading() as connection:
            project = await api._service._project(connection, project_id)
    except StorageNotFoundError as exc:
        raise HTTPException(404, '项目不存在') from exc
    root = Path(project['workspace_path']).resolve()
    if not root.is_dir():
        raise HTTPException(404, '项目文件夹不存在')
    return root


def preview(root, relative):
    path = file_path(root, relative)
    mime = mimetypes.guess_type(path.name)[0] or ''
    size = path.stat().st_size
    result = dict(path=relative, size=size, content='', truncated=False)
    if mime.startswith('image/'):
        return {**result, 'kind': 'image'}
    if mime == 'application/pdf':
        return {**result, 'kind': 'pdf'}
    with path.open('rb') as stream:
        data = stream.read(PREVIEW_BYTES + 1)
    truncated = len(data) > PREVIEW_BYTES
    if b'\x00' in data:
        return {**result, 'kind': 'unsupported'}
    try:
        # Incremental decoder allows the last UTF-8 character to straddle a page.
        import codecs
        content = codecs.getincrementaldecoder('utf-8')().decode(data[:PREVIEW_BYTES], final=not truncated)
    except UnicodeDecodeError:
        return {**result, 'kind': 'unsupported'}
    return {**result, 'kind': 'markdown' if path.suffix.lower() in {'.md', '.markdown', '.mdown'} else 'text', 'content': content, 'truncated': truncated}


async def updates(root, stopped):
    """Watch native filesystem changes, with timeout reconciliation for reconnects."""
    previous = await asyncio.to_thread(scan, root)
    yield previous
    def relevant(change, path):
        try:
            return visible(Path(path).relative_to(root).parts)
        except ValueError:
            return False
    async with aclosing(awatch(root, watch_filter=relevant, debounce=100, step=50,
                               stop_event=stopped, yield_on_timeout=True, rust_timeout=1000)) as watcher:
        async for changes in watcher:
            current = await asyncio.to_thread(scan, root)
            if changes or current['revision'] != previous['revision']:
                # Even a rewrite preserving size and timestamp must refresh a preview.
                if changes and current['revision'] == previous['revision']:
                    current['revision'] += f'-{time.time_ns()}'
                previous = current
                yield current
            else:
                yield None  # SSE heartbeat keeps proxies and disconnect detection live.


@router.get('/tree', response_model=FileTree)
async def tree(project_id: str):
    return await asyncio.to_thread(scan, await workspace(project_id))


@router.get('/preview', response_model=FilePreview)
async def read_preview(project_id: str, path: str = Query(min_length=1)):
    try:
        return await asyncio.to_thread(preview, await workspace(project_id), path)
    except FileNotFoundError as exc:
        raise HTTPException(404, '文件已删除或移动') from exc
    except PermissionError as exc:
        raise HTTPException(403, '没有读取该文件的权限') from exc


@router.get('/file')
async def read_file(project_id: str, path: str = Query(min_length=1), download: bool = False):
    target = file_path(await workspace(project_id), path)
    mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
    inline = not download and (mime.startswith('image/') or mime == 'application/pdf')
    return FileResponse(target, media_type=mime, filename=target.name,
        content_disposition_type='inline' if inline else 'attachment',
        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                 'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'"})


@router.get('/events', responses={200: {'content': {'text/event-stream': {}}}})
async def events(project_id: str, request: Request):
    root = await workspace(project_id)
    async def stream():
        stopped = asyncio.Event()
        async with aclosing(updates(root, stopped)) as changes:
            try:
                async for value in changes:
                    if await request.is_disconnected():
                        break
                    if value is None:
                        yield ': heartbeat\n\n'
                    else:
                        yield 'event: files\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
            finally:
                stopped.set()
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
