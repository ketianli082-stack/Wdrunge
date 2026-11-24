import asyncio
import time

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.base import AgentSpec
from app.agents.team import Team
from app.agents.worker import WorkerAgent
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.collaboration.runtime import Scheduler
from app.storage import ChatStore, StorageConflictError


async def setup(tmp_path):
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/goals.db')
    p = (await store.create_project('Goals', str(tmp_path)))['id']
    c = (await store.create_conversation(p))['id']
    await store.add_user_message(c, 'Finish the goal', 'user')
    s = Collaboration(store)
    g = await s.create_goal(p, 'leader', c, 'Deliver the feature', ['Tests pass'], 'feature', 60)
    return store, s, p, c, g


async def task(s, p, c, g, key='task', **kwargs):
    return await s.create_request(p, 'leader', 'backend', 'Implement the feature', key,
                                  conversation=c, goal_id=g['id'], acceptance_criteria=['API works'], **kwargs)


def test_goal_restart_idempotency_and_completion_guards(tmp_path):
    async def run():
        store, s, p, c, g = await setup(tmp_path)
        assert (await s.create_goal(p, 'leader', c, g['objective'], g['criteria'], 'feature', 60))['id'] == g['id']
        with pytest.raises(StorageConflictError):
            await s.create_goal(p, 'leader', c, 'Other objective', g['criteria'], 'feature', 60)
        with pytest.raises(StorageConflictError, match='Every goal task'):
            await s.complete_goal(p, 'leader', c, g['id'], {'Tests pass': 'verified'})
        r = await task(s, p, c, g)
        await store.close()
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/goals.db'); s = Collaboration(store)
        assert (await s.get_goal(p, g['id'], c))['tasks'][0]['id'] == r['id']
        await s.claim()
        await s.transition(p, r['id'], 'backend', 'review', result='API check passed')
        with pytest.raises(ValueError, match='Evidence'):
            await s.transition(p, r['id'], 'leader', 'completed', result='done')
        with pytest.raises(ValueError):
            await s.transition(p, r['id'], 'backend', 'completed', evidence={'API works':'test'})
        await s.transition(p, r['id'], 'leader', 'completed', evidence={'API works':'GET /health returned 200'})
        with pytest.raises(ValueError, match='Evidence'):
            await s.complete_goal(p, 'leader', c, g['id'], {'Tests pass':' '})
        result = await s.complete_goal(p, 'leader', c, g['id'], {'Tests pass':'Integration suite passed'})
        assert result['state'] == 'completed'
        assert (await s.transition(p, r['id'], 'leader', 'completed', evidence={'API works':'GET /health returned 200'}))['state']=='completed'
        assert await s.complete_goal(p, 'leader', c, g['id'], result['evidence']) == result
        with pytest.raises(ValueError, match='active goal'):
            await task(s, p, c, g, 'late')
        assert (await task(s, p, c, g))['state'] == 'completed'
        await s.scheduling_tick(time.time()+100000)
        assert not [x for x in await s.inbox(p, 'leader', 'test', c) if x['type']=='goal_reminder']
        await store.close()
    asyncio.run(run())


def test_pause_cancel_and_stale_instances(tmp_path):
    async def run():
        store, s, p, c, g = await setup(tmp_path)
        r = await task(s,p,c,g); old = (await s.claim('first'))[2]
        await s.update_goal(p, 'leader', c, g['id'], 'paused', 'User paused')
        assert await s.claim() is None
        with pytest.raises(StorageConflictError, match='lease'):
            await s.transition(p,r['id'],'backend','review',result='stale',expected_instance=old['instance_id'])
        token = active.set(Context(s,p,'backend',c,'ctx',r['id'],old['instance_id']))
        try:
            assert 'error' in await execute_collaboration('create_task', dict(goal_id=g['id'], recipient='testing', goal='test', criteria=['passed'],key='stale'))
        finally:
            active.reset(token)
        await s.update_goal(p,'leader',c,g['id'],'active','Continue')
        new = (await s.claim('second'))[2]
        assert new['instance_id'] != old['instance_id'] and new['workspace_id'] == old['workspace_id']
        with pytest.raises(StorageConflictError, match='lease'):
            await s.transition(p,r['id'],'backend','failed',result='old error',expected_instance=old['instance_id'])
        await s.update_goal(p,'leader',c,g['id'],'cancelled','Stop')
        assert (await s.get_request(p,r['id']))['state']=='cancelled'
        assert await s.claim() is None
        await store.close()
    asyncio.run(run())


def test_dependency_conversation_isolation_and_child_inheritance(tmp_path):
    async def run():
        store,s,p,c,g = await setup(tmp_path)
        other = await s.create_goal(p,'leader',c,'Other goal',['Other test'],'other')
        r = await task(s,p,c,g)
        with pytest.raises(ValueError, match='same goal'):
            await task(s,p,c,other,'bad',dependencies=[r['id']])
        await s.claim()
        child = await s.create_request(p,'backend','testing','Test feature','child',conversation=c,parent=r['id'])
        assert child['goal_id']==g['id'] and child['acceptance_criteria']==['Test feature']
        c2 = (await store.create_conversation(p))['id']
        with pytest.raises(ValueError, match='another conversation'):
            await s.get_goal(p,g['id'],c2)
        assert await s.goal_reminder(p,c2)==''
        assert other['id'] not in await s.goal_reminder(p,c,r['id'])
        await store.close()
    asyncio.run(run())


def test_reminder_persistence_dedup_backoff_and_stall_limit(tmp_path):
    async def run():
        store,s,p,c,g = await setup(tmp_path)
        await asyncio.gather(s.goal_tick(g['next_reminder_at']), s.goal_tick(g['next_reminder_at']))
        events=await s.inbox(p,'leader','ctx',c)
        assert len(events)==1 and events[0]['type']=='goal_reminder'
        await store.close()
        store=ChatStore(f'sqlite+aiosqlite:///{tmp_path}/goals.db');s=Collaboration(store)
        assert len(await s.inbox(p,'leader','ctx',c))==1
        await s.goal_tick(g['next_reminder_at']+1000)
        assert (await s.get_goal(p,g['id'],c))['reminder_count']==1  # unread is not a new reminder
        for count in (2,3,4):
            inbox=await s.inbox(p,'leader',f'leader:{c}',c)
            await s.acknowledge(p,'leader',f'leader:{c}',[e['event_id'] for e in inbox])
            current=await s.get_goal(p,g['id'],c)
            await s.goal_tick(current['next_reminder_at'])
            current=await s.get_goal(p,g['id'],c)
            assert current['stagnant_reminders']==count
        assert current['state']=='blocked'
        assert not await s.inbox(p,'leader','ctx',c)
        assert (await s.snapshot(p))['goal'][0]['reason']
        await s.update_goal(p,'leader',c,g['id'],'active','Resolved')
        current=await s.get_goal(p,g['id'],c)
        assert current['stagnant_reminders']==0
        assert not await s.inbox(p,'leader','ctx',c)
        await s.set_paused(p,True);await s.goal_tick(current['next_reminder_at'])
        assert (await s.get_goal(p,g['id'],c))['reminder_count']==3
        await store.close()
    asyncio.run(run())


def test_normal_execution_does_not_trigger_paid_reminders(tmp_path):
    async def run():
        store,s,p,c,g=await setup(tmp_path);r=await task(s,p,c,g)
        for _ in range(4):
            current=await s.get_goal(p,g['id'],c);await s.goal_tick(current['next_reminder_at'])
        assert not await s.inbox(p,'leader','ctx',c)
        await s.claim();await s.transition(p,r['id'],'backend','review',result='check passed')
        current=await s.get_goal(p,g['id'],c);await s.goal_tick(current['next_reminder_at'])
        assert any(x['type']=='goal_reminder' for x in await s.inbox(p,'leader','ctx',c))
        await store.close()
    asyncio.run(run())


def test_worker_reminder_reissued_after_overflow_without_false_receipts(tmp_path, monkeypatch):
    async def run():
        store,s,p,c,g=await setup(tmp_path);await task(s,p,c,g);r=(await s.claim())[2]
        calls=[];worker=WorkerAgent(AgentSpec(id='backend',name='Backend',prompt_file='backend.md'))
        class Model:
            def bind_tools(self,tools):
                names = {t['function']['name'] for t in tools}
                assert 'create_task' not in names and {'submit_task', 'enable_tools'} <= names
                return self
            async def ainvoke(self,messages,**kwargs):
                if 'context_compaction' in kwargs.get('config',{}).get('tags',[]):
                    assert not any(str(m.content).startswith('Goal reminder') for m in messages)
                    return AIMessage(content='Continue implementation and verify.')
                calls.append(messages)
                assert sum(str(m.content).startswith('Goal reminder') for m in messages)==1
                if len(calls)==1: raise ValueError('maximum context length exceeded')
                return AIMessage(content='done')
        monkeypatch.setattr(worker,'model',lambda:Model())
        token=active.set(Context(s,p,'backend',c,'worker',r['id'],r['instance_id']))
        try:
            async for _ in worker.stream('Implement',conversation_id='test',context_mode='fresh',handoff='Keep tests'): pass
            assert len(calls)==2
            assert await s.inbox(p,'backend','worker',c,request=r['id'])
            state=await worker.build_graph().aget_state({'configurable':{'thread_id':worker._threads['test']}})
            # Stored once in the context (cacheable), not appended to every call.
            assert sum(str(m.content).startswith('Goal reminder') for m in state.values['messages']) == 1
        finally:
            active.reset(token);await store.close()
    asyncio.run(run())


def test_reminder_wakes_coordinator_and_finishes_goal(tmp_path,monkeypatch):
    async def run():
        store,s,p,c,g=await setup(tmp_path)
        team=Team();await team.configure_persistence(store,InMemorySaver());team.collaboration=s
        calls=[]
        async def leader(messages,**kwargs):
            calls.append(messages);state=await s.get_goal(p,g['id'],c)
            assert any(str(m.content).startswith('Goal reminder') for m in messages) or state['state']=='completed'
            if not state['tasks']:
                name='create_task';args=dict(goal_id=g['id'],recipient='backend',goal='Implement',criteria=['API works'],key='implement')
            elif state['tasks'][0]['state']=='review':
                name='complete_task';args=dict(identifier=state['tasks'][0]['id'],result='Verified',evidence={'API works':'Tests passed'})
            elif all(t['state']=='completed' for t in state['tasks']) and state['state']!='completed':
                name='complete_goal';args=dict(identifier=g['id'],evidence={'Tests pass':'Integration check passed'})
            else: return AIMessage(content='Finished' if state['state']=='completed' else 'Work queued')
            return AIMessage(content='',tool_calls=[dict(name=name,args=args,id=f'call-{len(calls)}')])
        async def worker(self,*args,**kwargs): yield 'content',{'content':'Implementation and tests passed'}
        monkeypatch.setattr(team.leader,'next_message',leader);monkeypatch.setattr(WorkerAgent,'stream',worker)
        await s.goal_tick(g['next_reminder_at']);scheduler=Scheduler(s,team);await scheduler.start()
        try:
            async def wait():
                while (await s.get_goal(p,g['id'],c))['state']!='completed': await asyncio.sleep(.02)
            await asyncio.wait_for(wait(),10)
            assert (await s.get_goal(p,g['id'],c))['tasks'][0]['state']=='completed'
        finally:
            await scheduler.close();await store.close()
    asyncio.run(run())


def test_goal_task_pages_and_paused_deadlines(tmp_path):
    async def run():
        store,s,p,c,g=await setup(tmp_path)
        a=await task(s,p,c,g,'a',deadline=time.time()-1)
        b=await task(s,p,c,g,'b')
        first=await s.get_goal(p,g['id'],c,limit=1)
        second=await s.get_goal(p,g['id'],c,offset=first['next_offset'],limit=1)
        assert first['total_tasks']==2 and first['task_counts']=={'queued':2}
        assert first['tasks'][0]['id']==a['id'] and second['tasks'][0]['id']==b['id']
        assert 'spec' not in first['tasks'][0] and second['next_offset'] is None
        await s.update_goal(p,'leader',c,g['id'],'paused','Wait')
        await s.scheduling_tick(time.time()+10000)
        assert not await s.inbox(p,'leader',f'leader:{c}',c)
        await store.close()
    asyncio.run(run())


def test_leader_refreshes_goal_after_context_overflow(tmp_path,monkeypatch):
    async def run():
        from langchain_core.messages import HumanMessage
        store,s,p,c,g=await setup(tmp_path)
        team=Team();await team.configure_persistence(store,InMemorySaver());team.collaboration=s
        calls=[]
        class Summarizer:
            async def ainvoke(self,messages,**kwargs):
                assert not any(str(m.content).startswith('Goal reminder') for m in messages)
                return AIMessage(content='The feature objective remains pending.')
        async def leader(messages,**kwargs):
            fresh=[m for m in messages if str(m.content).startswith('Goal reminder')]
            assert len(fresh)==1
            calls.append(messages)
            if len(calls)==1:
                await s.update_goal(p,'leader',c,g['id'],'paused','Human paused during model call')
                raise ValueError('maximum context length exceeded')
            assert '"state": "paused"' in fresh[0].content
            return AIMessage(content='Goal paused')
        monkeypatch.setattr(team.leader,'model',lambda:Summarizer())
        monkeypatch.setattr(team.leader,'next_message',leader)
        async for _ in team.stream([HumanMessage(content='Continue goal')],conversation_id=c,workspace_path=tmp_path): pass
        assert len(calls)==2
        await store.close()
    asyncio.run(run())


def test_the_goal_reminder_changes_with_the_goal_not_with_task_progress(tmp_path):
    """Blog6: the reminder carried task counts, so each create_task changed it and the
    Leader's context got a new copy after every one of 14 calls in a single turn."""
    from langchain_core.messages import HumanMessage
    from app.collaboration.reminders import goal_messages, with_goal_state

    async def run():
        store, s, p, c, g = await setup(tmp_path)
        token = active.set(Context(s, p, 'leader', c, f'leader:{c}'))
        try:
            messages = with_goal_state([HumanMessage(content='Finish the goal')], await goal_messages())
            assert [m.additional_kwargs.get('goal_state') for m in messages] == [None, True]
            first = await task(s, p, c, g, 'one')
            await task(s, p, c, g, 'two')
            job = (await s.claim('owner'))[2]
            await s.transition(p, first['id'], 'backend', 'review', result='done', expected_instance=job['instance_id'])
            # Tasks were created, started and delivered: the goal itself did not change.
            assert with_goal_state(messages, await goal_messages()) == messages
            assert 'task_counts' not in messages[-1].content and (await s.get_goal(p, g['id'], c))['task_counts'] == {'queued': 1, 'review': 1}
            await s.update_goal(p, 'leader', c, g['id'], 'paused', 'User asked to wait')
            changed = with_goal_state(messages, await goal_messages())
            assert len(changed) == 3 and '"state": "paused"' in changed[-1].content and 'User asked to wait' in changed[-1].content
        finally:
            active.reset(token)
        # An assignee's copy names its own task and is as stable.
        other = await store.create_conversation(p)
        goal = await s.create_goal(p, 'leader', other['id'], 'Another', ['x'], 'another', 60)
        mine = await task(s, p, other['id'], goal, 'mine')
        running = (await s.claim('owner'))[2]
        token = active.set(Context(s, p, 'backend', other['id'], 'worker', running['id'], running['instance_id']))
        try:
            before = await goal_messages()
            assert f'"id": "{mine["id"]}"' in before[0].content
            await task(s, p, other['id'], goal, 'sibling')
            assert (await goal_messages())[0].content == before[0].content
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())
