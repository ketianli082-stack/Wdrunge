import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.collaboration.delivery import scan, preview, file_path, updates, router, PREVIEW_BYTES
from app.collaboration.api import configure
from app.collaboration.service import Collaboration
from app.storage import ChatStore


def test_tree_reads_real_folders_and_excludes_private_or_linked_files(tmp_path):
    (tmp_path/'docs'/'design').mkdir(parents=True)
    (tmp_path/'docs'/'design'/'proposal.md').write_text('# Proposal\n')
    (tmp_path/'empty').mkdir()
    (tmp_path/'.env').write_text('secret')
    (tmp_path/'node_modules').mkdir()
    (tmp_path/'node_modules'/'hidden.md').write_text('dependency')
    (tmp_path/'alias').symlink_to(tmp_path/'docs', target_is_directory=True)
    result = scan(tmp_path)
    assert [entry['path'] for entry in result['entries']] == ['docs', 'docs/design', 'docs/design/proposal.md', 'empty']
    for bad in ['../outside.md', '/etc/passwd', '.env', 'alias/design/proposal.md']:
        with pytest.raises(HTTPException) as exc:
            file_path(tmp_path, bad)
        assert exc.value.status_code == 403


def test_preview_latest_text_limits_and_deleted_files(tmp_path):
    path = tmp_path/'document.md'
    path.write_text('# Initial\n')
    assert preview(tmp_path, path.name)['kind'] == 'markdown'
    path.write_text('# Updated\n')
    assert preview(tmp_path, path.name)['content'] == '# Updated\n'
    path.write_text('中' * PREVIEW_BYTES)
    result = preview(tmp_path, path.name)
    assert result['truncated'] and len(result['content'].encode()) <= PREVIEW_BYTES
    (tmp_path/'archive.bin').write_bytes(b'\x00\xffbinary')
    assert preview(tmp_path, 'archive.bin')['kind'] == 'unsupported'
    path.unlink()
    with pytest.raises(HTTPException) as exc:
        preview(tmp_path, path.name)
    assert exc.value.status_code == 404


def test_live_watcher_reports_create_edit_rename_delete(tmp_path):
    async def scenario():
        stopped = asyncio.Event()
        stream = updates(tmp_path, stopped)
        assert (await anext(stream))['entries'] == []
        async def wait_for(predicate):
            async for event in stream:
                if event and predicate(event):
                    return event
        try:
            waiter = asyncio.create_task(wait_for(lambda event: any(e['path'] == 'docs/a.md' for e in event['entries'])))
            await asyncio.sleep(.15)
            (tmp_path/'docs').mkdir()
            (tmp_path/'docs'/'a.md').write_text('first')
            created = await asyncio.wait_for(waiter, 5)
            waiter = asyncio.create_task(wait_for(lambda event: event['revision'] != created['revision']))
            (tmp_path/'docs'/'a.md').write_text('latest text')
            await asyncio.wait_for(waiter, 5)
            assert preview(tmp_path, 'docs/a.md')['content'] == 'latest text'
            waiter = asyncio.create_task(wait_for(lambda event: any(e['path'] == 'docs/b.md' for e in event['entries'])))
            (tmp_path/'docs'/'a.md').rename(tmp_path/'docs'/'b.md')
            renamed = await asyncio.wait_for(waiter, 5)
            assert not any(e['path'] == 'docs/a.md' for e in renamed['entries'])
            waiter = asyncio.create_task(wait_for(lambda event: not any(e['kind'] == 'file' for e in event['entries'])))
            (tmp_path/'docs'/'b.md').unlink()
            await asyncio.wait_for(waiter, 5)
        finally:
            stopped.set()
            await stream.aclose()
    asyncio.run(scenario())


def test_delivery_routes_use_project_workspace_and_safe_mime(tmp_path):
    async def setup():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
        root = tmp_path/'project'
        root.mkdir()
        (root/'docs').mkdir()
        (root/'docs'/'article.md').write_text('# Article\nLive content')
        (root/'page.html').write_text('<script>alert(1)</script>')
        p = await store.create_project('Demo', str(root))
        service = Collaboration(store)
        await service.initialize()
        configure(service)
        return store, p['id']
    store, project = asyncio.run(setup())
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        url = f'/api/projects/{project}/delivery'
        assert client.get(url+'/tree').status_code == 200
        assert client.get(url+'/preview', params={'path':'docs/article.md'}).json()['content'] == '# Article\nLive content'
        response = client.get(url+'/file', params={'path':'page.html'})
        assert response.headers['content-disposition'].startswith('attachment')
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(url+'/preview', params={'path':'../test.db'}).status_code == 403
        assert client.get('/api/projects/other/delivery/tree').status_code == 404
    asyncio.run(store.close())
