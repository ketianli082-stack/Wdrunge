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
        await s.acknowledge(p, 'frontend', 'ctx', inbox[0]['event_ids'])
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


def test_section_subscriptions_diff_search_and_read_confirmation(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        doc = await create(s, p, '# Guide\nIntro\n## API\nold\n## Other\nstable\n')
        sections = (await s.read(p, doc))['outline']
        api, other = sections[-2]['id'], sections[-1]['id']
        await s.subscribe(p, 'frontend', doc, section_id=api)
        await s.subscribe(p, 'designer', doc, section_id=other)
        for version, before, after in [(1, 'old', 'new'), (2, 'new', 'newer')]:
            await s.publish(p, 'backend', [dict(document_id=doc, base_version=version, summary=after, edits=[dict(operation='replace_text', section_id=api, old_text=before, new_text=after)])])
        reminders = await s.inbox(p, 'frontend', 'ctx')
        assert len(reminders) == 1 and len(reminders[0]['event_ids']) == 2
        assert not await s.inbox(p, 'designer', 'ctx')
        assert '+newer' in (await s.section_diff(p, doc, 1, 3, api))['diff']
        assert (await s.search(p, 'newer'))['matches'][0]['section_id'] == api
        result, key = await s.read_cached(p, 'frontend', 'ctx', document_id=doc, section_id=api)
        assert result['content'] == 'newer\n'
        assert not (await s.read_cached(p, 'frontend', 'ctx', document_id=doc, section_id=api))[0].get('already_in_context')
        await s.confirm_reads(p, [key])
        assert (await s.read_cached(p, 'frontend', 'ctx', document_id=doc, section_id=api))[0]['already_in_context']
        assert 'content' in (await s.read_cached(p, 'frontend', 'fresh', document_id=doc, section_id=api))[0]
        await store.close()
    run(scenario())


def test_commonmark_index_and_bounded_pages():
    from app.collaboration.documents import page
    content = '# Real\n\n> # Quoted\n\n<!--\n# Hidden\n-->\n\n~~~md\n# Code\n~~~\n\nSection\n-------\ntext\n'
    assert [s['title'] for s in outline(content)] == ['Document', 'Real', 'Section']
    body = 'first paragraph\n\n```json\n' + 'x' * 100 + '\n```\n'
    first, cursor, fragmented = page(body, 0, 30)
    assert len(first) <= 30 and cursor and not fragmented
    rest, following, fragmented = page(body, cursor, 30)
    assert len(rest) <= 30 and following and fragmented


def test_priority_deadline_escalation_and_cancellation_graph(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        low = await s.create_request(p, 'leader', 'frontend', 'Low priority', 'low', priority=0)
        high = await s.create_request(p, 'leader', 'frontend', 'Urgent', 'high', priority=3, deadline=100)
        downstream = await s.create_request(p, 'leader', 'testing', 'Test urgent', 'test', dependencies=[high['id']])
        assert (await s.claim())[2]['id'] == high['id']
        await s.scheduling_tick(now=200)
        await s.scheduling_tick(now=300)
        events = await s.inbox(p, 'leader', 'ctx')
        assert sum(e['type'] == 'request_overdue' for e in events) == 1
        graph = await s.dependency_graph(p)
        assert graph['edges'] == [{'from': downstream['id'], 'to': high['id'], 'type': 'depends_on'}]
        await s.transition(p, high['id'], 'leader', 'cancelled')
        assert (await s.get_request(p, downstream['id']))['state'] == 'cancelled'
        assert (await s.claim())[2]['id'] == low['id']
        await store.close()
    run(scenario())


def test_decision_history_staleness_duplicates_and_handoff(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        text = '# Contract\n' + 'The response uses a documented token field. ' * 4
        doc = await create(s, p, text)
        await s.publish(p, 'backend', [dict(path='docs/copy.md', content=text, summary='Copy for review')])
        links = [dict(document_id=doc, version=1)]
        a = await s.record_decision(p, 'leader', 'Token naming', 'API compatibility', links, {'auth.token_field': 'token'}, state='accepted')
        await s.record_decision(p, 'leader', 'Alternative naming', 'New clients', links, {'auth.token_field': 'access_token'}, state='accepted')
        kinds = {f['kind'] for f in (await s.maintenance(p))['findings']}
        assert {'duplicate_section', 'contradictory_decisions'} <= kinds
        request = await s.create_request(p, 'leader', 'frontend', 'Use contract', 'contract', based_on=links)
        await s.publish(p, 'backend', [dict(document_id=doc, base_version=1, summary='Revise contract', edits=[])])
        assert (await s.get_request(p, request['id']))['needs_review']
        assert 'decision_evidence_changed' in {f['kind'] for f in (await s.maintenance(p))['findings']}
        with pytest.raises(ValueError):
            await s.record_decision(p, 'frontend', 'Approve', 'reason', links, state='accepted')
        handoff = await s.handoff(p, 'frontend', save=True)
        assert handoff['pending'][0]['id'] == request['id'] and handoff['documents']
        assert (await s.snapshot(p))['handoff']
        with pytest.raises(StorageConflictError):
            await s.record_decision(p, 'leader', 'Overwrite', 'reason', links, identifier=a['id'], base_version=0)
        await store.close()
    run(scenario())


def test_safe_document_auto_merge_rejects_changed_section(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        doc = await create(s, p, '# Guide\nintro\n## A\na\n## B\nb\n')
        sections = (await s.read(p, doc))['outline']
        a, b = sections[-2]['id'], sections[-1]['id']
        def change(sid, old, new):
            return dict(document_id=doc, base_version=1, allow_merge=True, summary=new, edits=[dict(operation='replace_text', section_id=sid, old_text=old, new_text=new)])
        await s.publish(p, 'backend', [change(a, 'a', 'first')])
        await s.publish(p, 'backend', [change(b, 'b', 'second')])
        assert (await s.read(p, doc, 3, a))['content'] == 'first\n'
        assert (await s.read(p, doc, 3, b))['content'] == 'second\n'
        with pytest.raises(StorageConflictError):
            await s.publish(p, 'backend', [change(a, 'a', 'overwrite')])
        await store.close()
    run(scenario())


def test_multi_instance_leases_and_isolated_merge(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        (workspace/'code.txt').write_text('one\ntwo\nthree\n')
        for key in ['a','b','c']:
            await s.create_request(p, 'leader', 'frontend', key, key)
        other = Collaboration(ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}'))
        await other.initialize()
        claims = await asyncio.gather(s.claim('one'), other.claim('two'))
        assert len({job[2]['id'] for job in claims}) == 2
        assert await s.claim('three') is None
        first, second = claims[0][2], claims[1][2]
        wa = await s.prepare_workspace(p, first['id'])
        wb = await other.prepare_workspace(p, second['id'])
        (wa.work/'code.txt').write_text('ONE\ntwo\nthree\n')
        (wb.work/'code.txt').write_text('one\ntwo\nTHREE\n')
        assert (workspace/'code.txt').read_text() == 'one\ntwo\nthree\n'
        await s.publish_artifacts(p, first['id'], first['instance_id'])
        await other.publish_artifacts(p, second['id'], second['instance_id'])
        assert (workspace/'code.txt').read_text() == 'ONE\ntwo\nTHREE\n'
        await s.recover_requests()
        assert (await s.get_request(p, first['id']))['state'] == 'running'
        async with s.transaction() as c:
            request = await s._get(c, 'request', p, first['id'])
            request['lease_until'] = 1
            await s._put(c, 'request', p, first['id'], request)
        await s.recover_requests()
        assert (await s.get_request(p, first['id']))['state'] == 'queued'
        with pytest.raises(StorageConflictError):
            await s.publish_artifacts(p, first['id'], first['instance_id'])
        await other.store.close()
        await store.close()
    run(scenario())


def test_workspace_conflict_preserves_original_and_refreshes_dependency(tmp_path):
    from app.collaboration.workspaces import IsolatedWorkspace, merge_text
    root = tmp_path/'source'
    root.mkdir()
    (root/'a.txt').write_text('original\n')
    (root/'b.txt').write_text('original\n')
    w = IsolatedWorkspace(root, 'abc123')
    w.prepare()
    (w.work/'a.txt').write_text('local\n')
    (w.work/'b.txt').write_text('local\n')
    (root/'b.txt').write_text('external\n')
    with pytest.raises(StorageConflictError):
        w.plan()
    assert (root/'a.txt').read_text() == 'original\n'
    (root/'dependency.svg').write_text('<svg/>')
    w.refresh()
    assert (w.work/'dependency.svg').exists()
    assert (w.work/'a.txt').read_text() == 'local\n'
    with pytest.raises(StorageConflictError):
        merge_text('line\n', 'change1\n', 'change2\n')


def test_real_worker_graph_uses_isolated_request_workspace(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    class FakeModel:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content='File delivered')
            return AIMessage(content='', tool_calls=[{'name':'write_project_file','args':{'path':'result.txt','content':'isolated artifact'},'id':'write'}])
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: FakeModel())
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        team._checkpointer = InMemorySaver()
        scheduler = Scheduler(s, team)
        request = await s.create_request(p, 'leader', 'frontend', 'Write result', 'result', artifacts=['result.txt'])
        job = await s.claim(scheduler.owner)
        await scheduler.execute(*job)
        r = await s.get_request(p, request['id'])
        assert r['state'] == 'review', r['result']
        assert r['workspace_published']
        assert (workspace/'result.txt').read_text() == 'isolated artifact'
        await store.close()
    run(scenario())


def test_messages_can_target_one_concurrent_request(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        one = await s.create_request(p, 'leader', 'frontend', 'One', 'one')
        two = await s.create_request(p, 'leader', 'frontend', 'Two', 'two')
        await s.send(p, 'leader', 'frontend', 'Only task one', 'msg', request_id=one['id'])
        assert any(r.get('content') == 'Only task one' for r in await s.inbox(p, 'frontend', 'one', request=one['id']))
        assert not any(r.get('content') == 'Only task one' for r in await s.inbox(p, 'frontend', 'two', request=two['id']))
        await store.close()
    run(scenario())


def test_missing_delivery_does_not_publish_partial_edits(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        r = await s.create_request(p, 'leader', 'frontend', 'Deliver image', 'missing', artifacts=['missing.svg'])
        job = (await s.claim())[2]
        w = await s.prepare_workspace(p, r['id'])
        (w.work/'partial.txt').write_text('unfinished')
        with pytest.raises(ValueError):
            await s.publish_artifacts(p, r['id'], job['instance_id'])
        assert not (workspace/'partial.txt').exists()
        await store.close()
    run(scenario())


def test_command_api_and_revision_trace(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.collaboration.api import router, configure
    async def scenario():
        store, service, project, workspace = await setup(tmp_path)
        return store, service, project
    store, service, project = run(scenario())
    configure(service)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        url = f'/api/projects/{project}/collaboration'
        response = client.post(url+'/commands', json={'member':'backend','action':'publish','payload':{'changes':[{'path':'docs/trace.md','content':'# Trace\ncontent\n','summary':'Create trace','base_version':0}]}})
        assert response.status_code == 200, response.text
        doc = response.json()['revisions'][0]['document_id']
        history = client.post(url+'/commands', json={'action':'history','payload':{'document_id':doc}})
        assert history.json()['revisions'][0]['summary'] == 'Create trace'
        assert client.post(url+'/commands', json={'action':'read','payload':{}}).status_code == 409
        assert client.get('/api/projects/missing/collaboration').status_code == 404
    run(store.close())


def test_leader_receipts_wait_for_durable_reply(tmp_path, monkeypatch):
    from app.agents.team import Team
    from langchain_core.messages import AIMessage, HumanMessage
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        conversation = await store.create_conversation(p, 'Chat')
        await s.send(p, 'frontend', 'leader', 'Important delivery', 'delivery', conversation['id'])
        team = Team()
        team.collaboration = s
        async def answer(messages, *, allow_delegation=True):
            return AIMessage(content='Acknowledged')
        monkeypatch.setattr(team.leader, 'next_message', answer)
        events = [event async for event in team.stream([HumanMessage(content='Status')], conversation_id=conversation['id'], workspace_path=workspace)]
        context = f"leader:{conversation['id']}"
        assert await s.inbox(p, 'leader', context, conversation['id'])
        await team.acknowledge_leader(conversation['id'])
        assert not await s.inbox(p, 'leader', context, conversation['id'])
        await store.close()
    run(scenario())


def test_persisted_leader_dispatches_then_wakes_to_accept(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from app.tools._workspace import workspace_root
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    async def fake_worker(self, task, **kwargs):
        (workspace_root()/'delivery.txt').write_text('ready')
        yield 'content', {'content':'Delivery ready at delivery.txt'}
    monkeypatch.setattr(WorkerAgent, 'stream', fake_worker)
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        conversation = await store.create_conversation(p, 'Delivery')
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        team.collaboration = s
        async def leader(messages, *, allow_delegation=True):
            requests = (await s.snapshot(p))['request']
            if not requests:
                return AIMessage(content='', tool_calls=[{'id':'delegate','name':'delegate_task','args':{'worker':'frontend','task':'Deliver file','summary':'Create file','handoff':'Write delivery.txt'}}])
            if requests[0]['state'] == 'review':
                return AIMessage(content='', tool_calls=[{'id':'accept','name':'collaborate','args':{'action':'transition','payload':{'identifier':requests[0]['id'],'state':'completed','result':'Verified delivery'}}}])
            return AIMessage(content='Delivery accepted' if requests[0]['state']=='completed' else 'Work assigned; waiting')
        monkeypatch.setattr(team.leader, 'next_message', leader)
        scheduler = Scheduler(s, team)
        await scheduler.start()
        async def collect():
            result = []
            async for name, payload in team.stream([HumanMessage(content='Build')], conversation_id=conversation['id'], workspace_path=workspace):
                if name == 'chat_message':
                    await store.add_chat_message(conversation['id'], payload)
                result.append((name,payload))
            return result
        try:
            events = await asyncio.wait_for(collect(), 5)
            assert any(name=='delta' for name, _ in events)
            async def accepted():
                while (await s.snapshot(p))['request'][0]['state'] != 'completed':
                    await asyncio.sleep(.02)
            await asyncio.wait_for(accepted(), 5)
            assert (workspace/'delivery.txt').read_text() == 'ready'
        finally:
            await scheduler.close()
            await store.close()
    run(scenario())


def test_retry_failed_request_preserves_workspace_and_unblocks_dependency(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        child = await s.create_request(p, 'leader', 'product', 'Product', 'child')
        parent = await s.create_request(p, 'leader', 'frontend', 'Build', 'parent', dependencies=[child['id']])
        job = await s.claim()
        isolated = await s.prepare_workspace(p, child['id'])
        (isolated.work/'draft.txt').write_text('retained work')
        await s.transition(p, child['id'], 'product', 'failed', 'Recursion limit of 25 reached')
        assert await s.claim() is None
        assert (await s.get_request(p, parent['id']))['blocked_reason'] == 'dependency_failed'
        await s.transition(p, child['id'], 'leader', 'queued', 'Continue original task')
        retried = await s.claim()
        assert retried[2]['id'] == child['id']
        assert retried[2]['workspace_id'] == job[2]['workspace_id']
        assert (await s.prepare_workspace(p, child['id'])).work.joinpath('draft.txt').read_text() == 'retained work'
        await s.transition(p, child['id'], 'product', 'review', 'Ready')
        await s.transition(p, child['id'], 'leader', 'completed', 'Accepted')
        resumed = await s.claim()
        assert resumed[2]['id'] == parent['id']
        assert 'blocked_reason' not in resumed[2]
        await store.close()
    run(scenario())


def test_group_mentions_show_only_summaries_with_durable_details_and_replies(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        c = (await store.create_conversation(p, 'Group'))['id']
        msg = await s.send(p, 'frontend', 'backend', 'Full contract details and internal parameters', 'contract', c, summary='请补充点赞接口的响应格式。')
        assert await s.send(p, 'frontend', 'backend', 'Full contract details and internal parameters', 'contract', c, summary='请补充点赞接口的响应格式。') == msg
        await s.send(p, 'backend', 'frontend', 'Full response shape and examples', 'reply', c, reply_to=msg['id'], summary='响应格式已补齐，请按接口文档接入。')
        detail = await store.get_conversation_detail(c)
        assert len(detail['messages']) == 2
        assert detail['messages'][0]['mentions'][0]['summary'] == '请补充点赞接口的响应格式。'
        assert 'Full' not in str(detail['messages'])
        assert 'Full contract details' in (await s.inspect_message(p, msg['id']))['content']
        inbox = await s.inbox(p, 'backend', 'ctx', c)
        await s.acknowledge(p, 'backend', 'ctx', [item['event_id'] for item in inbox])
        assert (await store.get_conversation_detail(c))['messages'][0]['read_by'] == ['backend']
        with pytest.raises(ValueError):
            await s.send(p, 'frontend', 'backend', 'Details', 'too-long', c, summary='x'*161)
        assert len((await store.get_conversation_detail(c))['messages']) == 2
        await store.close()
    run(scenario())


def test_request_handoff_and_acceptance_appear_as_short_group_replies(tmp_path):
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        c = (await store.create_conversation(p, 'Handoff'))['id']
        r = await s.create_request(p, 'frontend', 'designer', 'Detailed design requirements', 'design', conversation=c, summary='请提供页面风格与布局方案。')
        await s.claim()
        await s.transition(p, r['id'], 'designer', 'review', 'Full delivery details', summary='布局方案已交付，请前端核对。')
        assert not (await store.get_conversation_detail(c))['messages'][0]['completed_by']
        await s.transition(p, r['id'], 'frontend', 'completed', 'Accepted full evidence', summary='方案已确认，我继续实现页面。')
        detail = await store.get_conversation_detail(c)
        assert len(detail['messages']) == 3
        assert [m['sender'] for m in detail['messages']] == ['frontend', 'designer', 'frontend']
        assert detail['messages'][0]['completed_by'] == ['designer']
        assert 'Detailed' not in str(detail['messages']) and 'Full' not in str(detail['messages'])
        events = await store.conversation_event_page(c)
        assert sum(e['type']=='chat_message' for e in events['events']) == 3
        await store.close()
    run(scenario())


def test_single_slot_scheduler_resumes_parent_after_child_review(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from app.collaboration.tools import active
    from langgraph.checkpoint.memory import InMemorySaver
    seen = []
    async def worker(self, task, **kwargs):
        ctx = active.get()
        current = await ctx.service.get_request(ctx.project, ctx.request)
        seen.append((ctx.member, current['attempts']))
        if ctx.member == 'frontend' and current['attempts'] == 1:
            child = await ctx.service.create_request(ctx.project, 'frontend', 'designer', 'Provide style', 'style', conversation=ctx.conversation, parent=ctx.request, summary='请提供视觉方案。')
            await ctx.service.wait_for(ctx.project, ctx.request, [child['id']], 'frontend')
            return
        if ctx.member == 'frontend':
            child = await ctx.service.get_request(ctx.project, current['dependencies'][0])
            assert child['state'] == 'review'
            await ctx.service.transition(ctx.project, child['id'], 'frontend', 'completed', 'Design verified')
        yield 'content', {'content':'Verified delivery'}
    monkeypatch.setattr(WorkerAgent, 'stream', worker)
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        c = (await store.create_conversation(p, 'Flow'))['id']
        root = await s.create_request(p, 'leader', 'frontend', 'Build', 'root', conversation=c)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team, concurrency=1)
        await scheduler.start()
        try:
            async def finished():
                while (await s.get_request(p, root['id']))['state'] != 'review':
                    await asyncio.sleep(.05)
            await asyncio.wait_for(finished(), 5)
            assert seen == [('frontend', 1), ('designer', 1), ('frontend', 2)]
            await s.transition(p, root['id'], 'leader', 'completed', 'Whole delivery accepted')
            assert all(r['state'] == 'completed' for r in (await s.snapshot(p))['request'])
        finally:
            await scheduler.close()
            await store.close()
    run(scenario())


def test_malformed_delivery_result_is_recoverable_before_publication(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from app.collaboration.tools import active
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    stages = []
    class Model:
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages):
            ctx = active.get()
            results = [m for m in messages if isinstance(m, ToolMessage)]
            current = await ctx.service.get_request(ctx.project, ctx.request)
            if not results:
                stages.append('invalid')
                result = {'status':'done', 'details':'Delivered'}
            else:
                assert 'result' in results[-1].content and 'valid string' in results[-1].content
                assert current['state'] == 'running' and not current['workspace_published']
                stages.append('corrected')
                result = 'Delivered; verified requirements'
            return AIMessage(content='', tool_calls=[{'id':f'delivery-{len(results)}','name':'collaborate','args':{'action':'transition','payload':{'identifier':ctx.request,'state':'review','result':result}}}])
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())
    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team)
        r = await s.create_request(p, 'leader', 'product', 'Deliver', 'malformed-result')
        await scheduler.execute(*(await s.claim(scheduler.owner)))
        final = await s.get_request(p, r['id'])
        assert stages == ['invalid','corrected']
        assert final['state'] == 'review' and final['result'] == 'Delivered; verified requirements'
        assert final['workspace_published']
        with pytest.raises(ValueError, match='valid string'):
            await s.transition(p, r['id'], 'leader', 'completed', {'wrong':'type'})
        assert (await s.get_request(p, r['id']))['state'] == 'review'
        await store.close()
    run(scenario())


def test_live_usage_counts_concurrent_instances_before_completion(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team)
        release = asyncio.Event()
        started = asyncio.Queue()
        async def stream(self, task, **kwargs):
            amount = int(task)
            self.record_usage(AIMessage(content='working', usage_metadata={'input_tokens': amount, 'output_tokens': 10, 'total_tokens': amount+10}))
            yield 'usage', {}
            await started.put(True)
            await release.wait()
            yield 'content', {'content': 'done'}
        monkeypatch.setattr(WorkerAgent, 'stream', stream)
        await s.create_request(p, 'leader', 'frontend', '100', 'one')
        await s.create_request(p, 'leader', 'frontend', '200', 'two')
        tasks = [asyncio.create_task(scheduler.execute(*(await s.claim(scheduler.owner)))) for _ in range(2)]
        await started.get()
        await started.get()
        def usage():
            result = team.usage_snapshot()
            return result, next(a for a in result['agents'] if a['id'] == 'frontend')
        total, member = usage()
        assert total['total_tokens'] == member['total_tokens'] == 320
        assert member['context_tokens'] == 200
        assert len(scheduler.active_workers) == 2
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert usage()[0]['total_tokens'] == 320
        release.set()
        await tasks[1]
        assert usage()[0]['total_tokens'] == 320
        assert not scheduler.active_workers
        await store.close()
    run(scenario())
