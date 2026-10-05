"""Project environment registry: ports and commands are recorded by the platform,
claimed safely, shared with every agent and trusted by the command reviewer."""
import asyncio
import json
import socket
import sys
from types import SimpleNamespace

import pytest

from app.agents.tools import development
from app.agents.tools._workspace import active_workspace, source_workspace
from app.collaboration import environment
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.processes import Processes
from app.storage import ChatStore


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/env.db')
    project = (await store.create_project('Env', str(workspace)))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, workspace


@pytest.fixture(autouse=True)
def isolated_platform_ports(monkeypatch):
    monkeypatch.setattr(environment, '_platform_ports', set())
    monkeypatch.delenv('WDRUNGE_RESERVED_PORTS', raising=False)


def test_ports_are_allocated_free_and_never_reserved_or_shared(tmp_path, monkeypatch):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        environment.note_platform_port(8100)
        monkeypatch.setenv('WDRUNGE_RESERVED_PORTS', '8101')
        api = await s.register_service(p, 'backend', 'api', command=['python3', 'app.py'], cwd='backend')
        assert api['port'] not in {8100, 8101} and api['claimed'] and api['url'] == f"http://127.0.0.1:{api['port']}"
        # Re-registering keeps the port; another service gets a different one.
        assert (await s.register_service(p, 'backend', 'api'))['port'] == api['port']
        web = await s.register_service(p, 'frontend', 'web')
        assert web['port'] != api['port']
        with pytest.raises(ValueError, match='reserved by the Wdrunge platform'):
            await s.register_service(p, 'backend', 'admin', port=8100)
        with pytest.raises(ValueError, match='already registered for service api'):
            await s.register_service(p, 'frontend', 'web', port=api['port'])
        # A port some other program holds can be declared but is not claimed.
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            taken = await s.register_service(p, 'backend', 'legacy', port=busy.getsockname()[1])
        assert not taken['claimed'] and 'warning' in taken
        assert await s.trusted_ports(p) == {api['port']: 'api', web['port']: 'web'}
        await s.register_service(p, 'user', 'db', kind='tcp', port=free_port(), source='user')
        with pytest.raises(ValueError, match='Only the user'):
            await s.remove_service(p, 'backend', 'db')
        await s.remove_service(p, 'user', 'db')
        await store.close()
    asyncio.run(run())


def test_manifests_declare_services_without_overriding_registered_ones(tmp_path):
    async def run():
        store, s, p, workspace = await setup(tmp_path)
        (workspace / 'web').mkdir()
        (workspace / 'web' / 'package.json').write_text(json.dumps({'scripts': {'dev': 'vite --port 5174'}}))
        (workspace / 'web' / 'vite.config.ts').write_text('export default { server: { port: 5175 } }')
        (workspace / 'docker-compose.yml').write_text('services:\n  db:\n    ports:\n      - "55432:5432"\n')
        (workspace / '.env.example').write_text('API_PORT=8765\nDEBUG=1\n')
        (workspace / 'node_modules').mkdir()
        (workspace / 'node_modules' / 'package.json').write_text(json.dumps({'scripts': {'x': 'serve --port 1111'}}))
        await s.register_service(p, 'backend', 'api', port=8765)
        data = await s.environment(p)
        services = {svc['name']: svc for svc in data['services']}
        assert {svc['port'] for svc in services.values()} == {5174, 5175, 55432, 8765}
        assert services['api']['source'] == 'agent'
        assert services['web']['command'] == {'argv': ['npm', 'run', 'dev'], 'cwd': 'web'}
        assert services['db']['note'].startswith('declared in docker-compose.yml')
        assert all(svc['status'] == 'declared' for svc in services.values())
        # Discovery runs once per port: the second read adds nothing.
        assert await s.discover_services(p) == []
        await store.close()
    asyncio.run(run())


def test_every_agent_handoff_carries_the_environment(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        environment.note_platform_port(17420)
        await s.register_service(p, 'backend', 'api', port=free_port(), command=['python3', 'app.py'])
        await s.register_command(p, 'testing', 'test', ['pytest', '-q'], cwd='backend')
        handoff = await s.handoff(p, 'frontend')
        brief = handoff['environment']
        assert brief['reserved_ports'] == [17420]
        assert brief['services'][0]['name'] == 'api' and brief['services'][0]['start'] == 'python3 app.py'
        assert brief['commands'] == [{'name': 'test', 'run': 'pytest -q', 'cwd': 'backend', 'status': 'declared'}]
        saved = await s.handoff(p, 'leader', save=True, summary='stage')
        async with s.reading() as c:
            record = await s._get(c, 'handoff', p, saved['id'])
        assert 'environment' not in record
        await store.close()
    asyncio.run(run())


def test_agents_use_registry_tools_and_users_set_trusted_entries(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        token = active.set(Context(s, p, 'backend', '', 'ctx'))
        try:
            created = await execute_collaboration('register_service', {'name': 'api', 'command': ['python3', 'app.py']})
            assert created['claimed'] and created['owner'] == 'backend'
            invalid = await execute_collaboration('register_service', {'name': 'API Server'})
            assert 'error' in invalid
            listed = await execute_collaboration('environment', {})
            assert [svc['name'] for svc in listed['services']] == ['api']
            command = await execute_collaboration('register_command', {'name': 'test', 'argv': ['pytest']})
            assert command['status'] == 'declared'
        finally:
            active.reset(token)
        token = active.set(Context(s, p, 'leader', '', 'user'))
        try:
            user = await execute_collaboration('register_service', {'name': 'web', 'port': free_port()})
            assert user['source'] == 'user'
            removed = await execute_collaboration('register_service', {'name': 'api', 'remove': True})
            assert removed == {'removed': 'api'}
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


@pytest.fixture
def real_processes(monkeypatch):
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(policy, 'current', Approve())
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': True})
    monkeypatch.setattr(sandbox, 'command_line', lambda argv, *args, **kwargs: argv)
    manager = Processes()
    monkeypatch.setattr(process_module, 'processes', manager)
    monkeypatch.setattr(development, 'processes', manager)
    monkeypatch.setattr(development, 'STARTUP_GRACE', .5)
    return manager


def test_started_services_are_verified_in_the_registry(tmp_path, real_processes):
    async def run():
        store, s, p, workspace = await setup(tmp_path)
        (workspace / 'web').mkdir()
        registered = await s.register_service(p, 'frontend', 'web')
        port = registered['port']
        tokens = active.set(Context(s, p, 'frontend', '', 'ctx')), active_workspace.set(workspace), source_workspace.set(workspace)
        try:
            started = await development.execute_command.ainvoke({'command': sys.executable, 'background': True, 'name': 'web-dev',
                'cwd': 'web', 'arguments': ['-m', 'http.server', '--bind', '127.0.0.1', str(port)], 'ready_port': port})
            assert started['ready'] and started['registered_services'][0]['name'] == 'web'
            stub_port = free_port()
            # A server that prints its own address is recognized without ready_port.
            script = f"import http.server as h; print('Serving on http://127.0.0.1:{stub_port}/', flush=True); h.HTTPServer(('127.0.0.1', {stub_port}), h.SimpleHTTPRequestHandler).serve_forever()"
            observed = await development.execute_command.ainvoke({'command': sys.executable, 'background': True, 'name': 'Stub API',
                                                                  'arguments': ['-c', script]})
            assert observed['ports'] == [stub_port]
        finally:
            await real_processes.close()
            source_workspace.reset(tokens[2])
            active_workspace.reset(tokens[1])
            active.reset(tokens[0])
        services = {svc['name']: svc for svc in (await s.environment(p))['services']}
        assert services['web']['status'] == 'verified' and services['web']['command']['cwd'] == 'web'
        assert services['web']['command']['argv'][1:3] == ['-m', 'http.server']
        assert services['stub-api']['port'] == stub_port and services['stub-api']['source'] == 'observed'
        await store.close()
    asyncio.run(run())


def test_reviewer_trusts_registered_ports_and_never_platform_ports(tmp_path, monkeypatch):
    async def review(details):
        review.seen.append(details)
        return policy.Verdict(decision='allow', reason='本项目服务验证', confidence=.95, uncertain=False)
    review.seen = []
    monkeypatch.setattr(policy, 'review', review)
    monkeypatch.setattr(process_module.processes, 'jobs', {})
    async def run():
        store, s, p, workspace = await setup(tmp_path)
        api = await s.register_service(p, 'backend', 'api')
        environment.note_platform_port(17420)
        manager = policy.Permissions(store)
        await manager.initialize()
        tokens = active.set(Context(s, p, 'testing', '', 'ctx')), active_workspace.set(workspace.resolve())
        try:
            # Nothing is running, but the port is the project's registered api service.
            probe = {'kind': 'command', 'argv': ['curl', f"http://127.0.0.1:{api['port']}/health"], 'network': True}
            await asyncio.wait_for(manager.authorize(probe), 2)
            target = review.seen[-1]['operation_facts']['network_targets'][0]
            assert target['served_by'] == 'project service api'
            assert review.seen[-1]['project_environment']['services'][0]['claimed_by_project'] is True
            platform = {'kind': 'command', 'argv': ['curl', 'http://127.0.0.1:17420/api/team'], 'network': True}
            task = asyncio.create_task(manager.authorize(platform))
            row = await pending(manager)
            assert 'Wdrunge 平台自身的端口 [17420]' in row['reason']
            await manager.decide(row['id'], 'once')
            await asyncio.wait_for(task, 2)
            # Even an identical, human-approved command asks again for the platform port.
            task = asyncio.create_task(manager.authorize(platform))
            row = await pending(manager)
            await manager.decide(row['id'], 'deny')
            with pytest.raises(PermissionError):
                await asyncio.wait_for(task, 2)
        finally:
            active_workspace.reset(tokens[1])
            active.reset(tokens[0])
            await store.close()
    asyncio.run(run())


async def pending(manager):
    for _ in range(400):
        rows = [r for r in await manager.rows('approval') if r['state'] == 'pending']
        if rows:
            return rows[0]
        await asyncio.sleep(.005)
    raise AssertionError('No approval request')


def test_platform_learns_its_ports_from_loopback_traffic():
    calls = []
    async def app(scope, receive, send):
        calls.append(scope['type'])
    middleware = environment.PlatformPortsMiddleware(app)
    async def run():
        await middleware({'type': 'http', 'server': ('127.0.0.1', 17420), 'headers': [(b'origin', b'http://localhost:1420')]}, None, None)
        await middleware({'type': 'http', 'server': ('testserver', 80), 'headers': [(b'origin', b'https://example.com:8443')]}, None, None)
        await middleware({'type': 'lifespan'}, None, None)
    asyncio.run(run())
    assert environment.platform_ports() == {17420, 1420} and calls == ['http', 'http', 'lifespan']
