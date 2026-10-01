import asyncio
import pytest
from app.storage import ChatStore, StorageConflictError
from app.collaboration.service import Collaboration
from app.collaboration.documents import outline


def run(coro):
    return asyncio.run(coro)


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
    project = await store.create_project('Project', str(workspace))
    service = Collaboration(store)
    await service.initialize()
    return store, service, project['id'], workspace


async def create(service, p, content='# Guide\nIntro\n## API\nold\n'):
    return (await service.publish(p, 'backend', [dict(path='docs/api.md', content=content, summary='Initial API', base_version=0)]))[0]['document_id']


def test_revision_conflict_sections_external_and_recovery(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        doc = await create(s, p)
        headings = (await s.read(p, doc))['outline']
        assert [h['title'] for h in headings] == ['Document', 'Guide', 'API']
        section = headings[-1]['id']
        await s.subscribe(p, 'frontend', doc)
        change = dict(document_id=doc, base_version=1, summary='New API', edits=[dict(operation='replace_text', section_id=section, old_text='old', new_text='new')])
        results = await asyncio.gather(s.publish(p, 'backend', [change]), s.publish(p, 'backend', [change]), return_exceptions=True)
        assert sum(isinstance(r, StorageConflictError) for r in results) == 1
        assert (await s.read(p, doc, 1, section))['content'] == 'old\n'
        assert (await s.read(p, doc, 2, section))['content'] == 'new\n'
        inbox = await s.inbox(p, 'frontend', 'ctx')
        assert len(inbox) == 1
        assert await s.inbox(p, 'frontend', 'ctx') == inbox
        await s.acknowledge(p, 'frontend', 'ctx', [inbox[0]['event_id']])
        assert not await s.inbox(p, 'frontend', 'ctx')
        assert await s.inbox(p, 'frontend', 'new-context')
        (workspace/'docs/api.md').write_text('# External\nManual edit\n')
        assert (await s.catalog(p))[0]['external_change']
        with pytest.raises(StorageConflictError):
            await s.publish(p, 'backend', [{**change, 'base_version': 2}])
        await s.publish(p, 'backend', [dict(document_id=doc, base_version=2, external=True, summary='Import manual edit')])
        await store.close()
        restored = Collaboration(ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}'))
        assert (await restored.catalog(p))[0]['version'] == 3
        assert (await restored.snapshot(p))['subscription']
        await restored.store.close()
    run(scenario())


def test_batch_atomic_validation_and_dependency_conflict(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        doc = await create(s, p)
        with pytest.raises(StorageConflictError):
            await s.publish(p, 'backend', [dict(path='docs/new.md', content='valid', summary='new'), dict(document_id=doc, base_version=0, summary='bad')])
        assert not (workspace/'docs/new.md').exists()
        with pytest.raises(StorageConflictError):
            await s.publish(p, 'backend', [dict(path='docs/new.md', content='valid', summary='new', based_on=[dict(document_id=doc, version=0)])])
        with pytest.raises(ValueError):
            await s.publish(p, 'backend', [dict(path='../escape.md', content='bad', summary='bad')])
        await store.close()
    run(scenario())


def test_requests_suspend_resume_cycle_delivery_and_idempotency(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        root = await s.create_request(p, 'leader', 'frontend', 'Build page', 'page')
        assert (await s.claim())[2]['id'] == root['id']
        image = await s.create_request(p, 'frontend', 'designer', 'Draw image', 'image', artifacts=['hero.svg'], parent=root['id'])
        assert (await s.create_request(p, 'frontend', 'designer', 'Draw image', 'image', artifacts=['hero.svg'], parent=root['id']))['id'] == image['id']
        await s.wait_for(p, root['id'], [image['id']], 'frontend')
        assert (await s.claim())[2]['id'] == image['id']
        with pytest.raises(StorageConflictError):
            await s.wait_for(p, image['id'], [root['id']], 'designer')
        with pytest.raises(ValueError):
            await s.transition(p, image['id'], 'designer', 'review', 'Image ready')
        (workspace/'hero.svg').write_text('<svg/>')
        await s.transition(p, image['id'], 'designer', 'review', 'Image ready', ['hero.svg'])
        await s.transition(p, image['id'], 'frontend', 'completed', 'Accepted')
        assert (await s.claim())[2]['id'] == root['id']
        message = await s.send(p, 'frontend', 'designer', 'Thanks', 'thanks')
        assert await s.send(p, 'frontend', 'designer', 'Thanks', 'thanks') == message
        with pytest.raises(StorageConflictError):
            await s.send(p, 'frontend', 'designer', 'Changed', 'thanks')
        await store.close()
    run(scenario())


def test_markdown_fences_and_direct_body():
    result = outline('# Title\n```md\n# Not heading\n```\n## Child\ntext\n')
    assert len(result) == 3
    assert result[1]['body_end'] < result[1]['end']


def test_journal_recovers_failed_materialization(tmp_path, monkeypatch):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        import app.collaboration.service as module
        original = module.os.replace
        monkeypatch.setattr(module.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk interruption')))
        with pytest.raises(OSError):
            await create(s, p)
        assert not (await s.snapshot(p))['document']
        assert (await s.snapshot(p))['pending']
        monkeypatch.setattr(module.os, 'replace', original)
        await s.recover()
        assert len((await s.catalog(p))) == 1
        assert not (await s.snapshot(p))['pending']
        await store.close()
    run(scenario())


def test_dependency_rechecked_at_delivery_and_inbox_isolation(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        doc = await create(s, p)
        a = await store.create_conversation(p, 'A')
        b = await store.create_conversation(p, 'B')
        await s.send(p, 'frontend', 'designer', 'A only', 'a', a['id'])
        assert not await s.inbox(p, 'designer', 'ctx', b['id'])
        assert await s.inbox(p, 'designer', 'ctx', a['id'])
        r = await s.create_request(p, 'frontend', 'designer', 'Design', 'design', based_on=[dict(document_id=doc, version=1)])
        await s.claim()
        await s.publish(p, 'backend', [dict(document_id=doc, base_version=1, summary='Revise requirements', edits=[])])
        with pytest.raises(StorageConflictError):
            await s.transition(p, r['id'], 'designer', 'review', 'Done')
        await store.close()
    run(scenario())
