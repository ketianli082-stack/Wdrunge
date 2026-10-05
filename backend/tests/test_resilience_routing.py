"""Stalls are diagnosed and recovered through a fallback chain; a burst of provider
failures pauses new work; models are routed by role, purpose and goal scale."""
import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import resilience
from app.agents.base import Agent, AgentSpec
from app.agents.stall import StreamTrace


class StreamChunkTimeoutError(Exception):
    """Same name as the provider stream timeout; recognized by class name."""


class Calls:
    def __init__(self, outcomes):
        self.outcomes, self.count = list(outcomes), 0

    async def ainvoke(self, value, **kwargs):
        self.count += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fallback_chain_after_retries_and_non_transient_errors_fail_fast():
    async def run():
        primary = Calls([StreamChunkTimeoutError('stall')] * 2)
        streaming_off = Calls([AIMessage(content='ok')])
        response = await resilience.ainvoke_with_retry(primary, [], attempts=2, base_delay=0,
                                                       alternates=(lambda: streaming_off, lambda: None))
        assert response.content == 'ok' and primary.count == 2 and streaming_off.count == 1
        assert response.response_metadata['wdrunge_retries'] == 2
        skipped = Calls([StreamChunkTimeoutError('stall')])
        fallback = Calls([AIMessage(content='fallback')])
        assert (await resilience.ainvoke_with_retry(skipped, [], attempts=1, alternates=(lambda: None, lambda: fallback))).content == 'fallback'
        broken = Calls([ValueError('bad request')])
        never = Calls([AIMessage(content='unused')])
        with pytest.raises(ValueError):
            await resilience.ainvoke_with_retry(broken, [], attempts=3, alternates=(lambda: never,))
        assert never.count == 0
        exhausted = Calls([StreamChunkTimeoutError('a')])
        alternate = Calls([StreamChunkTimeoutError('b')])
        with pytest.raises(StreamChunkTimeoutError, match='b'):
            await resilience.ainvoke_with_retry(exhausted, [], attempts=1, alternates=(lambda: alternate,))
    asyncio.run(run())


def test_provider_breaker_pauses_new_work_after_a_failure_burst(monkeypatch):
    monkeypatch.setenv('WDRUNGE_PROVIDER_BREAKER', '3')
    for second in (0, 10, 20):
        assert resilience.provider_cooldown(now=second) == 0
        resilience.note_failure(now=second)
    assert resilience.provider_cooldown(now=25) == pytest.approx(55)
    assert resilience.provider_cooldown(now=81) == 0          # cooled down
    assert resilience.provider_cooldown(now=200) == 0         # old failures expire


def test_stream_trace_names_what_was_being_generated():
    trace = StreamTrace()
    assert trace.start() is None
    trace.chunk(AIMessageChunk(content='', additional_kwargs={'reasoning_content': 'thinking'}))
    trace.chunk(AIMessageChunk(content='', tool_call_chunks=[{'name': 'collaboration_publish', 'args': '{"changes"', 'id': 'c', 'index': 0}]))
    trace.chunk(AIMessageChunk(content='', tool_call_chunks=[{'name': None, 'args': ': [', 'id': None, 'index': 0}]))
    pending = trace.pending()
    assert pending['chunks'] == 3 and pending['last_chunk'] == 'tool_args' and pending['tool'] == 'collaboration_publish'
    assert trace.start()['tool'] == 'collaboration_publish'  # a retry starts: the stalled call is reported
    trace.end()
    assert trace.pending() is None


def test_a_mid_stream_stall_is_recorded_in_the_task_journal(tmp_path, monkeypatch):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGenerationChunk
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from app.collaboration.service import Collaboration
    from app.storage import ChatStore
    monkeypatch.setenv('WDRUNGE_MODEL_RETRIES', '1')

    class Stalling(BaseChatModel):
        @property
        def _llm_type(self):
            return 'stalling'
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, *args, **kwargs):
            raise NotImplementedError
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            for piece in ('{"path": "docs/report.md", ', '"content": "# Report'):
                chunk = ChatGenerationChunk(message=AIMessageChunk(content='', tool_call_chunks=[
                    {'name': 'write_project_file' if piece.startswith('{"path') else None, 'args': piece, 'id': 'w', 'index': 0}]))
                if run_manager:
                    await run_manager.on_llm_new_token('', chunk=chunk)
                yield chunk
            raise StreamChunkTimeoutError('No streaming chunk received for 120.0s')
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Stalling())

    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/stall.db')
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        project = (await store.create_project('Stall', str(workspace)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        request = await service.create_request(project, 'leader', 'testing', 'Write the report', 'report', conversation=conversation)
        await Scheduler(service, team).execute(*(await service.claim('owner')))
        journal = await service.journal(project, request['id'])
        stall = journal['stalls'][-1]
        assert stall['outcome'] == 'failed' and stall['last_chunk'] == 'tool_args' and stall['tool'] == 'write_project_file'
        assert stall['chunks'] == 2 and stall['error'] == 'StreamChunkTimeoutError'
        assert '最后在生成 tool_args（write_project_file 参数）' in await service.journal_markdown(project, request['id'])
        assert (await service.get_request(project, request['id']))['state'] == 'queued'  # resumes instead of failing
        await store.close()
    asyncio.run(run())


def test_models_are_routed_by_purpose_role_and_goal_scale(monkeypatch):
    monkeypatch.setenv('QWEN_MODEL', 'qwen-max')
    backend = Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
    leader = Agent(AgentSpec(id='leader', name='Leader', prompt_file='leader.md'))
    assert backend.model_name() == 'qwen-max'
    monkeypatch.setenv('WDRUNGE_MODEL_SMALL_TASKS', 'qwen-plus')
    monkeypatch.setenv('WDRUNGE_MODEL_SUMMARY', 'qwen-flash')
    assert backend.model_name(scale='small') == 'qwen-plus' and backend.model_name(scale='medium') == 'qwen-max'
    assert leader.model_name(scale='small') == 'qwen-max'  # coordination keeps the strongest model
    assert backend.model_name('summary', 'small') == 'qwen-flash'
    monkeypatch.setenv('WDRUNGE_MODEL_BACKEND', 'qwen-coder')
    assert backend.model_name(scale='small') == 'qwen-coder'
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test')
    assert backend.model(variant='fallback') is None
    monkeypatch.setenv('QWEN_FALLBACK_MODEL', 'qwen-plus-latest')
    assert backend.model(variant='fallback').model_name == 'qwen-plus-latest'
    assert backend.model(variant='non_streaming').disable_streaming is True
