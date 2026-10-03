"""Regression cases for failures observed in real document tool calls."""
import asyncio
import pytest

from app.collaboration.documents import DocumentEditError, edit_document, outline
from app.collaboration.tools import COLLABORATION_TOOLS, Context, active
from app.collaboration.service import Collaboration
from app.storage import ChatStore


def ids(content):
    return {s['title']: s['id'] for s in outline(content)}


@pytest.mark.parametrize('replacement', ['new body', '```text\nnew code\n```', ''])
def test_replacing_body_without_trailing_newline_preserves_following_sections(replacement):
    original = '# Document\n## First\nold\n\n## Second\nkeep\n### Child\nchild\n'
    sections = ids(original)
    result = edit_document(original, [
        dict(operation='replace_section_body', section_id=sections['First'], new_text=replacement),
        dict(operation='replace_text', section_id=sections['Second'], old_text='keep', new_text='updated'),
    ])
    assert ids(result) == sections
    assert '\n## Second\nupdated\n### Child\nchild\n' in result
    assert '```##' not in result


def test_unclosed_fence_is_rejected_before_it_can_swallow_other_sections():
    original = '# Document\n## First\nold\n## Second\nkeep\n'
    with pytest.raises(DocumentEditError, match='Markdown fences') as error:
        edit_document(original, [dict(operation='replace_section_body', section_id=ids(original)['First'], new_text='```text\nnever closed')])
    assert error.value.details['edit_index'] == 0


def test_repair_that_changes_body_boundaries_identifies_the_dependent_edit():
    # Actual failure pattern: first fix a glued heading, then edit text that has
    # moved to its own section using the old containing section's ID.
    original = '# Guide\n## First\nbody### Second\nmarker\n'
    first = ids(original)['First']
    with pytest.raises(DocumentEditError, match=r'edits\[1\].*match exactly once') as error:
        edit_document(original, [
            dict(operation='replace_text', section_id=first, old_text='body### Second', new_text='body\n\n### Second'),
            dict(operation='replace_text', section_id=first, old_text='marker', new_text='new'),
        ])
    assert error.value.details == {'edit_index': 1, 'section_id': first, 'operation': 'replace_text'}


def test_typed_publish_read_and_failed_batch_are_atomic(tmp_path):
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "docs.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        token = active.set(Context(service, project['id'], 'frontend', '', 'ctx'))
        publish = COLLABORATION_TOOLS['collaboration_publish']
        read = COLLABORATION_TOOLS['collaboration_read']
        try:
            original = '# Guide\n## First\nold\n## Second\nkeep\n'
            result = await publish.ainvoke({'changes': [{'path': 'docs/frontend/guide.md', 'base_version': 0, 'summary': 'Create', 'content': original}]})
            doc = result['revisions'][0]['document_id']
            first = ids(original)['First']
            failed = await publish.ainvoke({'changes': [{'document_id': doc, 'base_version': 1, 'summary': 'Unsafe fence', 'edits': [
                {'operation': 'replace_text', 'section_id': first, 'old_text': 'old', 'new_text': 'changed'},
                {'operation': 'replace_section_body', 'section_id': first, 'new_text': '```text\nunclosed'},
            ]}]})
            assert failed['code'] == 'invalid_document_edit' and failed['edit_index'] == 1
            assert (tmp_path / 'docs/frontend/guide.md').read_text() == original
            assert (await service.catalog(project['id']))[0]['version'] == 1
            assert not (await service.snapshot(project['id']))['pending']
            updated = await publish.ainvoke({'changes': [{'document_id': doc, 'base_version': 1, 'summary': 'Safe body', 'edits': [
                {'operation': 'replace_section_body', 'section_id': first, 'new_text': '```text\nnew\n```'},
            ]}]})
            assert updated['revisions'][0]['version'] == 2
            result = await read.ainvoke({'document_id': doc, 'version': 2, 'section_id': ids(original)['Second']})
            assert result['content'] == 'keep\n'
            missing = await read.ainvoke({'document_id': doc, 'version': 2, 'section_id': 'missing'})
            assert doc in missing['error'] and 'v2' in missing['error']
        finally:
            active.reset(token)
            await store.close()
    asyncio.run(scenario())


def test_handoff_budget_keeps_complete_records_and_reports_omissions():
    import json
    from app.collaboration.context import encode_handoff
    handoff = {'documents': [{'id': f'doc-{i}', 'version': 1, 'title': 'a' * 1200} for i in range(30)],
               'pending': [{'id': 'request-1', 'dependencies': []}], 'decisions': [],
               'remaining': {'documents': 2, 'pending': 0, 'decisions': 0}}
    encoded = encode_handoff(handoff)
    data = json.loads(encoded)
    assert len(encoded) <= 10000
    assert data['remaining']['documents'] + len(data['documents']) == 32
    assert all(d['title'] == 'a' * 1200 for d in data['documents'])
    assert data['pending'] == handoff['pending']
    assert len(handoff['documents']) == 30  # Do not mutate service data.


def test_worker_executes_typed_tools_with_one_activity_per_call(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.runnables import RunnableLambda
    from app.agents.base import AgentSpec
    from app.agents.worker import WorkerAgent
    original = '# Guide\n## First\nbody\n'
    class Model:
        def bind_tools(self, tools):
            names = {t['function']['name'] for t in tools}
            assert 'collaboration_publish' in names and 'collaborate' not in names
            assert 'collaboration_recover' not in names
            return RunnableLambda(respond)
    async def respond(messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return AIMessage(content='', tool_calls=[{'id': 'publish', 'name': 'collaboration_publish', 'args': {
                'changes': [{'path': 'docs/frontend/spec.md', 'base_version': 0, 'summary': 'create', 'content': original}]}}])
        import json
        assert json.loads(results[-1].content)['revisions'][0]['version'] == 1
        return AIMessage(content='Published')
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "worker.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        worker = WorkerAgent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md'))
        monkeypatch.setattr(worker, 'model', lambda: Model())
        token = active.set(Context(service, project['id'], 'frontend', '', 'ctx'))
        try:
            events = [e async for e in worker.stream('Publish', conversation_id='worker', context_mode='fresh', handoff='')]
            activities = [e[1] for e in events if e[0] == 'activity']
            assert [a['phase'] for a in activities] == ['running', 'completed']
            assert all(a['description'] == '发布文档修订' and a['tool'] == 'collaboration_publish' for a in activities)
        finally:
            active.reset(token)
            await store.close()
    asyncio.run(scenario())


def test_leader_checkpoint_does_not_suppress_bodies_removed_from_context(tmp_path, monkeypatch):
    import json
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from app.agents.team import Team
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "leader.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        doc = (await service.publish(project['id'], 'frontend', [{'path': 'docs/spec.md', 'summary': 'create', 'content': 'important body'}]))[0]['document_id']
        team = Team()
        calls = []
        async def respond(messages, **kwargs):
            step = len(calls)
            calls.append(active.get().read_context)
            if step in (0, 2):
                return AIMessage(content='', tool_calls=[{'id': f'read-{step}', 'name': 'collaboration_read', 'args': {'document_id': doc, 'version': 1, 'section_id': 'root'}}])
            result = [m for m in messages if isinstance(m, ToolMessage)][-1]
            assert json.loads(result.content)['content'] == 'important body'
            if step == 1:
                return AIMessage(content='', tool_calls=[{'id': 'checkpoint', 'name': 'checkpoint_context', 'args': {'goal': 'Continue'}}])
            return AIMessage(content='Done')
        monkeypatch.setattr(team.leader, 'next_message', respond)
        token = active.set(Context(service, project['id'], 'leader', '', 'ctx', read_context='before'))
        try:
            events = [e async for e in team._run([HumanMessage(content='Work')], 'leader')]
            assert calls[0] == calls[1] == 'before'
            assert calls[2] == calls[3] != 'before'
            assert any(e[0] == 'delta' and e[1]['content'] == 'Done' for e in events)
        finally:
            active.reset(token)
            await store.close()
    asyncio.run(scenario())


def test_worker_rebuilds_tools_when_persistence_context_changes(monkeypatch):
    from app.agents.base import AgentSpec
    from app.agents.worker import WorkerAgent
    from app.agents.tools.current_utc_time import current_utc_time
    from langchain_core.runnables import RunnableLambda
    from langchain_core.messages import AIMessage
    bound = []
    class Model:
        def bind_tools(self, tools):
            bound.append({t['function']['name'] for t in tools})
            return RunnableLambda(lambda messages: AIMessage(content='Done'))
    worker = WorkerAgent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md', tools=(current_utc_time,)))
    monkeypatch.setattr(worker, 'model', lambda: Model())
    plain = worker.build_graph()
    token = active.set(Context(object(), 'p', 'frontend', '', 'ctx'))
    try:
        persisted = worker.build_graph()
        assert persisted is not plain
        assert worker.build_graph() is persisted
        assert bound[0] == {'checkpoint_context', 'recall_context', 'enable_tools'}  # current_utc_time loads with the time group
        assert 'collaboration_read' in bound[1]
        active.get().request = 'durable-request'
        assert worker.build_graph() is not persisted
        assert 'collaboration_wait' in bound[2]
    finally:
        active.reset(token)
