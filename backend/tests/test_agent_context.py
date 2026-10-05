"""Inspecting each agent's model-facing context of a session."""
import asyncio

import pytest
from fastapi import HTTPException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from app import main
from app.agents.context_view import message_entry
from app.storage import ChatStore


def test_message_entries_keep_calls_results_and_reasoning():
    call = AIMessage(content='', tool_calls=[{'id': 'c1', 'name': 'read_project_file', 'args': {'path': 'a.py'}}],
                     additional_kwargs={'reasoning_content': 'need a.py'})
    entry = message_entry(call)
    assert entry['type'] == 'ai' and entry['reasoning'] == 'need a.py'
    assert entry['tool_calls'] == [{'id': 'c1', 'name': 'read_project_file', 'arguments': '{"path": "a.py"}'}]
    result = message_entry(ToolMessage(content=[{'type': 'text', 'text': 'line 1'}], tool_call_id='c1', name='read_project_file', status='error'))
    assert result == {'id': None, 'type': 'tool', 'content': 'line 1', 'tool_call_id': 'c1', 'name': 'read_project_file', 'status': 'error'}


def test_session_lists_leader_and_task_contexts_and_returns_their_messages(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from app.collaboration.service import Collaboration

    class Model(BaseChatModel):
        """Calls one tool, then answers."""
        @property
        def _llm_type(self):
            return 'fake-context'

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if not any(isinstance(m, ToolMessage) for m in messages):
                message = AIMessage(content='', tool_calls=[{'id': 'time-1', 'name': 'current_utc_time', 'args': {}}])
            else:
                message = AIMessage(content='It is done.')
            return ChatResult(generations=[ChatGeneration(message=message)])
    monkeypatch.setattr(WorkerAgent, 'model', lambda self, **_: Model())

    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/context.db')
        project = (await store.create_project('Context', str(tmp_path)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        team.leader_contexts[conversation] = [SystemMessage(content='leader prompt'), HumanMessage(content='hi')]
        request = await service.create_request(project, 'leader', 'product', 'Tell the time', 'time', conversation=conversation)
        await Scheduler(service, team).execute(*(await service.claim('test')))

        async def execution_context(_):
            return service, project
        monkeypatch.setattr(main, 'execution_context', execution_context)
        monkeypatch.setattr(main, 'get_team', lambda: team)

        contexts = (await main.agent_contexts(conversation))['contexts']
        thread = (await service.journal(project, request['id']))['attempts'][-1]['thread']
        assert [c['key'] for c in contexts] == ['leader', f"task:{request['id']}:{thread}"]
        assert contexts[1]['agent'] == 'product' and contexts[1]['running'] is False
        # A resumed execution keeps its attempt number and thread: still one context.
        await service.journal_attempt_start(project, request['id'], 1, 'resumed', 'continue')
        await service.journal_set_thread(project, request['id'], 'resumed', thread)
        assert len((await main.agent_contexts(conversation))['contexts']) == 2

        leader = await main.agent_context(conversation, key='leader')
        assert [m['type'] for m in leader['messages']] == ['system', 'human']
        task = await main.agent_context(conversation, key=contexts[1]['key'])
        types = [m['type'] for m in task['messages']]
        assert types[0] == 'system' and 'tool' in types and task['messages'][-1]['content'] == 'It is done.'
        call = next(m for m in task['messages'] if m.get('tool_calls'))['tool_calls'][0]
        assert call['name'] == 'current_utc_time'
        assert next(m for m in task['messages'] if m['type'] == 'tool')['tool_call_id'] == call['id']

        unchanged = await main.agent_context(conversation, key=contexts[1]['key'], revision=task['revision'])
        assert unchanged['unchanged'] and 'messages' not in unchanged
        with pytest.raises(HTTPException) as missing:
            await main.agent_context(conversation, key='task:other:1')
        assert missing.value.status_code == 404
        await store.close()
    asyncio.run(run())
