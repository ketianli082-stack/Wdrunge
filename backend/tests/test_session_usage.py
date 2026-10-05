"""Tokens and cache hit rate per session (conversation), by agent, purpose and task."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app import main
from app.agents.base import Agent, AgentSpec
from app.agents.usage import record_call, usage_scope
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active
from app.storage import ChatStore


def reply(input_tokens, cached, output=10, model='qwen-test'):
    message = AIMessage(content='ok', usage_metadata={'input_tokens': input_tokens, 'output_tokens': output,
                                                      'total_tokens': input_tokens + output,
                                                      'input_token_details': {'cache_read': cached}})
    message.response_metadata.update(model_name=model, wdrunge_latency_ms=1200, wdrunge_retries=1)
    return message


def test_usage_is_attributed_to_sessions_tasks_and_purposes(tmp_path):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/usage.db')
        project = (await store.create_project('Usage', str(tmp_path)))['id']
        first = (await store.create_conversation(project))['id']
        second = (await store.create_conversation(project))['id']
        leader = Agent(AgentSpec(id='leader', name='Leader', prompt_file='leader.md'))
        backend = Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        leader._store = backend._store = store
        token = usage_scope.set({'conversation_id': first})
        try:
            await leader.record_usage_durable(reply(1000, 800))
            await leader.record_usage_durable(reply(500, 0), purpose='summary')
            await record_call(store, 'reviewer', reply(200, 100), 'review')
        finally:
            usage_scope.reset(token)
        service = SimpleNamespace()
        token = active.set(Context(service, project, 'backend', first, 'ctx', request='leader:api'))
        try:
            await backend.record_usage_durable(reply(2000, 1800))
        finally:
            active.reset(token)
        token = usage_scope.set({'conversation_id': second})
        try:
            await backend.record_usage_durable(reply(9000, 0))
        finally:
            usage_scope.reset(token)
        await store.record_agent_usage('backend', 50, 40, None)  # recorded before session attribution existed
        usage = await store.conversation_usage(first)
        totals = usage['totals']
        assert totals['calls'] == 4 and totals['input_tokens'] == 3700 and totals['output_tokens'] == 40
        assert totals['cached_tokens'] == 2700 and totals['cache_percent'] == round(100 * 2700 / 3700, 1)
        assert totals['latency_ms'] == 1200 and totals['retries'] == 4
        by_agent = {g['key']: g for g in usage['by_agent']}
        assert set(by_agent) == {'leader', 'backend', 'reviewer'} and by_agent['backend']['cache_percent'] == 90.0
        assert {g['key'] for g in usage['by_purpose']} == {'coordination', 'summary', 'review', 'work'}
        assert {g['key']: g['calls'] for g in usage['by_task']} == {None: 3, 'leader:api': 1}
        assert usage['by_model'][0]['key'] == 'qwen-test'
        assert (await store.conversation_usage(second))['totals']['total_tokens'] == 9010
        await store.close()
    asyncio.run(run())


def test_scheduled_task_calls_carry_session_task_and_attempt(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    class Model(BaseChatModel):
        """A real chat model, so the graph emits the usual model-end events."""
        @property
        def _llm_type(self):
            return 'fake-usage'
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=reply(3000, 2500))])
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/task.db')
        project = (await store.create_project('Task', str(tmp_path)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(service, team)
        request = await service.create_request(project, 'leader', 'backend', 'Explain', 'explain', conversation=conversation)
        await scheduler.execute(*(await service.claim(scheduler.owner)))
        usage = await store.conversation_usage(conversation)
        assert usage['by_task'][0]['key'] == request['id'] and usage['by_task'][0]['cache_percent'] == round(100 * 2500 / 3000, 1)
        async with store.engine.connect() as connection:
            from sqlalchemy import select
            from app.storage import agent_usage
            row = (await connection.execute(select(agent_usage).where(agent_usage.c.request_id == request['id']))).mappings().first()
        assert row['attempt'] == 1 and row['project_id'] == project and row['purpose'] == 'work'
        await store.close()
    asyncio.run(run())


def test_session_usage_endpoint_reports_tokens_cache_approvals_and_failures(tmp_path, monkeypatch):
    test_store = ChatStore(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setattr(main, 'store', test_store)
    monkeypatch.setattr(main, '_runtime_store_identity', None)
    monkeypatch.setattr(main, '_checkpoint_connection', None)
    with TestClient(main.app) as client:
        project = client.post('/api/projects', json={'name': 'Usage', 'workspace_path': str(tmp_path)}).json()
        conversation = client.post(f"/api/projects/{project['id']}/conversations", json={'title': 'S'}).json()
        async def seed():
            token = usage_scope.set({'conversation_id': conversation['id']})
            try:
                await record_call(test_store, 'leader', reply(1000, 750), 'coordination')
            finally:
                usage_scope.reset(token)
            await test_store.upsert_activity(conversation['id'], {'id': 'a1', 'agent': 'backend', 'tool': 'execute_command', 'description': 'x', 'phase': 'failed'})
            await test_store.upsert_activity(conversation['id'], {'id': 'a2', 'agent': 'backend', 'tool': 'execute_command', 'description': 'x', 'phase': 'completed'})
        client.portal.call(seed)
        data = client.get(f"/api/conversations/{conversation['id']}/usage").json()
        assert data['totals']['total_tokens'] == 1010 and data['totals']['cache_percent'] == 75.0
        assert data['tool_calls'] == {'total': 2, 'failed': 1}
        assert set(data['approvals']) == {'human', 'automatic', 'allow_all', 'pending'}
        assert client.get('/api/conversations/missing/usage').status_code == 404


def test_reasoning_tokens_are_recorded_separately(tmp_path):
    """Reasoning (thinking) tokens count toward output tokens and max_tokens; Blog2 could
    not tell them apart from the visible output."""
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/reasoning.db')
        project = (await store.create_project('Usage', str(tmp_path)))['id']
        conversation = (await store.create_conversation(project))['id']
        backend = Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        backend._store = store
        thinking = reply(1000, 0, output=251)
        thinking.usage_metadata['output_token_details'] = {'reasoning': 246}
        token = usage_scope.set({'conversation_id': conversation})
        try:
            await backend.record_usage_durable(thinking)
            await record_call(store, 'reviewer', reply(100, 0), 'review')
        finally:
            usage_scope.reset(token)
        usage = await store.conversation_usage(conversation)
        assert usage['totals']['output_tokens'] == 261 and usage['totals']['reasoning_tokens'] == 246
        assert {g['key']: g['reasoning_tokens'] for g in usage['by_agent']} == {'backend': 246, 'reviewer': 0}
        await store.close()
    asyncio.run(run())
