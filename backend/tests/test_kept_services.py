"""Services that outlive their task: kept on delivery, visible to colleagues, replaced
on restart, marked stale by later publishes and stopped with the conversation."""
import asyncio
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

from app.agents.tools import development
from app.agents.tools._workspace import active_workspace, source_workspace
from app.agents.tools.validation import ToolFailure
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.processes import Processes
from app.storage import ChatStore


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture
def manager(monkeypatch):
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
    monkeypatch.setattr(development, 'STARTUP_GRACE', .3)
    return manager


def test_kept_service_lifecycle_across_tasks(tmp_path, manager):
    project_root = tmp_path / 'project'
    snapshots = {name: project_root / '.wdrunge-runs' / name / 'work' for name in ('backend', 'testing')}
    for snapshot in snapshots.values():
        (snapshot / 'backend').mkdir(parents=True)
    port = free_port()
    server = ['-m', 'http.server', '--bind', '127.0.0.1', str(port)]

    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "kept.db"}')
        project = (await store.create_project('Kept', str(project_root)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        await service.register_service(project, 'leader', 'api', port=port)
        source = source_workspace.set(project_root)

        def task(member, request):
            return (active.set(Context(service, project, member, conversation, f'request:{request}', request)),
                    active_workspace.set(snapshots[member]))

        def leave(tokens):
            active_workspace.reset(tokens[1])
            active.reset(tokens[0])
        try:
            # The backend task starts api and asks to keep it.
            tokens = task('backend', 'r-backend')
            started = await development.execute_command.ainvoke({'command': sys.executable, 'arguments': server,
                'cwd': 'backend', 'background': True, 'name': 'api', 'ready_port': port, 'keep_running': True})
            assert started['kept'] is True and started['ready'] is True
            await manager.close(workspace=snapshots['backend'].resolve(), keep_services=True)  # delivered
            leave(tokens)

            # The testing task sees it running and does not start a second copy.
            tokens = task('testing', 'r-testing')
            [api] = (await development.detect_environment.ainvoke({}))['services']
            assert api['runtime']['state'] == 'running' and api['runtime']['kept']
            assert api['runtime']['started_by'] == 'backend'
            assert [p['service'] for p in (await development.manage_process.ainvoke({'action': 'list'}))['project_services']] == ['api']
            assert (await development.manage_process.ainvoke({'action': 'status', 'process_id': 'api'}))['state'] == 'running'
            with pytest.raises(ToolFailure, match='Service api is already running'):
                await development.execute_command.ainvoke({'command': sys.executable, 'arguments': server,
                    'cwd': 'backend', 'background': True, 'name': 'api', 'ready_port': port})

            # A later publish under the service's directory marks it stale for colleagues.
            manager.mark_stale(project_root, snapshots['testing'].resolve(), ['backend/app.py', 'README.md'])
            brief = await service.environment_brief(project)
            assert 'STALE' in brief['services'][0]['running'] and 'backend/app.py' in brief['services'][0]['running']

            # Restarting with keep_running replaces it.
            restarted = await development.execute_command.ainvoke({'command': sys.executable, 'arguments': server,
                'cwd': 'backend', 'background': True, 'name': 'api', 'ready_port': port, 'keep_running': True})
            assert restarted['replaced_process'] == api['runtime']['process_id'] and restarted['ready'] is True
            runtime = (await service.environment(project))['services'][0]['runtime']
            assert runtime['started_by'] == 'testing' and 'stale' not in runtime
            leave(tokens)

            # Stopping the conversation stops its kept services.
            assert await manager.close_services(project_root, conversation) == ['api']
            assert (await service.environment(project))['services'][0]['runtime']['state'] == 'stopped'
        finally:
            source_workspace.reset(source)
            await manager.close()
            await store.close()
    asyncio.run(scenario())


def test_undelivered_task_stops_even_kept_services(tmp_path, manager):
    async def scenario():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            job = await manager.start([sys.executable, '-c', 'import time; time.sleep(30)'], '.', 120, 'svc', keep=True)
            await manager.close(workspace=tmp_path.resolve(), keep_services=False)
            assert job.process.returncode is not None
        finally:
            await manager.close()
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
    asyncio.run(scenario())


def test_keeping_a_running_service_extends_its_lifetime(tmp_path, manager, monkeypatch):
    monkeypatch.setenv('WDRUNGE_SERVICE_LIFETIME_SECONDS', '60')
    async def scenario():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            job = await manager.start([sys.executable, '-c', 'import time; time.sleep(30)'], '.', 1, 'svc')
            manager.keep_service(job, 'api')
            await asyncio.sleep(1.5)  # past the original one-second lifetime
            assert job.process.returncode is None and not job.timed_out
            assert manager.service_job(tmp_path, name='api') is job
        finally:
            await manager.close()
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
    asyncio.run(scenario())


def test_user_stops_a_running_service_from_the_environment_panel(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.collaboration.api import router, configure
    stopped = []
    job = SimpleNamespace(id='p1')
    monkeypatch.setattr(process_module.processes, 'service_job',
                        lambda root, name=None, port=None: job if name == 'api' else None)
    async def stop(target):
        stopped.append(target)
    monkeypatch.setattr(process_module.processes, 'stop', stop)
    async def setup():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "api.db"}')
        project = (await store.create_project('Kept', str(tmp_path)))['id']
        service = Collaboration(store)
        await service.initialize()
        await service.register_service(project, 'user', 'api')
        return store, service, project
    store, service, project = asyncio.run(setup())
    configure(service)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        url = f'/api/projects/{project}/collaboration/services'
        assert client.post(url + '/api/stop').json() == {'stopped': 'api', 'process_id': 'p1'}
        assert stopped == [job]
        assert client.post(url + '/web/stop').status_code == 404
    asyncio.run(store.close())


def test_delivered_kept_service_moves_to_the_project_root(tmp_path, manager):
    """A service kept from a task snapshot cannot see later merges; after delivery it is
    started again from the project root, and later publishes restart it there."""
    import urllib.request
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'site').mkdir(parents=True)
    (project_root / 'site').mkdir(parents=True)
    (snapshot / 'site' / 'page.txt').write_text('snapshot')
    (project_root / 'site' / 'page.txt').write_text('project')
    port = free_port()

    def fetch():
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/page.txt', timeout=5) as response:
            return response.read().decode()

    async def scenario():
        tokens = active_workspace.set(snapshot), source_workspace.set(project_root)
        try:
            job = await manager.start([sys.executable, '-m', 'http.server', '--bind', '127.0.0.1', str(port)],
                                      'site', 120, 'web', network=True, reason='serve the site', keep=True)
            assert await manager.wait_ready(job, port, 10)
            job.service, job.started_by = 'web', 'backend'
            assert await asyncio.to_thread(fetch) == 'snapshot'
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        try:
            [moved] = await manager.relaunch_delivered(snapshot, project_root)
            assert job.process.returncode is not None
            assert Path(moved.cwd) == (project_root / 'site').resolve() and moved.keep and moved.network
            assert (moved.service, moved.started_by, moved.ports) == ('web', 'backend', {port})
            assert await asyncio.to_thread(fetch) == 'project'

            # Another task publishes under its directory: it restarts from the root again,
            # while a service still running in some snapshot is only marked stale.
            other = project_root / '.wdrunge-runs' / 'frontend' / 'work'
            manager.refresh_after_publish(project_root, other.resolve(), ['site/page.txt', 'README.md'])
            await asyncio.gather(*manager.tasks)
            [current] = manager.project_services(project_root)
            assert current is not moved and moved.process.returncode is not None and not current.stale
            assert await asyncio.to_thread(fetch) == 'project'
        finally:
            await manager.close()
    asyncio.run(scenario())
