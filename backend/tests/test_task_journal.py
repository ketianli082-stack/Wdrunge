"""Task work journals and resume-not-restart: interrupted work continues instead of
being redone, and Leader sees why an attempt ended."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.resilience import ainvoke_with_retry, is_transient
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.storage import ChatStore


class StreamChunkTimeoutError(Exception):
    """Same name as the provider stream timeout; the classifier matches by class name."""


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/journal.db')
    project = (await store.create_project('Journal', str(workspace)))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, workspace


def turn(name, args, result, call_id):
    return [AIMessage(content='', id=f'ai-{call_id}', tool_calls=[{'id': call_id, 'name': name, 'args': args}]),
            ToolMessage(content=result if isinstance(result, str) else json.dumps(result), tool_call_id=call_id, id=f'r-{call_id}',
                        status='error' if isinstance(result, dict) and result.get('error') else 'success')]


def test_journal_collects_facts_notes_attempts_and_renders_handoff(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        r = await s.create_request(p, 'leader', 'testing', 'Verify the page', 'verify')
        messages = [HumanMessage(content='task', id='task'),
                    *turn('write_project_file', {'path': 'docs/testing/report.md', 'content': 'x'},
                          'Wrote docs/testing/report.md (1 characters; sha256=abcdef).', 'c1'),
                    *turn('browser_verify', {}, {'url': 'http://localhost:5173/', 'passed': True, 'screenshot': 'docs/testing/a.png'}, 'c2'),
                    *turn('execute_command', {'command': 'curl', 'arguments': ['http://127.0.0.1:8765/']},
                          {'id': 'p1', 'argv': ['curl', 'http://127.0.0.1:8765/'], 'exit_code': 7, 'error': 'Command exited 7',
                           'error_details': {'code': 'connection_failed'}}, 'c3')]
        await s.journal_attempt_start(p, r['id'], 1, 'inst-1', 'fresh')
        journal = await s.journal_facts(p, r['id'], messages)
        assert journal['last_failure']['code'] == 'connection_failed'
        # Recording the same messages again changes nothing.
        assert (await s.journal_facts(p, r['id'], messages))['facts'] == journal['facts']
        await s.journal_note(p, r['id'], 'testing', 'done', 'API layer verified; evidence in docs/testing/report.md')
        await s.journal_note(p, r['id'], 'testing', 'next', 'Browser failure path with expect_errors')
        with pytest.raises(ValueError):
            await s.journal_note(p, r['id'], 'testing', 'gossip', 'x')
        await s.journal_attempt_end(p, r['id'], 'inst-1', 'interrupted', 'StreamChunkTimeoutError: no chunk for 120s')
        brief = s.journal_brief(await s.journal(p, r['id']))
        assert 'Earlier attempts: #1 中断，自动续跑 (StreamChunkTimeoutError' in brief
        assert 'docs/testing/report.md wrote' in brief and 'http://localhost:5173/ passed=True' in brief
        assert '[done] API layer verified' in brief and 'Last tool failure: execute_command [connection_failed]' in brief
        markdown = await s.journal_markdown(p, r['id'])
        assert markdown.startswith('# 工作记录') and '[next] Browser failure path' in markdown
        snapshot = await s.snapshot(p)
        assert snapshot['journal'][0]['request'] == r['id'] and 'seen' not in json.dumps(snapshot['journal'])
        assert s.journal_brief(await s.journal(p, 'unknown')) == ''
        await store.close()
    asyncio.run(run())


def test_notes_are_task_scoped_and_leader_sees_the_journal(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        r = await s.create_request(p, 'leader', 'backend', 'Build API', 'api')
        job = (await s.claim('owner'))[2]
        token = active.set(Context(s, p, 'backend', '', 'ctx', request=r['id'], instance_id=job['instance_id']))
        try:
            assert (await execute_collaboration('journal', {'kind': 'blocked', 'text': 'submit_task rejects docs/api.md'}))['recorded'] == 'blocked'
        finally:
            active.reset(token)
        token = active.set(Context(s, p, 'leader', '', 'leader-ctx'))
        try:
            assert 'error' in await execute_collaboration('journal', {'kind': 'done', 'text': 'x'})
            inspected = await execute_collaboration('inspect_request', {'identifier': r['id']})
            assert inspected['journal']['notes'] == [{'kind': 'blocked', 'text': 'submit_task rejects docs/api.md', 'attempt': None}]
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_transient_errors_are_recognized_through_wrappers():
    class RateLimitError(Exception):
        pass
    class BadRequest(Exception):
        status_code = 400
    class Provider(Exception):
        status_code = 503
    wrapped = RuntimeError('graph failed')
    wrapped.__cause__ = StreamChunkTimeoutError('No streaming chunk received for 120.0s')
    assert is_transient(wrapped) and is_transient(RateLimitError()) and is_transient(Provider())
    assert not is_transient(BadRequest()) and not is_transient(ValueError('bad')) and not is_transient(asyncio.CancelledError())


def test_model_calls_retry_transient_failures_only():
    class Flaky:
        def __init__(self, failures):
            self.calls, self.failures = 0, failures
        async def ainvoke(self, value, **kwargs):
            self.calls += 1
            if self.calls <= len(self.failures):
                raise self.failures[self.calls - 1]
            return 'ok'
    async def run():
        flaky = Flaky([StreamChunkTimeoutError('stall'), StreamChunkTimeoutError('stall')])
        assert await ainvoke_with_retry(flaky, [], attempts=3, base_delay=0) == 'ok' and flaky.calls == 3
        broken = Flaky([ValueError('invalid request')])
        with pytest.raises(ValueError):
            await ainvoke_with_retry(broken, [], attempts=3, base_delay=0)
        assert broken.calls == 1
        exhausted = Flaky([StreamChunkTimeoutError('stall')] * 3)
        with pytest.raises(StreamChunkTimeoutError):
            await ainvoke_with_retry(exhausted, [], attempts=2, base_delay=0)
    asyncio.run(run())


def test_interrupted_requests_resume_and_failed_provider_runs_resume_on_retry(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        r = await s.create_request(p, 'leader', 'backend', 'Build API', 'api')
        job = (await s.claim('owner'))[2]
        assert job['resume'] is False
        assert await s.requeue_interrupted(p, r['id'], job['instance_id'], 'StreamChunkTimeoutError')
        again = (await s.claim('owner'))[2]
        assert again['resume'] is True and again['workspace_id'] == job['workspace_id']
        assert 'resume' not in await s.get_request(p, r['id'])
        # The automatic budget is bounded.
        assert await s.requeue_interrupted(p, r['id'], again['instance_id'], 'stall')
        third = (await s.claim('owner'))[2]
        assert not await s.requeue_interrupted(p, r['id'], third['instance_id'], 'stall')
        await s.transition(p, r['id'], 'backend', 'failed', result='provider failure', expected_instance=third['instance_id'])
        await s.mark_resumable(p, r['id'])
        await s.transition(p, r['id'], 'leader', 'queued', result='retry')
        assert (await s.claim('owner'))[2]['resume'] is True
        await store.close()
    asyncio.run(run())


def test_resume_answers_tool_calls_that_never_finished():
    from app.agents.base import AgentSpec
    from app.agents.worker import WorkerAgent
    async def run():
        worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md'))
        history = [HumanMessage(content='task', id='t'),
                   AIMessage(content='', id='a', tool_calls=[{'id': 'done', 'name': 'read_project_file', 'args': {}},
                                                            {'id': 'lost', 'name': 'execute_command', 'args': {}}]),
                   ToolMessage(content='ok', tool_call_id='done', id='r')]
        class Saver:
            async def aget_tuple(self, config):
                return SimpleNamespace(checkpoint={'channel_values': {'messages': history}})
        worker._checkpointer = Saver()
        worker._threads['c'] = 'thread'
        repairs = await worker.dangling_results('c')
        assert [(m.tool_call_id, m.status) for m in repairs] == [('lost', 'error')]
        assert json.loads(repairs[0].content)['error_details']['code'] == 'interrupted'
        assert await worker.dangling_results('unknown') == []
    asyncio.run(run())


def test_a_provider_stall_resumes_the_same_context_instead_of_redoing_work(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    monkeypatch.setenv('WDRUNGE_MODEL_RETRIES', '1')
    calls = []

    class Model:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            calls.append(messages)
            ctx = active.get()
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[{'id': 'write-1', 'name': 'write_project_file',
                                                          'args': {'path': 'backend/app.py', 'content': 'print(1)\n'}}])
            if len(calls) == 2:
                raise StreamChunkTimeoutError('No streaming chunk received for 120.0s')
            return AIMessage(content='', tool_calls=[{'id': 'deliver', 'name': 'collaborate', 'args': {'action': 'transition',
                'payload': {'identifier': ctx.request, 'state': 'review', 'result': 'API implemented in backend/app.py'}}}])
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def scenario():
        store, s, p, workspace = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team)
        r = await s.create_request(p, 'leader', 'backend', 'Implement the API in backend/app.py', 'api')
        await scheduler.execute(*(await s.claim(scheduler.owner)))
        interrupted = await s.get_request(p, r['id'])
        assert interrupted['state'] == 'queued' and interrupted['resume'] and interrupted['auto_resumes'] == 1
        await scheduler.execute(*(await s.claim(scheduler.owner)))
        final = await s.get_request(p, r['id'])
        assert final['state'] == 'review'
        resumed = calls[2]
        # The resumed call continues the saved context: the earlier write is still there,
        # followed by a short resume note rather than a fresh copy of the task.
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'write-1' for m in resumed)
        note = [m for m in resumed if isinstance(m, HumanMessage) and 'Resume your assigned task' in m.content]
        assert note and 'StreamChunkTimeoutError' in note[-1].content and 'backend/app.py wrote' in note[-1].content
        assert sum('Implement the API in backend/app.py' in str(m.content) for m in resumed) == 1
        journal = await s.journal(p, r['id'])
        assert [(a['mode'], a['outcome']) for a in journal['attempts']] == [('fresh', 'interrupted'), ('continue', 'delivered')]
        await store.close()
    asyncio.run(scenario())
