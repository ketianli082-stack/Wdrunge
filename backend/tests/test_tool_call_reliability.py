"""Regression tests for tool-call failures observed in a real Hello World run.

Each test names the failure it prevents: false approval prompts, sandbox network
surprises, services killed or unready, expected outcomes reported as failures, and
recoverable model slips (process IDs, BSD vs GNU utilities)."""
import asyncio
from collections import deque
import shutil
import socket
import sys
from types import SimpleNamespace

import pytest

from app.agents.tools import development
from app.agents.tools._workspace import active_workspace, source_workspace
from app.agents.tools.validation import ToolFailure, tool_result_failed
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.permissions import argv_ports, connection_texts, network_targets, operation_facts, task_services
from app.execution.processes import Processes
from app.storage import ChatStore


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def service(workspace, name, argv, returncode=None, log=b''):
    return SimpleNamespace(scope=str(workspace), workspace=str(workspace), name=name, argv=argv,
                           process=SimpleNamespace(returncode=returncode), logs=deque([log]), ports=set())


# --- approval false positives -------------------------------------------------

def test_request_header_values_are_not_network_targets():
    curl = ['curl', '-sS', '-i', 'http://127.0.0.1:8765/api/hello', '-H', 'Origin: http://localhost:5173',
            '--header=Referer: http://localhost:4000/', '-HOrigin: http://localhost:3000', '-e', 'http://localhost:9000/']
    assert [t['port'] for t in network_targets(connection_texts(curl))] == [8765]
    # Only HTTP clients get header parsing; a script's URL is still a target.
    node = ['node', '-e', "fetch('http://localhost:9999/')"]
    assert [t['port'] for t in network_targets(connection_texts(node))] == [9999]


def test_static_server_positional_port_is_recognized():
    assert argv_ports(['python3', '-m', 'http.server', '5173']) == {5173}
    assert argv_ports(['python3', '-m', 'http.server', '--bind', '127.0.0.1', '5173']) == {5173}
    assert argv_ports(['python3', '-m', 'http.server', '-d', 'frontend', '8080']) == {8080}
    assert argv_ports(['python3', 'backend/app.py', '--port', '8765']) == {8765}


def test_task_services_remember_stopped_ports_and_list_running_first(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    jobs = {'old': service(workspace, 'backend', ['python3', 'app.py', '--port', '8765'], returncode=-9),
            'tool': service(workspace, 'check', ['curl', '--version'], returncode=0),
            'web': service(workspace, 'frontend', ['python3', '-m', 'http.server', '5173'])}
    jobs['ready'] = service(workspace, 'api', ['python3', 'app.py'])
    jobs['ready'].ports = {9000}
    monkeypatch.setattr(process_module.processes, 'jobs', jobs)
    services = task_services(str(workspace), str(workspace))
    assert [(s['name'], s['ports'], s['running']) for s in services] == [
        ('api', [9000], True), ('frontend', [5173], True), ('backend', [8765], False)]
    facts = operation_facts({'workspace': str(workspace), 'argv': ['curl', 'http://127.0.0.1:8765/']}, services)
    assert facts['network_targets'][0]['served_by'] == 'backend'


def test_own_services_with_origin_headers_do_not_reach_a_human(tmp_path, monkeypatch):
    async def review(details):
        return policy.Verdict(decision='allow', reason='验证本项目服务', confidence=.95, uncertain=False)
    monkeypatch.setattr(policy, 'review', review)
    workspace = tmp_path.resolve()
    monkeypatch.setattr(process_module.processes, 'jobs', {
        'web': service(workspace, 'frontend', ['python3', '-m', 'http.server', '--bind', '127.0.0.1', '5173']),
        'api': service(workspace, 'backend', ['python3', 'backend/app.py'], returncode=-9,
                       log=b'[hello-api] GET http://127.0.0.1:8765/api/hello\n')})
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/own.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(workspace)
        try:
            preflight = ['curl', '-i', '-X', 'OPTIONS', 'http://127.0.0.1:8765/api/hello',
                         '-H', 'Origin: http://localhost:5173', '-H', 'Access-Control-Request-Method: GET']
            await asyncio.wait_for(manager.authorize({'kind': 'command', 'argv': preflight, 'network': True}), 2)
            page = ['python3', '-I', 'runner.py', '{"url": "http://localhost:5173/", "allowed_origins": ["http://127.0.0.1:8765"]}']
            await asyncio.wait_for(manager.authorize({'kind': 'command', 'argv': page, 'network': True}), 2)
            assert await manager.rows('approval') == []
            # A genuinely foreign port still needs a human, and the reason names only it.
            foreign = ['curl', 'http://127.0.0.1:8000/', '-H', 'Origin: http://localhost:5173']
            task = asyncio.create_task(manager.authorize({'kind': 'command', 'argv': foreign, 'network': True}))
            for _ in range(200):
                rows = [r for r in await manager.rows('approval') if r['state'] == 'pending']
                if rows:
                    break
                await asyncio.sleep(.005)
            assert '[8000]' in rows[0]['reason'] and '5173' not in rows[0]['reason']
            await manager.decide(rows[0]['id'], 'deny')
            with pytest.raises(PermissionError):
                await asyncio.wait_for(task, 2)
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(run())


def test_reviewer_retries_one_transient_failure(monkeypatch):
    calls = []
    class FlakyModel:
        def __init__(self, **kwargs):
            pass
        async def ainvoke(self, messages):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError('slow reviewer')
            return SimpleNamespace(content='{"decision":"allow","reason":"只读检查","confidence":0.9,"uncertain":false}')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-only')
    monkeypatch.setattr(policy, 'ChatOpenAI', FlakyModel)
    assert asyncio.run(policy.review({})).decision == 'allow' and len(calls) == 2
    monkeypatch.delenv('DASHSCOPE_API_KEY')
    calls.clear()
    assert asyncio.run(policy.review({})).decision == 'ask' and not calls


def test_reviewer_sees_accepted_decisions(tmp_path):
    from app.collaboration.service import Collaboration
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/decisions.db')
        project = (await store.create_project('Decisions', str(tmp_path)))['id']
        service_api = Collaboration(store)
        await service_api.initialize()
        document = (await service_api.publish(project, 'backend', [{'path': 'docs/api.md', 'base_version': 0, 'summary': 'API', 'content': '# API\n'}]))[0]
        links = [{'document_id': document['document_id'], 'version': 1}]
        await service_api.record_decision(project, 'leader', 'API uses port 8765', '8000 is taken', links, {'port': '8765'}, state='accepted')
        await service_api.record_decision(project, 'leader', 'Draft idea', 'Not agreed', links, {'port': '9000'})
        ctx = SimpleNamespace(service=service_api, project=project)
        assert await policy.Permissions.project_decisions(ctx) == [{'title': 'API uses port 8765', 'facts': {'port': '8765'}}]
        assert await policy.Permissions.project_decisions(None) == []
        await store.close()
    asyncio.run(run())


# --- execute_command / manage_process ------------------------------------------

def test_network_is_automatic_for_services_and_loopback_checks():
    assert development.automatic_network(['python3', 'backend/app.py'], True)
    assert development.automatic_network(['curl', '-H', 'Origin: http://localhost:5173', 'http://127.0.0.1:8765/'], False)
    assert not development.automatic_network(['curl', 'https://unpkg.com/vue'], False)
    assert not development.automatic_network(['curl', 'http://127.0.0.1:8765/', 'https://example.com/'], False)
    assert not development.automatic_network(['python3', '--version'], False)


def test_background_defaults_and_explicit_network_choice(monkeypatch):
    calls = []
    async def start(argv, cwd, timeout, name, network, reason, keep=False):
        calls.append((timeout, network))
        return SimpleNamespace(snapshot=lambda: {'id': 'svc'}, watcher=asyncio.get_running_loop().create_future(),
                               process=SimpleNamespace(returncode=None), timed_out=False, ports=set())
    async def run(argv, cwd, timeout, **kwargs):
        calls.append((timeout, kwargs['network']))
        return {'exit_code': 0, 'output': ''}
    monkeypatch.setattr(development.processes, 'start', start)
    monkeypatch.setattr(development.processes, 'run', run)
    monkeypatch.setattr(development.processes, 'logs', lambda job, offset=0, limit=0: {'output': ''})
    monkeypatch.setattr(development, 'STARTUP_GRACE', .01)
    async def scenario():
        await development.execute_command.ainvoke({'command': 'npm', 'arguments': ['run', 'dev'], 'background': True})
        await development.execute_command.ainvoke({'command': 'npm', 'arguments': ['run', 'dev'], 'background': True, 'network': False, 'timeout': 60})
        await development.execute_command.ainvoke({'command': 'curl', 'arguments': ['http://localhost:5173/']})
        await development.execute_command.ainvoke({'command': 'pytest', 'arguments': []})
    asyncio.run(scenario())
    assert calls == [(1800, True), (60, False), (120, True), (120, False)]


@pytest.fixture
def real_processes(tmp_path, monkeypatch):
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


def in_workspace(tmp_path, manager, body):
    async def run():
        tokens = active_workspace.set(tmp_path), source_workspace.set(tmp_path)
        try:
            await body()
        finally:
            await manager.close()
            source_workspace.reset(tokens[1])
            active_workspace.reset(tokens[0])
    asyncio.run(run())


def test_expected_exit_codes_are_not_failures(tmp_path, real_processes):
    async def body():
        probe = {'command': sys.executable, 'arguments': ['-c', 'raise SystemExit(7)']}
        failed = await development.execute_command.ainvoke(probe)
        assert tool_result_failed(failed) and failed['exit_code'] == 7
        expected = await development.execute_command.ainvoke({**probe, 'ok_exit_codes': [7]})
        assert not tool_result_failed(expected) and expected['exit_code'] == 7
        # A probe expecting a closed port (7) still succeeds when the port answers (0).
        answered = await development.execute_command.ainvoke(
            {'command': sys.executable, 'arguments': ['-c', 'pass'], 'ok_exit_codes': [7]})
        assert not tool_result_failed(answered) and answered['exit_code'] == 0
    in_workspace(tmp_path, real_processes, body)


def test_background_crash_during_startup_is_reported_with_its_cause(tmp_path, real_processes):
    script = "import sys; print('cannot bind 0.0.0.0:8765: [Errno 1] Operation not permitted'); sys.exit(1)"
    async def body():
        result = await development.execute_command.ainvoke(
            {'command': sys.executable, 'arguments': ['-c', script], 'background': True, 'name': 'api'})
        assert tool_result_failed(result) and result['exit_code'] == 1
        assert result['error_details']['code'] == 'os_permission_denied'
        assert 'Operation not permitted' in result['output']
    in_workspace(tmp_path, real_processes, body)


def test_ready_port_waits_for_service_and_rejects_taken_ports(tmp_path, real_processes):
    port = free_port()
    async def body():
        started = await development.execute_command.ainvoke({'command': sys.executable, 'background': True, 'name': 'web',
            'arguments': ['-m', 'http.server', '--bind', '127.0.0.1', str(port)], 'ready_port': port})
        assert started['ready'] is True and started['state'] == 'running' and 'error' not in started
        assert real_processes.jobs[started['id']].ports == {port}
        await development.execute_command.ainvoke({'command': sys.executable, 'arguments': ['-c', 'pass']})
        listed = await development.manage_process.ainvoke({'action': 'list'})
        assert listed['processes'][0]['ports'] == [port]
        assert [p['name'] for p in listed['processes']] == ['web'] and listed['finished_commands'] == 1
        quiet = await development.execute_command.ainvoke({'command': sys.executable, 'background': True,
            'arguments': ['-c', 'pass'], 'ready_port': free_port()})
        assert tool_result_failed(quiet) and quiet['error_details']['code'] == 'service_not_ready'
        with pytest.raises(ToolFailure, match=f'Port {port} already accepts connections'):
            await development.execute_command.ainvoke({'command': sys.executable, 'background': True,
                'arguments': ['-m', 'http.server', str(port)], 'ready_port': port})
    in_workspace(tmp_path, real_processes, body)


def test_processes_can_be_managed_by_name(tmp_path, real_processes):
    async def body():
        await development.execute_command.ainvoke({'command': sys.executable, 'background': True, 'name': 'api',
                                                   'arguments': ['-c', 'print("up", flush=True); import time; time.sleep(30)']})
        logs = await development.manage_process.ainvoke({'action': 'logs', 'process_id': 'api'})
        assert logs['name'] == 'api' and 'up' in logs['output']
        with pytest.raises(ValueError, match=r'13c39d.*\(api, running\)'):
            await development.manage_process.ainvoke({'action': 'status', 'process_id': '13c39d856bc8447cb7edac2131a1e5'})
        stopped = await development.manage_process.ainvoke({'action': 'stop', 'process_id': 'api'})
        assert stopped['state'] == 'exited'
    in_workspace(tmp_path, real_processes, body)


def test_environment_reports_host_command_conventions(tmp_path, monkeypatch):
    monkeypatch.setattr(development.platform, 'system', lambda: 'Darwin')
    token = active_workspace.set(tmp_path)
    try:
        host = asyncio.run(development.detect_environment.ainvoke({}))['host']
    finally:
        active_workspace.reset(token)
    assert host['userland'] == 'BSD' and any('lsof' in note for note in host['notes'])
    # An empty project still names the interpreter commands will run.
    assert host['executables']['python3'] == shutil.which('python3')


def test_browser_check_accepts_expected_error_paths():
    from app.agents.tools.browser_verify import BrowserCheck
    check = BrowserCheck.model_validate({'url': 'http://localhost:5173/', 'expect_errors': True,
                                         'actions': [{'action': 'wait_visible', 'selector': '.state.error'}]})
    assert check.expect_errors is True


def test_review_facts_mark_files_a_task_created_itself(tmp_path):
    """Deleting placeholders the task wrote is routine; deleting files the snapshot
    started with is not. The reviewer gets the fact, and still decides."""
    import json
    from app.execution.permissions import operation_facts
    work = tmp_path / '.wdrunge-runs' / 'abc' / 'work'
    (work / 'frontend' / 'assets').mkdir(parents=True)
    (work / 'docs').mkdir()
    (work.parent / 'manifest.json').write_text(json.dumps({'docs/api.md': 'h', 'frontend/index.html': 'h'}))
    facts = lambda path, **extra: operation_facts({'workspace': str(work), 'kind': 'delete', 'path': str(work / path), **extra}, [])
    assert facts('frontend/assets', recursive=True)['created_by_this_task'] is True
    assert facts('frontend/index.html')['created_by_this_task'] is False
    assert facts('frontend', recursive=True)['created_by_this_task'] is False
    # Outside a task snapshot there is no record, so nothing is claimed.
    assert 'created_by_this_task' not in operation_facts({'workspace': str(tmp_path), 'kind': 'delete', 'path': str(tmp_path / 'x')}, [])
