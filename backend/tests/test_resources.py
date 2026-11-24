"""Connected services: configured once by the user, prepared per project by the platform,
passed to project processes as environment variables and never shown to agents."""
import asyncio
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.tools._workspace import active_workspace, source_workspace
from app.collaboration import environment
from app.collaboration.service import Collaboration
from app.collaboration.workspaces import IsolatedWorkspace, secret_leaks
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.processes import Job, Processes, mask
from app.resources import docker, kinds, service
from app.resources.service import ConnectionInput, Resources
from app.storage import ChatStore


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


async def setup(tmp_path, name='Blog'):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/app.db')
    project = await store.create_project(name, str(workspace.resolve()))
    return store, Resources(store), project, workspace


@pytest.fixture
def provisioned(monkeypatch):
    calls = []

    async def provision(kind, host, port, admin, admin_password, database, user, password):
        calls.append({'admin': admin, 'admin_password': admin_password, 'database': database, 'user': user, 'password': password})
    monkeypatch.setattr(kinds, 'provision', provision)
    return calls


@pytest.fixture(autouse=True)
def no_global_resources(monkeypatch):
    monkeypatch.setattr(service, 'current', None)
    monkeypatch.setattr(environment, '_platform_ports', set())


def test_saved_connections_never_return_secrets_and_keep_them_on_edit(tmp_path):
    async def run():
        store, resources, _, _ = await setup(tmp_path)
        saved = await resources.save(ConnectionInput(name='mysql', type='mysql', username='root', password='r00t-secret',
                                                     extra=[{'key': 'MYSQL_SSL_MODE', 'value': 'DISABLED'},
                                                            {'key': 'MYSQL_API_TOKEN', 'value': 'tok-123456', 'secret': True}]))
        assert saved['has_password'] and saved['port'] == 3306 and saved['env_prefix'] == 'MYSQL'
        assert 'r00t-secret' not in json.dumps(saved) and 'tok-123456' not in json.dumps(saved)
        assert saved['extra'] == [{'key': 'MYSQL_SSL_MODE', 'secret': False, 'value': 'DISABLED', 'has_value': True},
                                  {'key': 'MYSQL_API_TOKEN', 'secret': True, 'value': '', 'has_value': True}]
        # Editing without retyping the password or secret field keeps both.
        edited = await resources.save(ConnectionInput(name='mysql', type='mysql', username='root', port=3307,
                                                      extra=[{'key': 'MYSQL_API_TOKEN', 'secret': True}]), saved['id'])
        [row] = await resources._rows()
        assert edited['port'] == 3307 and row['secrets'] == {'password': 'r00t-secret', 'extra': {'MYSQL_API_TOKEN': 'tok-123456'}}
        cleared = await resources.save(ConnectionInput(name='mysql', type='mysql', clear_password=True), saved['id'])
        assert not cleared['has_password']
        await store.close()
    asyncio.run(run())


def test_names_prefixes_and_variables_are_validated(tmp_path):
    async def run():
        store, resources, _, _ = await setup(tmp_path)
        await resources.save(ConnectionInput(name='cache', type='redis'))
        with pytest.raises(ValueError, match='已经有名为 cache'):
            await resources.save(ConnectionInput(name='cache', type='redis'))
        with pytest.raises(ValueError, match='变量前缀 CACHE'):
            await resources.save(ConnectionInput(name='cache2', type='redis', env_prefix='CACHE'))
        # A custom type is any name; it is reachable through its own variables.
        custom = await resources.save(ConnectionInput(name='search-engine', type='meilisearch', port=7700,
                                                      extra=[{'key': 'MEILI_MASTER_KEY', 'value': 'k' * 16, 'secret': True}]))
        assert custom['env_prefix'] == 'SEARCH_ENGINE' and custom['type_label'] == 'meilisearch' and not custom['can_provision']
        with pytest.raises(ValueError):
            ConnectionInput(name='x', type='custom', extra=[{'key': 'PATH', 'value': '/tmp'}])
        with pytest.raises(ValueError):
            ConnectionInput(name='x', type='custom', host='user@host')
        await store.close()
    asyncio.run(run())


def test_a_project_without_a_connected_database_gets_its_own_sqlite_file(tmp_path):
    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        provided = await resources.provided(workspace)
        data = workspace.resolve() / '.wdrunge-data'
        assert provided.data_dir == data and (data / '.gitignore').read_text() == '*\n'
        assert provided.env['DATABASE_URL'] == f'sqlite:///{data}/app.db' and provided.env['DB_ENGINE'] == 'sqlite'
        assert provided.env['WDRUNGE_DATA_DIR'] == str(data) and provided.secrets == () and provided.ports == ()
        # A folder that is not a project gets nothing.
        assert (await resources.provided(tmp_path)).env == {}
        await store.close()
    asyncio.run(run())


def test_each_project_gets_its_own_database_and_account(tmp_path, provisioned):
    async def run():
        store, resources, project, workspace = await setup(tmp_path, 'My Blog!')
        await resources.save(ConnectionInput(name='mysql', type='mysql', username='root', password='admin-pass'))
        await resources.save(ConnectionInput(name='cache', type='redis', password='redis-pass'))
        provided = await resources.provided(workspace)
        [call] = provisioned
        name = f"wd_my_blog_{project['id'].replace('-', '')[:6]}"
        assert (call['admin'], call['admin_password'], call['database'], call['user']) == ('root', 'admin-pass', name, name)
        env = provided.env
        assert env['DB_ENGINE'] == 'mysql' and env['DB_NAME'] == name and env['DB_USER'] == name
        assert env['DB_PASSWORD'] == call['password'] != 'admin-pass'
        assert env['DATABASE_URL'] == f"mysql://{name}:{call['password']}@127.0.0.1:3306/{name}"
        assert env['SQLALCHEMY_DATABASE_URL'].startswith('mysql+pymysql://')
        assert env['MYSQL_DATABASE'] == name and env['CACHE_URL'] == 'redis://:redis-pass@127.0.0.1:6379'
        # The admin account never reaches the project.
        assert 'admin-pass' not in json.dumps(env) and env.get('MYSQL_USER') == name
        assert set(provided.secrets) == {call['password'], 'redis-pass'} and provided.ports == (3306, 6379)
        # Prepared once; later commands reuse the account.
        await resources.provided(workspace)
        assert len(provisioned) == 1
        # Agents see names and variables, never values.
        brief = await resources.brief(project['id'])
        text = json.dumps(brief)
        assert call['password'] not in text and 'admin-pass' not in text and 'redis-pass' not in text
        mysql = next(item for item in brief['resources'] if item['name'] == 'mysql')
        assert mysql['own_database'] and mysql['database'] == name and 'MYSQL_PASSWORD' in mysql['env']
        assert brief['database']['engine'] == 'mysql' and 'DATABASE_URL' in brief['database']['env']
        assert (await resources.known_secrets(project['id'])).keys() >= {call['password'], 'admin-pass', 'redis-pass'}
        await store.close()
    asyncio.run(run())


def test_a_database_that_is_down_does_not_block_commands(tmp_path, monkeypatch):
    attempts = []

    async def provision(*args):
        attempts.append(args)
        raise ConnectionRefusedError('refused')
    monkeypatch.setattr(kinds, 'provision', provision)

    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        await resources.save(ConnectionInput(name='mysql', type='mysql', username='root', password='admin-pass'))
        provided = await resources.provided(workspace)
        assert provided.env['DB_ENGINE'] == 'sqlite' and 'MYSQL_HOST' not in provided.env
        status = await resources.project_status(project['id'])
        assert status['resources'][0]['state'] == 'error' and '连接不上' in status['resources'][0]['error']
        assert status['resources'][0]['user'] is None
        # Not retried on every command, but the user can retry at once.
        await resources.provided(workspace)
        assert len(attempts) == 1
        await resources.project_status(project['id'], retry=True)
        assert len(attempts) == 2
        await store.close()
    asyncio.run(run())


def test_connections_can_be_limited_to_some_projects(tmp_path, provisioned):
    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        other = tmp_path / 'other'
        other.mkdir()
        second = await store.create_project('Other', str(other.resolve()))
        await resources.save(ConnectionInput(name='mysql', type='mysql', password='x' * 8, projects=[second['id']]))
        assert (await resources.provided(workspace)).env['DB_ENGINE'] == 'sqlite'
        assert (await resources.provided(other)).env['DB_ENGINE'] == 'mysql'
        assert await resources.names(project['id']) == {}
        await store.close()
    asyncio.run(run())


def test_settings_api_needs_the_local_control_token(tmp_path, monkeypatch, provisioned):
    from app.execution import api as execution_api
    from app import main

    async def prepare():
        store, resources, project, _ = await setup(tmp_path)
        await store.initialize()
        return store, resources, project
    store, resources, project = asyncio.run(prepare())
    monkeypatch.setattr(service, 'current', resources)
    client = TestClient(main.app)
    assert client.get('/api/resources').status_code == 401
    headers = {'Authorization': f'Bearer {execution_api.control_token}'}
    created = client.post('/api/resources', headers=headers, json={'name': 'mysql', 'type': 'mysql', 'password': 'pw-123456'})
    assert created.status_code == 201 and 'pw-123456' not in created.text
    listed = client.get('/api/resources', headers=headers).json()
    assert [c['name'] for c in listed['connections']] == ['mysql'] and any(t['type'] == 'redis' for t in listed['types'])
    assert client.post('/api/resources', headers=headers, json={'name': 'Bad Name', 'type': 'mysql'}).status_code == 422
    status = client.get(f"/api/resources/projects/{project['id']}", headers=headers).json()
    assert status['database'] == 'mysql' and status['resources'][0]['state'] == 'ready'
    assert client.delete(f"/api/resources/{created.json()['id']}", headers=headers).status_code == 200
    assert client.get('/api/resources', headers=headers).json()['connections'] == []


def test_docker_containers_are_described_without_running_docker():
    inspected = {
        'Id': 'abcdef1234567890', 'Name': '/mysql8', 'Config': {
            'Image': 'mysql:8.4.8', 'Env': ['MYSQL_ROOT_PASSWORD=docker-root', 'PATH=/usr/bin'], 'Cmd': ['mysqld']},
        'NetworkSettings': {'Ports': {'33060/tcp': None, '3306/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '13306'}]}}}
    item = docker.describe(inspected)
    assert item == {'container_id': 'abcdef123456', 'container': 'mysql8', 'image': 'mysql:8.4.8', 'type': 'mysql',
                    'host': '127.0.0.1', 'port': 13306, 'username': 'root', 'password': 'docker-root', 'database': ''}
    redis = docker.describe({'Config': {'Image': 'redis:7', 'Cmd': ['redis-server', '--requirepass', 'cache-pw']},
                             'NetworkSettings': {'Ports': {'6379/tcp': [{'HostPort': '6379'}]}}})
    assert (redis['type'], redis['password'], redis['port']) == ('redis', 'cache-pw', 6379)
    other = docker.describe({'Config': {'Image': 'getmeili/meilisearch:v1'}, 'NetworkSettings': {'Ports': {'7700/tcp': [{'HostPort': '7701'}]}}})
    assert (other['type'], other['port']) == ('meilisearch', 7701)


def test_docker_import_reads_the_password_once_and_lists_without_it(tmp_path, monkeypatch):
    async def containers():
        return [{'container_id': 'abc', 'container': 'mysql8', 'image': 'mysql:8', 'type': 'mysql', 'host': '127.0.0.1',
                 'port': 3306, 'username': 'root', 'password': 'docker-root', 'database': ''}]
    monkeypatch.setattr(docker, 'containers', containers)

    async def run():
        store, resources, _, _ = await setup(tmp_path)
        [listed] = await resources.docker_candidates()
        assert 'password' not in listed and listed['has_password'] and not listed['imported']
        saved = await resources.docker_import('mysql8')
        assert 'test' in saved
        assert saved['name'] == 'mysql' and saved['source'] == {'kind': 'docker', 'container': 'mysql8', 'image': 'mysql:8'}
        assert (await resources._rows())[0]['secrets']['password'] == 'docker-root'
        assert (await resources.docker_candidates())[0]['imported']
        assert (await resources.docker_import('abc'))['name'] == 'mysql-2'
        await store.close()
    asyncio.run(run())


def test_project_processes_receive_variables_and_their_output_is_masked(tmp_path, monkeypatch, provisioned):
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        operations = []

        async def authorize(self, operation, **kwargs):
            self.operations.append(operation)
    approve = Approve()
    monkeypatch.setattr(policy, 'current', approve)
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': True})
    seen = {}

    def command_line(argv, *args, **kwargs):
        seen.update(kwargs)
        return argv
    monkeypatch.setattr(sandbox, 'command_line', command_line)

    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        monkeypatch.setattr(service, 'current', resources)
        await resources.save(ConnectionInput(name='mysql', type='mysql', password='admin-pass'))
        manager = Processes()
        tokens = active_workspace.set(workspace.resolve()), source_workspace.set(None)
        try:
            result = await manager.run([sys.executable, '-c', 'import os; print(os.environ["DB_NAME"], os.environ["DB_PASSWORD"])'],
                                       '.', 30, name='probe')
        finally:
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
        password = provisioned[0]['password']
        assert result['exit_code'] == 0 and provisioned[0]['database'] in result['output']
        assert password not in result['output'] and '*' * len(password) in result['output']
        assert seen['service_ports'] == (3306,) and seen['data_paths'] == [workspace.resolve() / '.wdrunge-data']
        operation = approve.operations[0]
        assert operation['resources']['connected_services'] == ['mysql'] and password not in json.dumps(operation)
        await manager.close()
        await store.close()
    asyncio.run(run())


def test_masking_keeps_log_offsets():
    assert mask(b'pw=secret!! end', (b'secret!!',)) == b'pw=******** end'


def test_clean_environment_adds_provided_variables(tmp_path):
    env = sandbox.clean_environment(tmp_path, tmp_path, {'DATABASE_URL': 'sqlite:////x/app.db'})
    assert env['DATABASE_URL'] == 'sqlite:////x/app.db' and env['HOME'] == str(tmp_path)


def test_connected_services_are_used_not_registered_declared_or_awaited(tmp_path, provisioned):
    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        service.current = resources
        try:
            await resources.save(ConnectionInput(name='mysql', type='mysql', password='admin-pass'))
            collaboration = Collaboration(store)
            await collaboration.initialize()
            p = project['id']
            with pytest.raises(ValueError, match='platform connects'):
                await collaboration.register_service(p, 'backend', 'mysql', kind='tcp', port=3306)
            with pytest.raises(ValueError, match='platform connects'):
                await collaboration.register_service(p, 'backend', 'db', kind='tcp', port=3306)
            with pytest.raises(ValueError, match='Remove it from services'):
                await collaboration.create_request(p, 'leader', 'backend', 'build the API', 'api', services=['mysql'])
            assert await collaboration.observe_service(p, 'backend', 3306, 'mysqld', ['mysqld'], '.') is None
            async with collaboration.reading() as c:
                from app.collaboration.planning import need_satisfied
                assert await need_satisfied(collaboration, c, p, {'service': 'mysql'})
                assert not await need_satisfied(collaboration, c, p, {'service': 'api'})
            brief = await collaboration.environment_brief(p)
            assert brief['connected']['database']['engine'] == 'mysql'
            assert provisioned[0]['password'] not in json.dumps(brief) and 'admin-pass' not in json.dumps(brief)
        finally:
            service.current = None
            await store.close()
    asyncio.run(run())


def test_reviewer_recognizes_database_urls_and_connected_ports():
    targets = policy.network_targets(['mysql://wd:pw@127.0.0.1/wd', 'postgresql+psycopg://localhost:6543/x', 'redis://h:6380'])
    assert [(t['port'], t['loopback']) for t in targets] == [(3306, True), (6543, True), (6380, False)]
    facts = policy.operation_facts({'workspace': '/w', 'argv': ['python3', '-c', 'x', 'mysql://u@127.0.0.1:3306/db']}, [],
                                   {'connected': {3306: 'mysql'}})
    assert facts['network_targets'][0]['served_by'].startswith('connected service mysql')


def test_runtime_data_and_secrets_stay_out_of_deliveries(tmp_path):
    root = tmp_path / 'project'
    (root / 'backend').mkdir(parents=True)
    (root / 'backend' / 'app.py').write_text('print(1)\n')
    (root / 'backend' / 'blog.db').write_bytes(b'SQLite format 3\x00old')
    (root / '.wdrunge-data').mkdir()
    (root / '.wdrunge-data' / 'app.db').write_bytes(b'data')
    workspace = IsolatedWorkspace(root, 'task1')
    work = workspace.prepare()
    assert not (work / 'backend' / 'blog.db').exists() and not (work / '.wdrunge-data').exists()
    (work / 'backend' / 'blog.db').write_bytes(b'SQLite format 3\x00new')
    (work / 'backend' / 'blog.db-wal').write_bytes(b'wal')
    (work / 'backend' / 'config.py').write_text("PASSWORD = 'wd-secret-123'\n")
    changes = workspace.plan()
    assert [item['path'] for item in changes] == ['backend/config.py']
    assert secret_leaks(changes, {'wd-secret-123': 'mysql'}) == [('backend/config.py', 'mysql')]
    assert secret_leaks(changes, {'other-secret': 'mysql'}) == []


def test_a_snapshot_taken_before_the_exclusion_never_deletes_the_live_database(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'blog.db').write_bytes(b'live')
    workspace = IsolatedWorkspace(root, 'old')
    workspace.prepare()
    manifest = json.loads(workspace.manifest_path.read_text())
    manifest['blog.db'] = 'recorded-by-an-older-version'
    workspace.manifest_path.write_text(json.dumps(manifest))
    assert workspace.plan() == []


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
    reports = []

    async def report(job, detail, stage):
        reports.append((job.service, detail))
    manager.failure_handler = report
    manager.reports = reports
    monkeypatch.setattr(process_module, 'processes', manager)
    return manager


def script(path, body):
    path.write_text(f'#!{sys.executable}\n{body}')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


async def start_in_snapshot(manager, snapshot, project_root, port):
    tokens = active_workspace.set(snapshot), source_workspace.set(project_root)
    try:
        job = await manager.start(['./serve', str(port)], 'site', 120, 'web', network=True, keep=True)
        assert await manager.wait_ready(job, port, 10)
        job.service, job.started_by = 'web', 'backend'
        return job
    finally:
        source_workspace.reset(tokens[1])
        active_workspace.reset(tokens[0])


SERVE = 'import sys, http.server\nhttp.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port=int(sys.argv[1]), bind="127.0.0.1")\n'


def test_a_restart_that_cannot_start_keeps_the_old_copy_serving(tmp_path, manager):
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'site').mkdir(parents=True)
    (project_root / 'site').mkdir(parents=True)
    script(snapshot / 'site' / 'serve', SERVE)  # only in the snapshot, e.g. a file never merged
    port = free_port()

    async def scenario():
        job = await start_in_snapshot(manager, snapshot, project_root, port)
        try:
            assert await manager.relaunch_delivered(snapshot, project_root) == []
            assert job.process.returncode is None and job.relaunch == 'failed'
            [(name, detail)] = manager.reports
            assert name == 'web' and '找不到启动命令 ./serve' in detail
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_restart_that_does_not_serve_brings_back_the_delivered_copy(tmp_path, manager):
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'site').mkdir(parents=True)
    (project_root / 'site').mkdir(parents=True)
    script(snapshot / 'site' / 'serve', SERVE)
    script(project_root / 'site' / 'serve', 'import sys\nprint("ImportError: no module named flask")\nsys.exit(1)\n')
    port = free_port()

    async def scenario():
        job = await start_in_snapshot(manager, snapshot, project_root, port)
        try:
            [restored] = await manager.relaunch_delivered(snapshot, project_root)
            assert job.process.returncode is not None
            assert restored.process.returncode is None and restored.relaunch == 'failed' and restored.from_snapshot
            assert restored.ports == {port} and restored.service == 'web'
            [(name, detail)] = manager.reports
            assert '服务仍由任务快照里的交付前副本提供' in detail and 'no module named flask' in detail
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_dependencies_are_installed_at_the_root_before_the_restart(tmp_path, manager, monkeypatch):
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'site' / 'node_modules').mkdir(parents=True)
    (project_root / 'site').mkdir(parents=True)
    for folder in (snapshot / 'site', project_root / 'site'):
        script(folder / 'serve', SERVE)
        (folder / 'package.json').write_text('{"name": "site"}')
    installs = []
    original = manager.run

    async def run(argv, cwd, *args, **kwargs):
        if argv[0] in {'npm', 'pnpm', 'yarn', 'bun'}:
            installs.append((argv, cwd, kwargs.get('network')))
            (project_root / 'site' / 'node_modules').mkdir()
            return {'exit_code': 0, 'timed_out': False, 'output': ''}
        return await original(argv, cwd, *args, **kwargs)
    monkeypatch.setattr(manager, 'run', run)
    port = free_port()

    async def scenario():
        await start_in_snapshot(manager, snapshot, project_root, port)
        try:
            [moved] = await manager.relaunch_delivered(snapshot, project_root)
            assert installs == [(['npm', 'install'], 'site', True)]
            assert not moved.from_snapshot and moved.ports == {port} and manager.reports == []
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv('WDRUNGE_TEST_MYSQL'), reason='set WDRUNGE_TEST_MYSQL=root:password@127.0.0.1:3306')
def test_provisioning_against_a_real_mysql(tmp_path):
    credentials, _, location = os.environ['WDRUNGE_TEST_MYSQL'].rpartition('@')
    admin, _, password = credentials.partition(':')
    host, _, port = location.partition(':')

    async def run():
        import pymysql
        store, resources, project, workspace = await setup(tmp_path, 'Resource Test')
        await resources.save(ConnectionInput(name='mysql', type='mysql', host=host, port=int(port or 3306), username=admin, password=password))
        provided = await resources.provided(workspace)
        env = provided.env
        assert env['DB_ENGINE'] == 'mysql', await resources.project_status(project['id'])
        connection = pymysql.connect(host=host, port=int(port or 3306), user=env['DB_USER'], password=env['DB_PASSWORD'], database=env['DB_NAME'])
        try:
            with connection.cursor() as cursor:
                cursor.execute('CREATE TABLE IF NOT EXISTS probe (id INT PRIMARY KEY)')
                with pytest.raises(pymysql.err.OperationalError):
                    cursor.execute('SELECT * FROM mysql.user')  # no access beyond its own database
        finally:
            connection.close()
        admin_connection = pymysql.connect(host=host, port=int(port or 3306), user=admin, password=password, autocommit=True)
        try:
            with admin_connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{env['DB_NAME']}`")
                cursor.execute(f"DROP USER IF EXISTS '{env['DB_USER']}'@'%'")
        finally:
            admin_connection.close()
        await store.close()
    asyncio.run(run())


def test_sqlite_backups_restore_and_keep_the_newest(tmp_path):
    import sqlite3
    from app.resources import backup
    data = tmp_path / '.wdrunge-data'
    data.mkdir()
    live = data / 'app.db'
    with sqlite3.connect(live) as db:
        db.execute('CREATE TABLE posts (title TEXT)')
        db.execute("INSERT INTO posts VALUES ('first')")

    async def run():
        made = await backup.backup(data)
        with sqlite3.connect(live) as db:
            db.execute('DROP TABLE posts')
            db.execute('CREATE TABLE broken (x)')
        await backup.restore(data, made['name'])
        with sqlite3.connect(live) as db:
            assert db.execute('SELECT title FROM posts').fetchall() == [('first',)]
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='broken'").fetchall()
        for _ in range(6):
            await backup.backup(data)
        assert len(backup.list_backups(data)) == backup.KEEP
        with pytest.raises(ValueError):
            await backup.restore(data, '../app.db')
        assert await backup.backup(tmp_path / 'empty') is None
    asyncio.run(run())


def migration_setup(tmp_path, monkeypatch):
    class Approve:
        store = SimpleNamespace(sqlite_path=None)

        def __init__(self):
            self.calls = []

        async def authorize(self, operation, **kwargs):
            self.calls.append((operation['argv'], kwargs.get('platform')))
    monkeypatch.setattr(policy, 'current', Approve())
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': True, 'backend': 'seatbelt'})
    monkeypatch.setattr(sandbox, 'command_line', lambda argv, *args, **kwargs: argv)
    manager = Processes()
    monkeypatch.setattr(process_module, 'processes', manager)
    return manager


def test_a_merged_migration_runs_at_the_root_after_a_backup(tmp_path, monkeypatch):
    import sqlite3
    manager = migration_setup(tmp_path, monkeypatch)

    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        monkeypatch.setattr(service, 'current', resources)
        collaboration = Collaboration(store)
        await collaboration.initialize()
        p = project['id']
        (workspace / 'backend').mkdir()
        (workspace / 'backend' / 'migrate.py').write_text(
            'import os, sqlite3\nsqlite3.connect(os.environ["SQLITE_PATH"]).execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER)")\n')
        resources.data_dir(workspace)
        sqlite3.connect(workspace / '.wdrunge-data' / 'app.db').execute('CREATE TABLE old (x)').connection.commit()
        await collaboration.register_command(p, 'backend', 'migrate', [sys.executable, 'migrate.py'], 'backend', 'apply schema migrations')
        assert await collaboration.migration_for(p, ['frontend/app.ts']) is None
        command = await collaboration.migration_for(p, ['backend/models.py'])
        assert command['name'] == 'migrate'
        assert await collaboration.apply_migration(p, 'backend:1', workspace, workspace, command)
        with sqlite3.connect(workspace / '.wdrunge-data' / 'app.db') as db:
            assert {row[0] for row in db.execute("SELECT name FROM sqlite_master")} == {'old', 'posts'}
        assert len((await resources.project_status(p))['backups']) == 1
        assert (await collaboration.environment(p))['commands'][0]['status'] == 'verified'

        (workspace / 'backend' / 'migrate.py').write_text('raise SystemExit("column title already exists")\n')
        command = await collaboration.migration_for(p, ['backend/models.py'])
        assert not await collaboration.apply_migration(p, 'backend:2', workspace, workspace, command, 'conv')
        async with collaboration.reading() as c:
            from sqlalchemy import text
            [event] = [json.loads(row) for row in await c.scalars(text("SELECT data FROM collaboration_events WHERE recipient='leader'"))]
        assert event['type'] == 'migration_failed' and 'column title already exists' in event['summary'] and '已备份' in event['summary']
        assert event['cause'] == '命令退出码 1' and event['cwd'] == str(workspace.resolve() / 'backend')
        # Once verified in a task, the platform replays the command without a new review.
        assert policy.current.calls[0][1] is None and policy.current.calls[-1][1]
        await manager.close()
        await store.close()
    asyncio.run(run())


def test_services_stay_on_their_code_when_the_migration_fails(tmp_path, manager):
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'site').mkdir(parents=True)
    (project_root / 'site').mkdir(parents=True)
    for folder in (snapshot / 'site', project_root / 'site'):
        script(folder / 'serve', SERVE)
    port = free_port()

    async def scenario():
        job = await start_in_snapshot(manager, snapshot, project_root, port)

        async def failed():
            return False
        try:
            assert await manager.relaunch_delivered(snapshot, project_root, before=failed()) == []
            assert job.process.returncode is None and job.relaunch == 'failed'
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_pasted_connection_urls_are_saved_and_hidden_from_the_team(tmp_path):
    from app.resources import capture

    async def run():
        store, resources, project, _ = await setup(tmp_path)
        text = '数据库用 mysql://root:S3cr%40t!@127.0.0.1:3306 ，缓存 redis://:cachepw@localhost:6380/0 。示例 mysql://u:***@h/db'
        masked, names = await capture.capture(resources, project['id'], text)
        assert names == ['mysql', 'redis']
        assert 'S3cr' not in masked and 'cachepw' not in masked
        assert 'mysql://root:***@127.0.0.1:3306' in masked and 'redis://:***@localhost:6380/0' in masked
        assert 'mysql://u:***@h/db' in masked and '已把连接 mysql、redis 的密码保存' in masked
        rows = {row['name']: row for row in await resources._rows()}
        assert rows['mysql']['secrets']['password'] == 'S3cr@t!' and rows['mysql']['data']['provision']
        assert rows['mysql']['data']['projects'] == [project['id']]
        assert rows['redis']['data']['port'] == 6380 and not rows['redis']['data']['provision']
        # Pasting a new password for the same account updates that connection.
        masked, names = await capture.capture(resources, project['id'], 'mysql://root:n3w-pass@127.0.0.1:3306')
        assert names == ['mysql'] and len(await resources._rows()) == 2
        assert {row['name']: row for row in await resources._rows()}['mysql']['secrets']['password'] == 'n3w-pass'
        # Ordinary text is untouched.
        assert await capture.capture(resources, project['id'], '把 http://user:pw@example.com 加到文档里') == (
            '把 http://user:pw@example.com 加到文档里', [])
        await store.close()
    asyncio.run(run())


def test_postgresql_urls_use_the_psycopg_driver(tmp_path, provisioned):
    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        await resources.save(ConnectionInput(name='pg', type='postgresql', username='postgres', password='admin-pass'))
        env = (await resources.provided(workspace)).env
        assert env['DB_ENGINE'] == 'postgresql' and env['SQLALCHEMY_DATABASE_URL'].startswith('postgresql+psycopg://')
        assert env['DATABASE_URL'].startswith('postgresql://') and env['DB_PORT'] == '5432'
        await store.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.getenv('WDRUNGE_TEST_POSTGRES'), reason='set WDRUNGE_TEST_POSTGRES=postgres:password@127.0.0.1:5432')
def test_provisioning_against_a_real_postgresql(tmp_path):
    credentials, _, location = os.environ['WDRUNGE_TEST_POSTGRES'].rpartition('@')
    admin, _, password = credentials.partition(':')
    host, _, port = location.partition(':')

    async def run():
        import psycopg
        store, resources, project, workspace = await setup(tmp_path, 'Resource Test')
        await resources.save(ConnectionInput(name='pg', type='postgresql', host=host, port=int(port or 5432), username=admin, password=password))
        env = (await resources.provided(workspace)).env
        assert env['DB_ENGINE'] == 'postgresql', await resources.project_status(project['id'])
        with psycopg.connect(host=host, port=int(port or 5432), user=env['DB_USER'], password=env['DB_PASSWORD'], dbname=env['DB_NAME'],
                             autocommit=True) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS probe (id INT PRIMARY KEY)')
        with psycopg.connect(host=host, port=int(port or 5432), user=admin, password=password, dbname='postgres', autocommit=True) as connection:
            connection.execute(f"DROP DATABASE IF EXISTS {env['DB_NAME']}")
            connection.execute(f"DROP ROLE IF EXISTS {env['DB_USER']}")
        await store.close()
    asyncio.run(run())


@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
def test_the_desktop_app_may_edit_and_delete_connections_cross_origin(method):
    """Editing (PUT) and deleting (DELETE) a connection come from the app's own origin:
    the CORS preflight must allow them, or WebKit fails the request with 'Load failed'."""
    from app import main
    response = TestClient(main.app).options('/api/resources/abc', headers={
        'Origin': 'tauri://localhost', 'Access-Control-Request-Method': method,
        'Access-Control-Request-Headers': 'authorization,content-type'})
    assert response.status_code == 200 and method in response.headers['access-control-allow-methods']


def test_mysql_provisioning_statements_survive_parameter_formatting(monkeypatch):
    """PyMySQL %-formats a statement that has parameters: the account host '%' must not
    be taken for a placeholder ("unsupported format character")."""
    import pymysql
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, args=None):
            # What pymysql.cursors.Cursor.execute does with the escaped arguments.
            executed.append(query % tuple(f"'{arg}'" for arg in args) if args is not None else query)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: Connection())
    asyncio.run(kinds.provision('mysql', '127.0.0.1', 3306, 'root', 'admin', 'wd_blog_abc123', 'wd_blog_abc123', 'p@ss%word'))
    assert "CREATE USER IF NOT EXISTS 'wd_blog_abc123'@'%' IDENTIFIED BY 'p@ss%word'" in executed
    assert "ALTER USER 'wd_blog_abc123'@'%' IDENTIFIED BY 'p@ss%word'" in executed
    assert "GRANT ALL PRIVILEGES ON `wd_blog_abc123`.* TO 'wd_blog_abc123'@'%'" in executed


def test_a_migration_that_never_ran_reports_the_cause(tmp_path, monkeypatch):
    manager = migration_setup(tmp_path, monkeypatch)

    async def expired(operation, **kwargs):
        raise PermissionError('等待人工审批超时（10 分钟内无人处理），操作没有执行。')
    monkeypatch.setattr(policy.current, 'authorize', expired)

    async def run():
        store, resources, project, workspace = await setup(tmp_path)
        collaboration = Collaboration(store)
        await collaboration.initialize()
        p = project['id']
        (workspace / 'backend').mkdir()
        (workspace / 'backend' / 'migrate.py').write_text('pass\n')
        command = await collaboration.register_command(p, 'backend', 'migrate', [sys.executable, 'migrate.py'], 'backend')
        assert not await collaboration.apply_migration(p, 'backend:1', workspace, workspace, command, 'conv')
        async with collaboration.reading() as c:
            from sqlalchemy import text
            [event] = [json.loads(row) for row in await c.scalars(text("SELECT data FROM collaboration_events WHERE recipient='leader'"))]
        assert '等待人工审批超时' in event['cause'] and '等待人工审批超时' in event['summary']
        await manager.close()
        await store.close()
    asyncio.run(run())


def test_a_command_with_its_own_interpreter_needs_no_root_venv(tmp_path, manager, monkeypatch):
    project_root = tmp_path / 'project'
    snapshot = project_root / '.wdrunge-runs' / 'backend' / 'work'
    (snapshot / 'api' / '.venv').mkdir(parents=True)
    (project_root / 'api').mkdir(parents=True)
    (project_root / 'api' / 'requirements.txt').write_text('fastapi\n')
    installs = []

    async def run(argv, cwd, *args, **kwargs):
        installs.append(argv)
        return {'exit_code': 0, 'timed_out': False, 'output': ''}
    monkeypatch.setattr(manager, 'run', run)
    outside = Path(getattr(sys, '_base_executable', sys.executable))
    job = SimpleNamespace(argv=[str(outside), 'migrate.py'], cwd=str(snapshot / 'api'))
    assert asyncio.run(manager.prepare_root(job, project_root, 'api')) is None
    assert installs == []
    # A bare interpreter name runs through the project .venv, so that one is installed.
    job = SimpleNamespace(argv=['python', 'migrate.py'], cwd=str(snapshot / 'api'))
    asyncio.run(manager.prepare_root(job, project_root, 'api'))
    assert installs and installs[0][1:3] == ['-m', 'venv']
