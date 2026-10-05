import asyncio
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from app.agents.team import Team
from app.agents.worker import WorkerAgent
from app.collaboration.runtime import Scheduler
from app.collaboration.service import Collaboration
from app.storage import ChatStore, StorageConflictError


async def setup(tmp_path):
    store=ChatStore(f'sqlite+aiosqlite:///{tmp_path}/runs.db')
    project=(await store.create_project('Runs',str(tmp_path)))['id']
    conversations=[(await store.create_conversation(project))['id'] for _ in range(2)]
    service=Collaboration(store);tasks=[]
    for index,conv in enumerate(conversations):
        await store.add_user_message(conv,'Build feature',f'user-{index}')
        goal=await service.create_goal(project,'leader',conv,'Feature',['Test passes'],'goal')
        tasks.append(await service.create_request(project,'leader','backend','Work',f'task-{index}',conversation=conv,goal_id=goal['id'],acceptance_criteria=['Work verified']))
    team=Team();await team.configure_persistence(store,InMemorySaver());team.collaboration=service
    return store,service,team,project,conversations,tasks


async def until(predicate):
    async def wait():
        while not await predicate(): await asyncio.sleep(.01)
    await asyncio.wait_for(wait(),3)


def test_durable_stop_is_scoped_and_old_generation_cannot_resume(tmp_path):
    async def run():
        store,s,team,p,convs,tasks=await setup(tmp_path)
        old=await s.begin_conversation_run(p,convs[0]);await s.claim('worker')
        generation=await s.stop_conversation_run(p,convs[0])
        assert (await s.conversation_execution(p,convs[0]))['stopping']
        with pytest.raises(StorageConflictError):await s.begin_conversation_run(p,convs[0])
        await s.finish_conversation_stop(p,convs[0],generation)
        assert (await s.get_request(p,tasks[0]['id']))['state']=='cancelled'
        assert (await s.get_request(p,tasks[1]['id']))['state']=='queued'
        assert not (await s.conversation_execution(p,convs[0]))['running']
        with pytest.raises(StorageConflictError):await s.create_request(p,'leader','frontend','Late','late',conversation=convs[0])
        await s.begin_conversation_run(p,convs[0])
        assert await s.conversation_stopped(p,convs[0],old)
        assert (await s.claim())[2]['id']==tasks[1]['id']
        await store.close()
    asyncio.run(run())


def test_startup_pauses_all_and_explicit_resume_is_scoped(tmp_path,monkeypatch):
    async def run():
        store,s,team,p,convs,tasks=await setup(tmp_path);old=(await s.claim('old'))[2]
        calls=[];cleaned=asyncio.Event()
        async def worker(self,*args,**kwargs):
            calls.append(kwargs['conversation_id'])
            try:
                await asyncio.Event().wait()
                yield 'content',{'content':'unreachable'}
            finally:cleaned.set()
        async def leader(*args,**kwargs):return AIMessage(content='Waiting for task')
        monkeypatch.setattr(WorkerAgent,'stream',worker);monkeypatch.setattr(team.leader,'next_message',leader)
        scheduler=Scheduler(s,team);await scheduler.start(pause_existing=True)
        try:
            await asyncio.sleep(.1);assert not calls
            for conv in convs:
                status=await s.conversation_execution(p,conv)
                assert status['paused'] and not status['running']
            assert (await s.get_request(p,tasks[0]['id']))['instance_id'] is None
            await s.begin_conversation_run(p,convs[0]);assert await s.claim() is None
            await s.resume_conversation_run(p,convs[0])
            async def started():return bool(calls)
            await until(started)
            assert len(calls)==1 and tasks[0]['id'] in calls[0]
            assert (await s.get_request(p,tasks[0]['id']))['workspace_id']==old['workspace_id']
            generation=await s.stop_conversation_run(p,convs[0])
            await team.cancel_conversation(convs[0]);await scheduler.cancel_conversation(p,convs[0])
            await s.finish_conversation_stop(p,convs[0],generation)
            assert cleaned.is_set()
            assert (await s.get_request(p,tasks[1]['id']))['paused_on_restart']
        finally:await scheduler.close();await store.close()
    asyncio.run(run())


def test_stop_cancels_live_leader_and_survives_http_disconnect(tmp_path,monkeypatch):
    from app import main
    async def run():
        store,s,team,p,convs,tasks=await setup(tmp_path)
        entered=asyncio.Event();cleanup=asyncio.Event();release=asyncio.Event()
        async def leader(*args,**kwargs):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cleanup.set();await release.wait()
        monkeypatch.setattr(team.leader,'next_message',leader)
        async def stream():
            async for _ in team.stream([HumanMessage(content='Work')],conversation_id=convs[0],workspace_path=tmp_path):pass
        live=asyncio.create_task(stream());await asyncio.wait_for(entered.wait(),3)
        async def context(conv):return s,p
        monkeypatch.setattr(main,'execution_context',context);monkeypatch.setattr(main,'get_team',lambda:team)
        monkeypatch.setattr(main,'_collaboration_scheduler',None);monkeypatch.setattr(main,'_conversation_stops',{})
        request=asyncio.create_task(main.stop_conversation(convs[0]));await asyncio.wait_for(cleanup.wait(),3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):await request
        assert (await s.conversation_execution(p,convs[0]))['stopping']
        release.set()
        async def stopped():return not (await s.conversation_execution(p,convs[0]))['stopping']
        await until(stopped)
        assert live.cancelled() and not getattr(team,'_conversation_tasks',{}).get(convs[0])
        await store.close()
    asyncio.run(run())


def test_reopening_app_pauses_live_backend_without_restarting_it(tmp_path,monkeypatch):
    from app import main
    async def run():
        store,s,team,p,convs,tasks=await setup(tmp_path)
        seen=[]
        async def cancel(conv):seen.append(conv)
        monkeypatch.setattr(team,'cancel_conversation',cancel)
        monkeypatch.setattr(main.collaboration_api,'_service',s)
        monkeypatch.setattr(main,'get_team',lambda:team)
        monkeypatch.setattr(main,'_collaboration_scheduler',None)
        result=await main._pause_previous_execution()
        assert result['paused_conversations']==2 and set(seen)==set(convs)
        assert await s.claim() is None
        for conv in convs:assert (await s.conversation_execution(p,conv))['paused']
        await store.close()
    asyncio.run(run())


def test_execution_http_endpoints_persist_status(tmp_path,monkeypatch):
    from app import main
    from fastapi.testclient import TestClient
    class TeamStub:
        def __init__(self):self.cancelled=[]
        async def cancel_conversation(self,conv):self.cancelled.append(conv)
    team=TeamStub()
    store=ChatStore(f'sqlite+aiosqlite:///{tmp_path}/http.db')
    monkeypatch.setattr(main,'store',store);monkeypatch.setattr(main,'get_team',lambda:team)
    with TestClient(main.app) as client:
        project=client.post('/api/projects',json={'name':'HTTP','workspace_path':str(tmp_path)}).json()['id']
        conv=client.post(f'/api/projects/{project}/conversations',json={}).json()['id']
        url=f'/api/conversations/{conv}/execution'
        assert client.get(url).json()['running'] is False
        service=main.collaboration_api._service
        client.portal.call(service.begin_conversation_run,project,conv)
        assert client.get(url).json()['running'] is True
        assert client.post(url+'/stop').json()['running'] is False
        assert team.cancelled==[conv]
        assert client.get(url).json()['stopping'] is False
        assert client.post(url+'/resume').status_code==409
        client.portal.call(service.begin_conversation_run,project,conv)
        assert client.post('/api/execution/pause').json()['paused_conversations']==1
        status=client.get(url).json()
        assert status['paused'] and not status['running']
        assert client.get('/api/conversations/missing/execution').status_code==404


def test_startup_fences_legacy_history_without_leader_session(tmp_path):
    from app.collaboration.coordinator import LeaderCoordinator

    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/legacy.db')
        project = (await store.create_project('Legacy', str(tmp_path)))['id']
        conv = (await store.create_conversation(project))['id']
        await store.add_user_message(conv, 'Previous work', 'legacy-message')
        service = Collaboration(store)
        async with service.transaction() as c:
            await service._event(c, project, 'leader', {'type': 'pending_work'}, conv)
        await service.pause_unfinished_on_startup()
        coordinator = LeaderCoordinator(service, None)
        await coordinator.start()
        await coordinator.tick()
        assert coordinator.job is None
        assert await service.conversation_stopped(project, conv)
        # A new user turn explicitly opens this conversation for work.
        await service.begin_conversation_run(project, conv)
        assert not await service.conversation_stopped(project, conv)
        await store.close()

    asyncio.run(run())
