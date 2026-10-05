import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.team import Team
from app.agents.worker import WorkerAgent
from app.collaboration.coordinator import LeaderCoordinator
from app.collaboration.runtime import Scheduler
from app.collaboration.service import Collaboration
from app.collaboration.tools import active
from app.storage import ChatStore


async def setup(tmp_path):
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
    p = (await store.create_project('P', str(tmp_path)))['id']
    c = (await store.create_conversation(p))['id']
    await store.add_user_message(c, 'Build feature', 'user')
    service = Collaboration(store)
    await service.initialize()
    team = Team()
    await team.configure_persistence(store, InMemorySaver())
    team.collaboration = service
    return store, service, team, p, c


async def until(predicate):
    async def wait():
        while not await predicate():
            await asyncio.sleep(.02)
    await asyncio.wait_for(wait(), 5)


def test_suspend_message_wake_reply_resuspend_and_delivery(tmp_path, monkeypatch):
    async def scenario():
        store, s, team, p, c = await setup(tmp_path)
        ask, deliver = asyncio.Event(), asyncio.Event()
        question_id = []
        calls = []
        async def worker(self, task, **kwargs):
            ctx = active.get()
            await ask.wait()
            question = await s.send(p, 'frontend', 'leader', 'Which format?', 'question', c, summary='请确认格式。')
            question_id.append(question['chat_message_id'])
            async def answered():
                return any(e.get('type')=='message' for e in await s.inbox(p, 'frontend', ctx.context, c, request=ctx.request))
            await until(answered)
            await deliver.wait()
            yield 'content', {'content': 'Done'}
        async def leader(messages, **kwargs):
            calls.append(messages)
            snapshot = await s.snapshot(p)
            requests = snapshot['request']
            if not requests:
                return AIMessage(content='', tool_calls=[{'id':'dispatch','name':'delegate_task','args':{'worker':'frontend','task':'Build','summary':'请实现功能','handoff':'Provide evidence'}}])
            if any(m['id']=='frontend:question' for m in snapshot['message']) and not any(m['id']=='leader:answer' for m in snapshot['message']):
                return AIMessage(content='', tool_calls=[{'id':'answer','name':'collaborate','args':{'action':'send','payload':{'recipient':'frontend','key':'answer','content':'Use JSON','summary':'按 JSON 格式继续。','request_id':requests[0]['id']}}}])
            if requests[0]['state']=='review':
                return AIMessage(content='', tool_calls=[{'id':'accept','name':'collaborate','args':{'action':'transition','payload':{'identifier':requests[0]['id'],'state':'completed','result':'Accepted'}}}])
            return AIMessage(content='All done' if requests[0]['state']=='completed' else 'Waiting for remaining work')
        monkeypatch.setattr(WorkerAgent, 'stream', worker)
        monkeypatch.setattr(team.leader, 'next_message', leader)
        scheduler = Scheduler(s, team)
        await scheduler.start()
        try:
            async def first_turn():
                async for event, data in team.stream([HumanMessage(content='Build feature')], conversation_id=c, workspace_path=tmp_path):
                    if event=='chat_message':
                        await store.add_chat_message(c, data)
            await asyncio.wait_for(first_turn(), 1)
            assert not deliver.is_set()
            assert team.leader.status == 'waiting'
            count = len(calls)
            await asyncio.sleep(.3)
            assert len(calls) == count  # suspended means no model polling
            ask.set()
            async def replied():
                return any(m['id']=='leader:answer' for m in (await s.snapshot(p))['message'])
            await until(replied)
            async def resting():
                return team.leader.status == 'waiting' and scheduler.coordinator.job.done()
            await until(resting)
            detail = await store.get_conversation_detail(c)
            assert next(m for m in detail['messages'] if m['id']==question_id[0])['read_by'] == ['leader']
            assert (await s.snapshot(p))['request'][0]['state']=='running'
            assert len((await s.snapshot(p))['request'])==1
            deliver.set()
            async def accepted():
                return (await s.snapshot(p))['request'][0]['state']=='completed'
            await until(accepted)
        finally:
            await scheduler.close()
            await store.close()
    asyncio.run(scenario())


def test_progress_deadline_dedup_and_context_isolation(tmp_path):
    async def scenario():
        store, s, team, p, c = await setup(tmp_path)
        await s.create_request(p, 'leader', 'frontend', 'Work', 'task', c)
        await s.leader_session(p, c, state='waiting', last_wake=100, last_progress=0)
        coordinator = LeaderCoordinator(s, team)
        before = await s.inbox(p, 'frontend', 'worker', c)
        await coordinator.progress(p, c, 279)
        assert len((await store.get_conversation_detail(c))['messages'])==2
        await coordinator.progress(p, c, 280)
        detail = await store.get_conversation_detail(c)
        progress = [m for m in detail['messages'] if m['kind']=='progress']
        assert len(progress)==2
        assert all(m['read_by'] for m in progress)
        assert await store.model_history(c)==[{'role':'user', 'content':'Build feature'}]
        assert await s.inbox(p, 'frontend', 'worker', c)==before
        assert not await s.inbox(p, 'leader', f'leader:{c}', c)
        assert not (await s.snapshot(p))['message']
        await coordinator.progress(p, c, 280)
        assert len((await store.get_conversation_detail(c))['messages'])==len(detail['messages'])
        await s.leader_session(p, c, last_wake=350)
        await coordinator.progress(p, c, 529)
        assert len((await store.get_conversation_detail(c))['messages'])==len(detail['messages'])
        await coordinator.progress(p, c, 530)
        assert len((await store.get_conversation_detail(c))['messages'])==len(detail['messages'])+2
        await store.close()
    asyncio.run(scenario())


def test_restart_replays_unconsumed_event_and_no_duplicate_wake(tmp_path, monkeypatch):
    async def scenario():
        store, s, team, p, c = await setup(tmp_path)
        await s.send(p, 'frontend', 'leader', 'Need decision', 'q', c)
        await s.leader_session(p, c, state='running', last_wake=0)
        coordinator = LeaderCoordinator(s, team)
        calls = []
        async def reply(messages, **kwargs):
            calls.append(messages)
            return AIMessage(content='Decision saved')
        monkeypatch.setattr(team.leader, 'next_message', reply)
        await coordinator.start()
        await coordinator.tick()
        await coordinator.job
        assert len(calls)==1
        await coordinator.tick()
        assert len(calls)==1
        assert (await store.model_history(c))[-1]['content']=='Decision saved'
        assert not await s.inbox(p, 'leader', f'leader:{c}', c)
        await coordinator.close()
        await store.close()
    asyncio.run(scenario())


def test_failed_model_does_not_mark_message_read_and_retries_are_bounded(tmp_path, monkeypatch):
    async def scenario():
        store, s, team, p, c = await setup(tmp_path)
        message = await s.send(p, 'frontend', 'leader', 'Need decision', 'q', c)
        async def fail(messages, **kwargs):
            raise RuntimeError('provider unavailable')
        monkeypatch.setattr(team.leader, 'next_message', fail)
        coordinator = LeaderCoordinator(s, team)
        await coordinator.start()
        for n in range(3):
            await coordinator.tick(now=time.time()+1000)
            await coordinator.job
        async with s.reading() as db:
            session = await s._get(db, 'leader_session', p, c)
        assert session['state']=='failed' and session['failures']==3
        detail = await store.get_conversation_detail(c)
        assert not next(m for m in detail['messages'] if m['id']==message['chat_message_id'])['read_by']
        assert await s.inbox(p, 'leader', f'leader:{c}', c)
        await coordinator.close()
        await store.close()
    asyncio.run(scenario())


def test_progress_skips_paused_finished_and_active_conversations(tmp_path):
    async def scenario():
        store, s, team, p, c = await setup(tmp_path)
        request = await s.create_request(p, 'leader', 'frontend', 'Work', 'task', c)
        await s.leader_session(p, c, state='waiting', last_wake=100, last_progress=0)
        coordinator = LeaderCoordinator(s, team)
        await s.set_paused(p, True)
        await coordinator.tick(now=280)
        assert not [m for m in (await store.get_conversation_detail(c))['messages'] if m['kind']=='progress']
        await s.set_paused(p, False)
        await s.leader_session(p, c, state='running')
        await coordinator.tick(now=280)
        assert not [m for m in (await store.get_conversation_detail(c))['messages'] if m['kind']=='progress']
        await s.leader_session(p, c, state='waiting')
        await coordinator.tick(now=280)
        assert len([m for m in (await store.get_conversation_detail(c))['messages'] if m['kind']=='progress'])==2
        # A restarted coordinator must honor the saved last_progress timestamp.
        restarted = LeaderCoordinator(s, team)
        await restarted.start()
        await restarted.tick(now=300)
        assert len([m for m in (await store.get_conversation_detail(c))['messages'] if m['kind']=='progress'])==2
        await s.transition(p, request['id'], 'leader', 'cancelled')
        await s.leader_session(p, c, state='idle')
        await coordinator.progress(p, c, 500)
        assert len([m for m in (await store.get_conversation_detail(c))['messages'] if m['kind']=='progress'])==2
        await restarted.close()
        await coordinator.close()
        await store.close()
    asyncio.run(scenario())
