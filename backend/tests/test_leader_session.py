"""The Leader keeps one session per conversation and sees only what changed; workers
continue their own context when a delivery is sent back for rework."""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.team import Team
from app.collaboration.context import coordination_update
from app.collaboration.service import Collaboration
from app.storage import ChatStore


def test_coordination_updates_are_full_once_then_deltas():
    handoff = {'pending': [{'id': 'a', 'state': 'queued', 'recipient': 'backend'}], 'documents': [{'id': 'd', 'version': 1, 'title': 'API'}],
               'decisions': [], 'environment': {'services': []}, 'remaining': {}}
    full, snapshot = coordination_update(handoff, None)
    assert full.startswith('Current coordination state') and snapshot['pending'] == {'a': 'queued'}
    same, _ = coordination_update(handoff, snapshot)
    assert same.startswith('Coordination update since your last turn: no task') and '1 tasks still pending' in same
    changed = {**handoff, 'pending': [{'id': 'a', 'state': 'review', 'recipient': 'backend'}, {'id': 'b', 'state': 'queued', 'recipient': 'frontend'}],
               'documents': [{'id': 'd', 'version': 2, 'title': 'API'}], 'environment': {'services': [{'name': 'api'}]}}
    delta, _ = coordination_update(changed, snapshot)
    assert '"tasks_changed"' in delta and '"b"' in delta and '"documents_new_or_updated"' in delta and '"environment"' in delta
    finished, _ = coordination_update({**handoff, 'pending': []}, snapshot)
    assert '"tasks_no_longer_pending": ["a"]' in finished


def test_leader_continues_its_session_across_turns(tmp_path, monkeypatch):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/leader.db')
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        project = (await store.create_project('Leader', str(workspace)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        team.collaboration = service
        seen = []

        async def answer(messages, **kwargs):
            seen.append(list(messages))
            if len(seen) == 1:
                return AIMessage(content='', tool_calls=[{'id': 'catalog-1', 'name': 'collaboration_catalog', 'args': {}}])
            return AIMessage(content=f'reply {len(seen)}')
        monkeypatch.setattr(team.leader, 'next_message', answer)
        history = [HumanMessage(content='Build the page')]
        async for _ in team.stream(history, conversation_id=conversation, workspace_path=workspace):
            pass
        assert seen[0][-1].content.startswith('Current coordination state') or any(
            str(m.content).startswith('Current coordination state') for m in seen[0])
        await service.create_request(project, 'leader', 'backend', 'Implement the API', 'api', conversation=conversation)
        follow_up = [*history, AIMessage(content='reply 2'), HumanMessage(content='How is it going?')]
        restarted = Team()  # a new process: the session comes from storage
        await restarted.configure_persistence(store, InMemorySaver())
        restarted.collaboration = service
        monkeypatch.setattr(restarted.leader, 'next_message', answer)
        async for _ in restarted.stream(follow_up, conversation_id=conversation, workspace_path=workspace):
            pass
        latest = seen[-1]
        # Earlier tool calls and results are still in context, the reply appears once,
        # and the new turn adds only a delta.
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'catalog-1' for m in latest)
        assert sum(isinstance(m, AIMessage) and m.content == 'reply 2' for m in latest) == 1
        updates = [m for m in latest if m.additional_kwargs.get('coordination_state')]
        assert len(updates) == 2 and updates[-1].content.startswith('Coordination update since your last turn')
        assert 'leader:api' in updates[-1].content
        assert latest[-1].content.startswith('Coordination update') or latest[-2].content == 'How is it going?' or any(
            m.content == 'How is it going?' for m in latest)
        # The second turn's prompt starts with the whole first turn: an append-only prefix.
        first_turn = [m for m in seen[1] if not m.additional_kwargs.get('goal_state')]
        assert [m.content for m in latest[:len(first_turn)]] == [m.content for m in first_turn]
        await store.close()
    asyncio.run(run())


def test_rework_continues_the_workers_context_with_feedback(tmp_path, monkeypatch):
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    calls = []

    class Model:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            from app.collaboration.tools import active
            calls.append(messages)
            ctx = active.get()
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[{'id': f'deliver-{len(calls)}', 'name': 'collaborate', 'args': {'action': 'transition',
                    'payload': {'identifier': ctx.request, 'state': 'review', 'result': f'Delivered round {len(calls)}'}}}])
            return AIMessage(content='Done.')
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/rework.db')
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        project = (await store.create_project('Rework', str(workspace)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(service, team)
        request = await service.create_request(project, 'leader', 'backend', 'Implement the API', 'api', conversation=conversation)
        await scheduler.execute(*(await service.claim(scheduler.owner)))
        assert (await service.get_request(project, request['id']))['state'] == 'review'
        await service.transition(project, request['id'], 'leader', 'queued', result='Return 404 JSON for unknown paths')
        job = await service.claim(scheduler.owner)
        assert job[2]['resume'] and job[2]['rework'] == 'Return 404 JSON for unknown paths'
        await scheduler.execute(*job)
        assert len(calls) == 2  # delivering ends attempt 1 after one call
        resumed = calls[1]
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'deliver-1' for m in resumed)
        note = [m for m in resumed if isinstance(m, HumanMessage) and 'Rework requested by leader' in m.content]
        assert note and 'Return 404 JSON for unknown paths' in note[-1].content
        journal = await service.journal(project, request['id'])
        assert [a['mode'] for a in journal['attempts']] == ['fresh', 'continue']
        await store.close()
    asyncio.run(run())
