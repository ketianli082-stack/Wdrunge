"""AI and whitelist routing use the actual permission gate, with no paid calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.execution import api, permissions as policy, sandbox
from app.execution.processes import Processes
from app.storage import ChatStore
from app.agents.tools._workspace import active_workspace


async def pending(manager):
    async def wait():
        while True:
            for row in await manager.rows('approval'):
                if row['state'] == 'pending':
                    return row
            await asyncio.sleep(.005)
    return await asyncio.wait_for(wait(), 2)


def test_confident_ai_allows_network_and_shared_reads_without_popup(tmp_path, monkeypatch):
    calls = []
    async def review(details):
        calls.append(details)
        return policy.Verdict(decision='allow', reason='必要的项目开发操作', confidence=.75, uncertain=False)
    monkeypatch.setattr(policy, 'review', review)
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/ai.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        try:
            for operation in [
                {'kind':'command', 'argv':['npm','ci'], 'network':True},
                {'kind':'command', 'argv':['python','-m','pytest'], 'read_paths':[str(tmp_path/'source/.venv')]},
                {'kind':'read_external_file', 'path':str(tmp_path/'config.ini')},
                {'kind':'move', 'path':str(tmp_path/'old.py'), 'destination':str(tmp_path/'new.py')},
            ]:
                await asyncio.wait_for(manager.authorize(operation), 2)
            assert len(calls) == 4
            assert not manager.pending and not await manager.rows('approval')
            assert not await manager.rows('grant')
            assert all(row['source'] == 'ai' for row in await manager.rows('review'))
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize('verdict', [
    dict(decision='allow', reason='有一点疑问', confidence=.7499, uncertain=False),
    dict(decision='allow', reason='高置信度但存在疑问', confidence=.99, uncertain=True),
    dict(decision='allow', reason='缺少判断依据'),
    dict(decision='ask', reason='请确认目标', confidence=.99, uncertain=False),
    dict(decision='deny', reason='有风险', confidence=.99, uncertain=False),
])
def test_uncertain_ai_falls_back_to_human(tmp_path, monkeypatch, verdict):
    async def review(details):return policy.Verdict(**verdict)
    monkeypatch.setattr(policy, 'review', review)
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/uncertain.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        try:
            task = asyncio.create_task(manager.authorize({'kind':'command','argv':['npm','run','build'],'network':True}))
            row = await pending(manager)
            assert not task.done() and not await manager.rows('review')
            await manager.decide(row['id'], 'once')
            await task
            assert not await manager.rows('grant')
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_whitelist_exact_boundaries_and_persisted_settings(tmp_path, monkeypatch):
    calls = []
    async def review(details):
        calls.append(details)
        return policy.Verdict(decision='ask', reason='需要人工确认')
    monkeypatch.setattr(policy, 'review', review)
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/white.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        try:
            await asyncio.wait_for(manager.authorize({'kind':'command','argv':['node','--version']}), 2)
            await asyncio.wait_for(manager.authorize({'kind':'command','argv':['git','status','--short']}), 2)
            assert not calls and len(await manager.rows('review')) == 2
            for operation in [
                {'kind':'command','argv':['node','--version','--eval','code']},
                {'kind':'command','argv':['node','--version'],'network':True},
                {'kind':'command','argv':['node','--version'],'read_paths':['/external']},
                {'kind':'command','argv':['node','--version'],'write_paths':['/external']},
                {'kind':'command','argv':['node','--version'],'cwd':str(tmp_path.parent)},
                {'kind':'command','argv':['/arbitrary/node','--version']},
                {'kind':'command','argv':['git','reset','--hard']},
            ]:
                task = asyncio.create_task(manager.authorize(operation))
                await manager.decide((await pending(manager))['id'], 'deny')
                with pytest.raises(PermissionError):await task
            await manager.configure(policy.ApprovalSettings(ai_review=False, whitelist=[]))
            restarted = policy.Permissions(store)
            await restarted.initialize()
            assert not restarted.settings.ai_review and not restarted.settings.whitelist
            task = asyncio.create_task(restarted.authorize({'kind':'command','argv':['node','--version']}))
            await restarted.decide((await pending(restarted))['id'], 'once')
            await task
            assert len(calls) == 7
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize('content', [
    'not json',
    json.dumps(dict(decision='allow',reason='ok',confidence=1.1,uncertain=False)),
    json.dumps(dict(decision='allow',reason='ok',confidence=.99,uncertain='false')),
])
def test_malformed_reviewer_response_asks_human(monkeypatch, content):
    class FakeModel:
        def __init__(self, **kwargs):pass
        async def ainvoke(self, messages):return SimpleNamespace(content=content)
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-only')
    monkeypatch.setattr(policy,'ChatOpenAI',FakeModel)
    assert asyncio.run(policy.review({})).decision == 'ask'


def test_reviewer_inspects_real_scripts_and_trusted_user_context(tmp_path, monkeypatch):
    monkeypatch.setenv('WDRUNGE_REVIEW_MODEL', 'qwen-flash')
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'build':'vite build'}}))
    captured = {}
    class FakeModel:
        def __init__(self, **kwargs):captured['model'] = kwargs
        async def ainvoke(self, messages):
            captured['details'] = json.loads(messages[1].content)
            return SimpleNamespace(content=json.dumps(dict(decision='allow',reason='项目构建',confidence=.98,uncertain=False)))
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-only')
    monkeypatch.setattr(policy,'ChatOpenAI',FakeModel)
    async def run():
        from app.collaboration.service import Collaboration
        from app.collaboration.tools import Context, active
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/context.db')
        project = (await store.create_project('Policy',str(tmp_path)))['id']
        conv = (await store.create_conversation(project))['id']
        await store.add_user_message(conv,'构建这个项目','human')
        service = Collaboration(store)
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        ctx = active.set(Context(service,project,'frontend',conv,'review-context'))
        try:
            await asyncio.wait_for(manager.authorize({'kind':'command','argv':['npm','run','build']}), 2)
            assert captured['details']['trusted_user_request'] == '构建这个项目'
            context = json.loads(captured['details']['project_context']['manifest_data'])
            assert context['runtimes'][0]['scripts']['build'] == 'vite build'
            assert captured['model']['model'] == 'qwen-flash'
        finally:
            active.reset(ctx)
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_auto_approval_reaches_process_execution_and_keeps_sandbox(tmp_path, monkeypatch):
    async def review(details):return policy.Verdict(decision='allow',reason='测试操作',confidence=.98,uncertain=False)
    monkeypatch.setattr(policy,'review',review)
    monkeypatch.setattr(sandbox,'capability',lambda:{'available':True})
    sandbox_calls = []
    def launcher(argv,*args,**kwargs):
        sandbox_calls.append(kwargs)
        return argv
    # Only the OS transport is mocked; authorization uses the production gate.
    monkeypatch.setattr(sandbox,'command_line',launcher)
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/process.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        monkeypatch.setattr(policy,'current',manager)
        processes = Processes()
        token = active_workspace.set(tmp_path)
        try:
            result = await asyncio.wait_for(processes.run(['echo','approved'],network=True), 3)
            assert result['exit_code'] == 0 and 'approved' in result['output']
            assert sandbox_calls[0]['network'] is True
            assert not await manager.rows('approval')
        finally:
            await processes.close()
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_settings_control_is_authenticated_and_validated(tmp_path, monkeypatch):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/settings.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        monkeypatch.setattr(policy,'current',manager)
        app = FastAPI()
        app.include_router(api.router)
        headers = {'Authorization':f'Bearer {api.control_token}'}
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            settings = dict(ai_review=False,whitelist=['versions'])
            assert (await client.post('/api/execution/settings',json=settings)).status_code == 401
            assert (await client.post('/api/execution/settings',headers=headers,json={**settings,'whitelist':['all_commands']})).status_code == 422
            assert (await client.post('/api/execution/settings',headers=headers,json=settings)).status_code == 200
            state = (await client.get('/api/execution/permissions',headers=headers)).json()
            assert state['settings'] == {**settings, 'allow_all': False}
            assert state['reviewer']['confidence_threshold'] == .75
        await store.close()
    asyncio.run(run())


def test_reviewer_receives_task_services_targets_and_precedents(tmp_path, monkeypatch):
    from collections import deque
    from app.execution import processes as process_module
    captured = []
    async def review(details):
        captured.append(details)
        return policy.Verdict(decision='ask', reason='需要确认', confidence=.5, uncertain=True)
    monkeypatch.setattr(policy, 'review', review)
    workspace = tmp_path.resolve()
    service = SimpleNamespace(scope=str(workspace), workspace=str(workspace), name='backend-dev',
        argv=['.venv/bin/uvicorn', 'main:app'], process=SimpleNamespace(returncode=None),
        logs=deque([b'INFO: Uvicorn running on http://127.0.0.1:8010 (Press CTRL+C to quit)\n']))
    monkeypatch.setattr(process_module.processes, 'jobs', {'job': service})
    async def run():
        from app.collaboration.service import Collaboration
        from app.collaboration.tools import Context, active
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/facts.db')
        project = (await store.create_project('Facts', str(workspace)))['id']
        conv = (await store.create_conversation(project))['id']
        await store.add_user_message(conv, '写一个 hello world 接口', 'human')
        service_api = Collaboration(store)
        request = await service_api.create_request(project, 'leader', 'backend', '实现 GET /api/hello', 'api', conversation=conv)
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(workspace)
        ctx = active.set(Context(service_api, project, 'backend', conv, 'facts', request=request['id']))
        try:
            probe = {'kind': 'command', 'argv': ['curl', '-s', 'http://127.0.0.1:8010/api/hello'], 'network': True}
            task = asyncio.create_task(manager.authorize(probe))
            row = await pending(manager)
            await manager.decide(row['id'], 'once')
            await asyncio.wait_for(task, 2)
            foreign = {'kind': 'command', 'argv': ['curl', 'http://127.0.0.1:8000/', 'https://example.com/x', '/etc/hosts'], 'network': True}
            task = asyncio.create_task(manager.authorize(foreign))
            row = await pending(manager)
            await manager.decide(row['id'], 'deny')
            with pytest.raises(PermissionError):
                await asyncio.wait_for(task, 2)
            first, second = captured
            assert first['requester']['role'] == 'backend'
            assert first['current_task']['goal'] == '实现 GET /api/hello'
            assert first['task_services'] == [{'name': 'backend-dev', 'argv': ['.venv/bin/uvicorn', 'main:app'], 'ports': [8010], 'same_task': True, 'running': True}]
            assert first['operation_facts']['network_targets'][0] == {'url': 'http://127.0.0.1:8010/api/hello', 'host': '127.0.0.1', 'port': 8010, 'loopback': True, 'served_by': 'backend-dev'}
            assert first['operation_facts']['executable_location'] == 'PATH lookup'
            targets = second['operation_facts']['network_targets']
            assert [(t['port'], t['loopback'], t['served_by']) for t in targets] == [(8000, True, None), (443, False, None)]
            assert second['operation_facts']['paths_outside_project'] == ['/etc/hosts']
            assert second['precedents'] == [{'argv': ['curl', '-s', 'http://127.0.0.1:8010/api/hello'], 'decision': 'human_allowed_once', 'network': True}]
        finally:
            active.reset(ctx)
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_ai_cannot_approve_foreign_loopback_ports_without_human_precedent(tmp_path, monkeypatch):
    from app.execution import processes as process_module
    async def review(details):
        return policy.Verdict(decision='allow', reason='本机接口验证', confidence=.95, uncertain=False)
    monkeypatch.setattr(policy, 'review', review)
    monkeypatch.setattr(process_module.processes, 'jobs', {})
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/foreign.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path.resolve())
        try:
            probe = {'kind': 'command', 'argv': ['curl', 'http://127.0.0.1:8000/api/hello'], 'network': True}
            task = asyncio.create_task(manager.authorize(probe))
            row = await pending(manager)
            assert '8000' in row['reason'] and row['review'] == 'ask'
            await manager.decide(row['id'], 'once')
            await asyncio.wait_for(task, 2)
            await asyncio.wait_for(manager.authorize(probe), 2)
            assert [r['source'] for r in await manager.rows('review')] == ['ai']
            other = {'kind': 'command', 'argv': ['curl', 'http://127.0.0.1:8000/admin/reset'], 'network': True}
            task = asyncio.create_task(manager.authorize(other))
            await manager.decide((await pending(manager))['id'], 'deny')
            with pytest.raises(PermissionError):
                await asyncio.wait_for(task, 2)
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_long_command_output_is_compacted_but_full_log_remains(tmp_path, monkeypatch):
    import sys
    from app.execution import processes as process_module
    from app.agents.tools.development import execute_command, manage_process
    from app.agents.tools._workspace import source_workspace
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(policy, 'current', Approve())
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': True})
    monkeypatch.setattr(sandbox, 'command_line', lambda argv, *args, **kwargs: argv)
    manager = Processes()
    monkeypatch.setattr(process_module, 'processes', manager)
    monkeypatch.setattr('app.agents.tools.development.processes', manager)
    script = "for i in range(3000): print(f'line {i} ok')\nprint('ERROR: build step 7 failed')\nfor i in range(3000): print(f'tail {i}')"
    async def run():
        token, source = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            result = await execute_command.ainvoke({'argv': [sys.executable, '-c', script]})
            assert result['exit_code'] == 0 and result['output_truncated']
            assert len(result['output'].encode()) < 5000
            assert result['output'].startswith('line 0 ok') and result['output'].rstrip().endswith('tail 2999')
            assert 'ERROR: build step 7 failed' in result['output']
            first = await manage_process.ainvoke({'action': 'logs', 'process_id': result['id'], 'limit': 30000})
            rest = await manage_process.ainvoke({'action': 'logs', 'process_id': result['id'],
                                                 'offset': first['next_offset'], 'limit': 30000})
            assert first['output'].startswith('line 0 ok') and 'ERROR: build step 7 failed' in rest['output']
            short = await execute_command.ainvoke({'argv': [sys.executable, '-c', 'print("hi")']})
            assert short['output'] == 'hi\n' and 'output_truncated' not in short
        finally:
            await manager.close()
            source_workspace.reset(source)
            active_workspace.reset(token)
    asyncio.run(run())


def test_allow_all_skips_every_prompt_but_keeps_isolation(tmp_path, monkeypatch):
    reviewed = []
    async def review(details):
        reviewed.append(details)
        return policy.Verdict(decision='ask', reason='需要确认', confidence=.2, uncertain=True)
    monkeypatch.setattr(policy, 'review', review)
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/allow-all.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path.resolve())
        try:
            foreign = {'kind': 'command', 'argv': ['curl', 'http://127.0.0.1:8000/admin'], 'network': True}
            waiting = asyncio.create_task(manager.authorize(foreign))
            await pending(manager)
            # Turning the switch on releases what is already waiting, then nothing asks.
            await manager.configure(policy.ApprovalSettings(allow_all=True))
            await asyncio.wait_for(waiting, 2)
            await asyncio.wait_for(manager.authorize({'kind': 'command', 'argv': ['rm', '-rf', 'build'], 'network': False}), 2)
            await asyncio.wait_for(manager.authorize({'kind': 'read_external_file', 'path': '/etc/hosts', 'reason': 'check'}), 2)
            assert len(reviewed) == 1  # only the call made before the switch was reviewed
            sources = [row.get('source') for row in await manager.rows('review')]
            assert sources.count('allow_all') == 2
            restarted = policy.Permissions(store)
            await restarted.initialize()
            assert restarted.settings.allow_all
            await manager.configure(policy.ApprovalSettings(allow_all=False))
            task = asyncio.create_task(manager.authorize(foreign))
            row = await pending(manager)
            await manager.decide(row['id'], 'deny')
            with pytest.raises(PermissionError):
                await asyncio.wait_for(task, 2)
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())
