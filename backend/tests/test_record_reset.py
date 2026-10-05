import asyncio

import httpx
import pytest
from sqlalchemy import text

from app import main
from app.agents.team import Team
from app.record_reset import RecordsGate, RecordsGateMiddleware
from app.storage import ChatStore, metadata


def test_reset_clears_persistent_and_runtime_records_after_draining(tmp_path, monkeypatch):
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "records.db"}')
        team = Team()
        gate = RecordsGate()
        monkeypatch.setattr(main, 'store', store)
        monkeypatch.setattr(main, 'get_team', lambda: team)
        monkeypatch.setattr(main, '_records_gate', gate)
        monkeypatch.setattr(main, '_runtime_store_identity', None)
        monkeypatch.setattr(main, '_checkpoint_connection', None)
        monkeypatch.setattr(main, '_collaboration_scheduler', None)
        monkeypatch.setenv('WDRUNGE_ENABLE_DEBUG_RESET', '1')
        async with main.lifespan(main.app):
            old_scheduler = main._collaboration_scheduler
            # Seed queued work without allowing any real model calls.
            await old_scheduler.close()
            workspace = tmp_path / 'workspace'
            workspace.mkdir()
            project = await store.create_project('Debug', str(workspace))
            conversation = await store.create_conversation(project['id'])
            cid = conversation['id']
            await store.add_user_message(cid, 'hello', 'm1')
            await store.upsert_activity(cid, dict(id='a1', agent='backend', tool='read', description='read', phase='completed'))
            await store.save_agent_session(cid, 'backend', 't1', 100)
            service = team.collaboration
            doc = (await service.publish(project['id'], 'backend', [dict(path='api.md', content='# API\nKeep me', summary='initial')]))[0]['document_id']
            await service.subscribe(project['id'], 'frontend', doc)
            await service.create_request(project['id'], 'leader', 'backend', 'test', 'r1', conversation=cid)
            await service.send(project['id'], 'leader', 'backend', 'hello', 'msg1', conversation=cid)
            # Exercise real LangGraph checkpoint and pending-write tables.
            from langgraph.checkpoint.base import empty_checkpoint
            checkpoint = empty_checkpoint()
            config = await team._checkpointer.aput({'configurable': {'thread_id': 't1', 'checkpoint_ns': ''}}, checkpoint, {}, {})
            await team._checkpointer.aput_writes(config, [('messages', 'pending')], 'task1')
            team.leader.total_tokens = 100
            team.workers['backend']._threads['old'] = 't1'
            team._pending_leader_receipts['old'] = []
            started, cleaned = asyncio.Event(), asyncio.Event()

            async def writer():
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    await store.add_user_message(cid, 'cleanup write', 'cleanup')
                    cleaned.set()

            task = asyncio.create_task(writer())
            gate.requests.add(task)
            await started.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/api/debug/records/reset', json={'confirmation': 'CLEAR_ALL_RECORDS'})
                assert response.status_code == 200, response.text
                assert response.json() == {'cleared': True, 'workspace_files_preserved': True}
                assert cleaned.is_set() and task.cancelled()
                assert old_scheduler.loop.done()
                assert main._collaboration_scheduler is not old_scheduler
                assert team.usage_snapshot()['total_tokens'] == 0
                assert not team.workers['backend']._threads
                assert not team._pending_leader_receipts
                assert (workspace / 'api.md').read_text() == '# API\nKeep me'
                tables = list(metadata.tables) + ['collaboration_records', 'collaboration_events', 'collaboration_receipts', 'collaboration_sections', 'checkpoints', 'writes']
                async with store.engine.connect() as connection:
                    for table in tables:
                        assert await connection.scalar(text(f'SELECT count(*) FROM "{table}"')) == 0, table
                assert (await client.get('/api/projects')).json() == []
                fresh = await client.post('/api/projects', json={'name': 'Fresh', 'workspace_path': str(workspace)})
                assert fresh.status_code == 201, fresh.text
                new_chat = await client.post(f'/api/projects/{fresh.json()["id"]}/conversations', json={})
                assert new_chat.status_code == 201, new_chat.text
    asyncio.run(scenario())


def test_gate_blocks_new_requests_and_waits_for_cancelled_cleanup():
    async def scenario():
        gate = RecordsGate()
        started, release, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        middleware = RecordsGateMiddleware(app, gate)
        output = []
        async def send(message):
            output.append(message)
        scope = {'type': 'http', 'path': '/api/test'}
        task = asyncio.create_task(middleware(scope, None, send))
        await started.wait()
        gate.resetting = True
        drain = asyncio.create_task(gate.drain())
        await cleaning.wait()
        assert not drain.done()
        await middleware(scope, None, send)
        assert output[0]['status'] == 503
        release.set()
        await drain
        assert task.cancelled() and not gate.requests
    asyncio.run(scenario())


def test_reset_requires_confirmation_and_survives_caller_disconnect(monkeypatch):
    async def scenario():
        gate = RecordsGate()
        monkeypatch.setattr(main, '_records_gate', gate)
        started, release = asyncio.Event(), asyncio.Event()
        completed = []
        async def clear():
            try:
                started.set()
                await release.wait()
                completed.append(True)
                return {'cleared': True, 'workspace_files_preserved': True}
            finally:
                gate.resetting = False
                gate.reset_task = None
        monkeypatch.setattr(main, '_clear_all_records', clear)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            monkeypatch.setenv('WDRUNGE_ENABLE_DEBUG_RESET', '0')
            payload = {'confirmation': 'CLEAR_ALL_RECORDS'}
            assert (await client.post('/api/debug/records/reset', json=payload)).status_code == 404
            monkeypatch.setenv('WDRUNGE_ENABLE_DEBUG_RESET', '1')
            assert (await client.post('/api/debug/records/reset', json={})).status_code == 422
            assert not started.is_set()
            caller = asyncio.create_task(client.post('/api/debug/records/reset', json=payload))
            await started.wait()
            reset_task = gate.reset_task
            assert (await client.post('/api/debug/records/reset', json=payload)).status_code == 409
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert gate.resetting
            release.set()
            await reset_task
            assert completed == [True] and not gate.resetting
    asyncio.run(scenario())
