import asyncio
import json
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from app.collaboration.payloads import ACTION_PAYLOADS
from app.collaboration.tools import Context, active, collaborate
from app.tools.validation import tool, tool_error


@pytest.mark.parametrize('action', ACTION_PAYLOADS)
def test_every_collaboration_action_rejects_unknown_fields_before_io(action):
    async def scenario():
        # No service implementation: validation must happen before any I/O.
        token = active.set(Context(object(), 'p', 'leader', 'c', 'ctx'))
        try:
            result = await collaborate.ainvoke({'action': action, 'payload': {'unexpected': True}})
            assert 'error' in result
            assert 'unexpected' in result['error']
        finally:
            active.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize('action,payload', [
    ('publish', {'changes': [{'path': 'docs/a.md', 'summary': {}, 'content': 'a'}]}),
    ('publish', {'changes': [{'document_id': 'd', 'summary': 'edit', 'edits': [{'operation': 'replace_text', 'section_id': 'root', 'old_text': 'a', 'new_text': {}}]}]}),
    ('transition', {'identifier': 'r', 'state': 'completed', 'result': {}}),
    ('send', {'recipient': 'backend', 'content': {}, 'key': 'k'}),
    ('request', {'recipient': 'backend', 'goal': 'a', 'key': 'k', 'dependencies': 'r'}),
    ('request', {'recipient': 'backend', 'goal': 'a', 'key': 'k', 'parent': 'forged'}),
    ('read', {'document_id': 'd', 'offset': 'bad'}),
    ('subscribe', {'document_id': 'd', 'enabled': 'false'}),
    ('decision', {'title': {}, 'rationale': 'x', 'links': []}),
    ('schedule', {'identifier': 'r', 'deadline': float('nan')}),
])
def test_nested_collaboration_validation(action, payload):
    async def scenario():
        token = active.set(Context(object(), 'p', 'leader', 'c', 'ctx'))
        try:
            assert 'error' in await collaborate.ainvoke({'action': action, 'payload': payload})
        finally:
            active.reset(token)
    asyncio.run(scenario())


def test_all_role_tools_reject_extra_arguments():
    from app.agents.team import Team
    team = Team()
    all_tools = {t.name: t for agent in (team.leader, *team.workers.values()) for t in agent.spec.tools}
    all_tools['collaborate'] = collaborate
    for name, candidate in all_tools.items():
        with pytest.raises(ValueError):
            candidate.args_schema.model_validate({'unexpected': True})
        assert candidate.args_schema.model_config['extra'] == 'forbid', name


def test_tool_node_recovers_errors_but_propagates_cancellation():
    @tool
    async def flaky(path: str) -> str:
        """A filesystem failure should be a tool result."""
        if path == 'cancel':
            await asyncio.Event().wait()
        raise PermissionError('Read-only file')
    async def scenario():
        from langgraph.graph import StateGraph, MessagesState, START, END
        graph = StateGraph(MessagesState)
        graph.add_node('tools', ToolNode([flaky], handle_tool_errors=tool_error))
        graph.add_edge(START, 'tools')
        graph.add_edge('tools', END)
        node = graph.compile()
        for args in ({'path': {}}, {'path': 'a'}, {'path': 'a', 'extra': 1}):
            result = await node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name': 'flaky', 'id': 't', 'args': args}])]})
            message = result['messages'][-1]
            assert message.status == 'error'
        task = asyncio.create_task(node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name': 'flaky', 'id': 't', 'args': {'path': 'cancel'}}])]}))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_legacy_delivery_result_can_be_accepted(tmp_path):
    from app.storage import ChatStore
    from app.collaboration.service import Collaboration
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "legacy.db"}')
        project = await store.create_project('Legacy', str(tmp_path))
        service = Collaboration(store)
        request = await service.create_request(project['id'], 'leader', 'backend', 'work', 'key')
        async with service.transaction() as c:
            request.update(state='review', result={'evidence': ['tests passed']})
            await service._put(c, 'request', project['id'], request['id'], request)
        result = await service.transition(project['id'], request['id'], 'leader', 'completed')
        assert json.loads(result['result']) == {'evidence': ['tests passed']}
        assert result['state'] == 'completed'
        await store.close()
    asyncio.run(scenario())


def test_leader_recovers_invalid_outer_tool_arguments(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.storage import ChatStore
    from app.collaboration.service import Collaboration
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "leader.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        async def reply(messages, **kwargs):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content='', tool_calls=[{'name': 'collaborate', 'id': 'bad', 'args': {'action': 'unknown', 'payload': []}}])
            assert 'error' in results[-1].content.lower()
            return AIMessage(content='Recovered')
        monkeypatch.setattr(team.leader, 'next_message', reply)
        token = active.set(Context(service, project['id'], 'leader', '', 'ctx'))
        try:
            events = [event async for event in team._run([HumanMessage(content='Inspect')], 'ctx')]
            assert any(kind == 'delta' and data['content'] == 'Recovered' for kind, data in events)
            assert any(kind == 'activity' and data['phase'] == 'failed' for kind, data in events)
        finally:
            active.reset(token)
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('output,failed', [
    ({'error': 'Invalid result'}, True),
    (ToolMessage(content='{"error":"Invalid result"}', tool_call_id='t'), True),
    (ToolMessage(content='{"passed":false,"status":500}', tool_call_id='t'), True),
    (ToolMessage(content='{"passed":true}', tool_call_id='t'), False),
    (ToolMessage(content='regular content', tool_call_id='t'), False),
])
def test_rejected_tool_results_are_not_logged_as_completed(output, failed):
    from app.tools.validation import tool_result_failed
    assert tool_result_failed(output) is failed
