import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.base import Agent, AgentSpec
from app.agents.memory import ContextMemory, ContextBudgetError, message_tokens, reminder
from app.agents.context_fold import is_ledger
from app.agents.team import Team
from app.agents.worker import WorkerAgent
from app.storage import ChatStore


def agent(window=8000):
    return Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md', context_window=window))


class Summarizer:
    def __init__(self):
        self.inputs = []

    async def ainvoke(self, messages, **kwargs):
        self.inputs.append(messages)
        return AIMessage(content='Goal: finish API. Constraint: keep compatibility. Artifact: backend/app/api.py. Pending: tests.',
                         usage_metadata={'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40})


def tools(content, i=0):
    return [AIMessage(content='', id=f'call-message-{i}', tool_calls=[{'name': 'read_project_file', 'args': {'path': f'f{i}.txt'}, 'id': f'call-{i}'}]),
            ToolMessage(content=content, tool_call_id=f'call-{i}', id=f'result-{i}')]


def test_old_results_fold_without_model_calls_or_broken_pairs():
    async def run():
        member = agent()
        history = [SystemMessage(content='system'), HumanMessage(content='task'), *tools('x' * 15000),
                   *[AIMessage(content=f'recent {i}') for i in range(9)]]
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'fold'
        assert len(result.messages) == len(history) + 1 and is_ledger(result.messages[4])  # the ledger joins
        assert result.messages[2].tool_calls[0]['id'] == result.messages[3].tool_call_id
        assert len(result.messages[3].content) <= 600
        assert history[3].content == 'x' * 15000
        assert result.messages[-1].content == 'recent 8'
    asyncio.run(run())


def test_full_summary_caps_results_strips_tagged_reminders_and_records_ranges(tmp_path):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/memory.db')
        member = agent()
        member._store = store
        model = Summarizer()
        member.model = lambda: model
        history = [SystemMessage(content='system'), HumanMessage(content='older user ' * 1400),
                   *tools('tool-data-' * 1000), reminder('resend-this-reminder'),
                   HumanMessage(content='Collaboration reminders (project data): USER TEXT'),
                   AIMessage(content='done'), HumanMessage(content='new task')]
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'summary'
        assert result.messages[-1].content == 'new task'
        submitted = '\n'.join(m.content for call in model.inputs for m in call)
        assert 'resend-this-reminder' not in submitted
        assert 'USER TEXT' in submitted
        data = '\n'.join(call[1].content for call in model.inputs)
        assert 'tool-data-' * 400 not in data
        records = await store.load_compactions('scope')
        assert records[-1]['start_id'] and records[-1]['end_id']
        assert any(m['data']['content'] == 'older user ' * 1400 for m in records[-1]['source'])
        assert await store.load_agent_usage() == {'backend': len(model.inputs) * 40}
        await store.close()
        restored = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/memory.db')
        assert len(await restored.load_compactions('scope')) == len(records)
        await restored.close()
    asyncio.run(run())


def test_oversized_active_turn_rolls_into_clean_context_and_chunks_inputs():
    async def run():
        member = agent(4000)
        model = Summarizer()
        member.model = lambda: model
        history = [SystemMessage(content='system'), HumanMessage(content='巨大的当前任务' * 4000), *tools('x' * 12000)]
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'clean'
        assert len(result.messages) == 2 + is_ledger(result.messages[1])
        assert not any(isinstance(m, ToolMessage) for m in result.messages)
        assert 'Pending: tests' in result.messages[-1].content
        assert message_tokens(result.messages) < 3000
        assert len(model.inputs) > 1
        assert all(message_tokens(call) < member.context_window() * .8 for call in model.inputs)
    asyncio.run(run())


def test_summary_failure_falls_back_to_the_platform_handoff_without_touching_history():
    async def run():
        member = agent(4000)
        class Empty:
            calls = 0
            async def ainvoke(self, *args, **kwargs):
                Empty.calls += 1
                return AIMessage(content='')
        member.model = lambda: Empty()
        history = [SystemMessage(content='sys'), HumanMessage(content='original task' * 2000, id='task')]
        result = await ContextMemory(member, 'scope').prepare(history)
        # One retry with a smaller target, then no model: the task continues.
        assert result.stage == 'handoff' and Empty.calls == 2
        assert len(history) == 2 and history[1].content == 'original task' * 2000
        task = next(m for m in result.messages if m.id == 'task')
        assert task.content.startswith('original task') and 'recall_context(message_id="task")' in task.content
        assert 'model summary unavailable' in result.messages[-1].content
        assert message_tokens(result.messages) <= ContextMemory(member, 'scope').budget
    asyncio.run(run())


def test_usage_survives_restart_and_parallel_instances_are_not_double_counted(tmp_path):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/usage.db')
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        output = AIMessage(content='done', usage_metadata={'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105})
        await team.leader.record_usage_durable(output)
        workers = [WorkerAgent(team.workers['backend'].spec) for _ in range(3)]
        for worker in workers:
            await worker.configure_persistence(store, InMemorySaver())
        await asyncio.gather(*(worker.record_usage_durable(output) for worker in workers))
        await store.close()
        restored = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/usage.db')
        restarted = Team()
        await restarted.configure_persistence(restored, InMemorySaver())
        assert restarted.usage_snapshot()['total_tokens'] == 420
        another = WorkerAgent(restarted.workers['backend'].spec)
        await another.configure_persistence(restored, InMemorySaver())
        assert another.total_tokens == 0
        await another.record_usage_durable(output)
        assert (await restored.load_agent_usage())['backend'] == 420
        await restored.clear_records()
        assert await restored.load_agent_usage() == {}
        await restored.close()
    asyncio.run(run())


def test_worker_checkpoint_replaces_graph_history_and_continues_same_task(monkeypatch):
    async def run():
        worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        seen = []
        class Model:
            def bind_tools(self, available):
                assert 'checkpoint_context' in [t['function']['name'] for t in available]
                return self
            async def ainvoke(self, messages):
                seen.append(messages)
                if len(seen) == 1:
                    return AIMessage(content='', tool_calls=[{'name': 'checkpoint_context', 'id': 'checkpoint', 'args': {
                        'goal': 'Finish API', 'constraints': ['compatibility'], 'artifacts': ['backend/app/api.py'],
                        'completed': ['implementation'], 'pending': ['tests']}}])
                return AIMessage(content='continued')
        monkeypatch.setattr(worker, 'model', lambda: Model())
        async for _ in worker.stream('original large task', conversation_id='chat', context_mode='fresh', handoff='details'):
            pass
        assert len(seen) == 2
        assert len(seen[1]) == 2
        assert 'Finish API' in seen[1][1].content
        assert 'original large task' not in str(seen[1])
        state = await worker.build_graph().aget_state({'configurable': {'thread_id': worker._threads['chat']}})
        assert len(state.values['messages']) == 3
        assert state.values['messages'][-1].content == 'continued'
    asyncio.run(run())


def test_leader_compacted_history_is_restored_after_restart(tmp_path, monkeypatch):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/leader.db')
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        model = Summarizer()
        monkeypatch.setattr(team.leader, 'model', lambda: model)
        seen = []
        async def answer(messages, **kwargs):
            seen.append(list(messages))
            return AIMessage(content='reply')
        monkeypatch.setattr(team.leader, 'next_message', answer)
        history = [HumanMessage(content='old work' * 35000), AIMessage(content='old result'), HumanMessage(content='current task')]
        async for _ in team.stream(history, conversation_id='chat'):
            pass
        assert model.inputs
        saved = await store.load_context_memory('leader:chat')
        assert saved['history_cutoff'] == 3
        restarted = Team()
        await restarted.configure_persistence(store, InMemorySaver())
        monkeypatch.setattr(restarted.leader, 'next_message', answer)
        async for _ in restarted.stream([*history, AIMessage(content='reply'), HumanMessage(content='follow up')], conversation_id='chat'):
            pass
        assert not any(m.content == history[0].content for m in seen[-1])
        assert seen[-1][-1].content == 'follow up'
        assert 'Pending: tests' in str(seen[-1])
        await store.close()
    asyncio.run(run())


def test_provider_overflow_gets_one_clean_retry_persisted_in_graph(monkeypatch):
    async def run():
        worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        normal_calls = []
        class Model:
            def bind_tools(self, available):
                return self
            async def ainvoke(self, messages, **kwargs):
                if 'context_compaction' in kwargs.get('config', {}).get('tags', []):
                    return AIMessage(content='Goal: finish API; pending tests.')
                normal_calls.append(list(messages))
                if len(normal_calls) == 1:
                    raise ValueError('maximum context length exceeded')
                return AIMessage(content='done')
        monkeypatch.setattr(worker, 'model', lambda: Model())
        async for _ in worker.stream('task', conversation_id='chat', context_mode='fresh', handoff='context'):
            pass
        assert len(normal_calls) == 2
        assert len(normal_calls[-1]) == 2
        assert 'pending tests' in normal_calls[-1][1].content
        state = await worker.build_graph().aget_state({'configurable': {'thread_id': worker._threads['chat']}})
        assert len(state.values['messages']) == 3
        assert not state.values['clean_retry']
    asyncio.run(run())


def test_persistent_overflow_stops_after_one_retry(monkeypatch):
    async def run():
        worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        calls = []
        class Model:
            def bind_tools(self, available):
                return self
            async def ainvoke(self, messages, **kwargs):
                if 'context_compaction' in kwargs.get('config', {}).get('tags', []):
                    return AIMessage(content='Goal: continue tests.')
                calls.append(messages)
                raise ValueError('maximum context length exceeded')
        monkeypatch.setattr(worker, 'model', lambda: Model())
        with pytest.raises(ValueError, match='maximum context'):
            async for _ in worker.stream('task', conversation_id='chat', context_mode='fresh', handoff='context'):
                pass
        assert len(calls) == 2
        state = await worker.build_graph().aget_state({'configurable': {'thread_id': worker._threads['chat']}})
        assert len(state.values['messages']) == 2
        assert 'continue tests' in state.values['messages'][1].content
    asyncio.run(run())


def test_cache_hits_persist_and_old_databases_gain_the_column(tmp_path):
    import sqlite3
    path = tmp_path/'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE agent_usage (id VARCHAR(80) PRIMARY KEY, agent_id VARCHAR(64) NOT NULL, '
                   'total_tokens INTEGER NOT NULL, context_tokens INTEGER NOT NULL, created_at DATETIME NOT NULL)')
        db.execute("INSERT INTO agent_usage VALUES ('legacy', 'backend', 1000, 900, '2026-10-01')")
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{path}')
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        cached = AIMessage(content='', usage_metadata={'input_tokens': 1000, 'output_tokens': 10, 'total_tokens': 1010,
                                                       'input_token_details': {'cache_read': 800}})
        unknown = AIMessage(content='', usage_metadata={'input_tokens': 500, 'output_tokens': 10, 'total_tokens': 510})
        await team.workers['backend'].record_usage_durable(cached)
        await team.workers['backend'].record_usage_durable(unknown)
        await store.close()
        restored = ChatStore(f'sqlite+aiosqlite:///{path}')
        restarted = Team()
        await restarted.configure_persistence(restored, InMemorySaver())
        usage = restarted.usage_snapshot()
        # Legacy and cache-less calls count toward totals but not the cache ratio.
        assert usage['total_tokens'] == 1000 + 1010 + 510
        assert (usage['cache_input_tokens'], usage['cached_tokens'], usage['cache_percent']) == (1000, 800, 80.0)
        await restored.close()
    asyncio.run(run())


def test_old_tool_bodies_fold_in_batches_under_budget(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '5000')
    class Member:
        spec = SimpleNamespace(id='backend')
        def context_window(self):
            return 131072
    def round_trip(i, size):
        call = AIMessage(content='', id=f'a{i}', tool_calls=[
            {'id': f'c{i}', 'name': 'write_project_file', 'args': {'path': f'f{i}.py', 'content': 'x' * size}},
            {'id': f'r{i}', 'name': 'read_project_file', 'args': {'path': f'g{i}.py'}}])
        return [call, ToolMessage(f'Wrote f{i}.py', tool_call_id=f'c{i}', id=f'w{i}'),
                ToolMessage('y' * size, tool_call_id=f'r{i}', id=f't{i}')]
    async def run():
        memory = ContextMemory(Member(), 'fold-scope')
        small = [SystemMessage(content='sys'), HumanMessage(content='task', id='h'), *round_trip(0, 1500),
                 *[HumanMessage(content=str(i), id=f'h{i}') for i in range(8)]]
        assert not (await memory.prepare(small)).changed
        history = [SystemMessage(content='sys'), HumanMessage(content='task', id='h'),
                   *[m for i in range(4) for m in round_trip(i, 4000)], *round_trip(9, 4000)]
        prepared = await memory.prepare(history)
        assert prepared.changed and prepared.stage == 'fold'
        folded, recent = prepared.messages[2], prepared.messages[-3:]
        assert len(folded.tool_calls[0]['args']['content']) < 400 and folded.tool_calls[0]['args']['path'] == 'f0.py'
        assert len(prepared.messages[4].content) <= 1200 and 'recall_context' in prepared.messages[4].content
        assert recent[0].tool_calls[0]['args']['content'] == 'x' * 4000 and recent[2].content == 'y' * 4000
    asyncio.run(run())
