"""Policy tests never call paid models or weaken production policy."""
import asyncio
from pathlib import Path
import sys
import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from app.storage import ChatStore
from app.execution import permissions as policy, sandbox, api
from app.execution.processes import Processes
from app.execution.environment import installation_commands, inspect_environment
from app.agents.tools._workspace import active_workspace
from app.agents.tools.file_management import make_file_management
from app.agents.tools.browser_verify import BrowserCheck

async def pending(manager):
    for _ in range(100):
        rows = [r for r in await manager.rows('approval') if r['state'] == 'pending']
        if rows:
            return rows[0]['id']
        await asyncio.sleep(.005)
    raise AssertionError('No approval request')


def test_exact_grants_once_denial_revocation_restart_scope(tmp_path, monkeypatch):
    async def ask(details):
        return policy.Verdict(decision='ask', reason='Needs review')
    monkeypatch.setattr(policy, 'review', ask)
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/permissions.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        operation = {'kind': 'command', 'argv': ['echo', 'hello']}
        try:
            once = asyncio.create_task(manager.authorize(operation))
            identifier = await pending(manager)
            await manager.decide(identifier, 'once')
            await once
            assert not await manager.rows('grant')
            with pytest.raises(ValueError):
                await manager.decide(identifier, 'always')
            remember = asyncio.create_task(manager.authorize(operation))
            await manager.decide(await pending(manager), 'always')
            await remember
            restarted = policy.Permissions(store)
            await restarted.initialize()
            await asyncio.wait_for(restarted.authorize(operation), .5)
            changed = asyncio.create_task(restarted.authorize({**operation, 'argv': ['echo', 'other']}))
            await restarted.decide(await pending(restarted), 'deny')
            with pytest.raises(PermissionError):
                await changed
            active_workspace.set(tmp_path/'other')
            other = asyncio.create_task(restarted.authorize(operation))
            await restarted.decide(await pending(restarted), 'deny')
            with pytest.raises(PermissionError):
                await other
            await restarted.revoke((await restarted.rows('grant'))[0]['id'])
            assert not await restarted.rows('grant')
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(scenario())


def test_platform_operations_skip_review_and_expired_approvals_say_why(tmp_path, monkeypatch):
    reviewed = []
    async def ask(details):
        reviewed.append(details)
        return policy.Verdict(decision='ask', reason='Needs review')
    monkeypatch.setattr(policy, 'review', ask)
    monkeypatch.setattr(policy, 'APPROVAL_SECONDS', .2)
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/permissions.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        token = active_workspace.set(tmp_path)
        try:
            install = {'kind': 'command', 'argv': ['python3', '-m', 'venv', str(tmp_path / '.venv')], 'network': True}
            await asyncio.wait_for(manager.authorize(install, platform='install from manifests'), 2)
            [audit] = await manager.rows('review')
            assert reviewed == [] and audit['source'] == 'platform'
            assert not [r for r in await manager.rows('approval')]
            # Nobody answers: the error names the cause instead of an empty TimeoutError.
            with pytest.raises(PermissionError, match='等待人工审批超时'):
                await manager.authorize({'kind': 'command', 'argv': ['echo', 'agent']})
            assert (await manager.rows('approval'))[0]['state'] == 'expired'
        finally:
            active_workspace.reset(token)
            await store.close()
    asyncio.run(scenario())


def test_cancelled_approval_cannot_be_accepted(tmp_path):
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/permissions.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        task = asyncio.create_task(manager.authorize({'kind': 'delete', 'path': 'x'}, human=True))
        identifier = await pending(manager)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError):
            await manager.decide(identifier, 'always')
        assert (await manager.rows('approval'))[0]['state'] == 'expired'
        await store.close()
    asyncio.run(scenario())


def test_model_failure_requests_human(monkeypatch):
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    assert asyncio.run(policy.review({'kind': 'command'})).decision == 'ask'


def test_control_api_requires_private_ui_token(tmp_path, monkeypatch):
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/api.db')
        await store.initialize()
        manager = policy.Permissions(store)
        await manager.initialize()
        monkeypatch.setattr(policy, 'current', manager)
        app = FastAPI()
        app.include_router(api.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/execution/permissions')).status_code == 401
            assert (await client.post('/api/execution/permissions/fake', json={'decision': 'once'}, headers={'Origin': 'http://localhost:1420'})).status_code == 401
            headers = {'Authorization': f'Bearer {api.control_token}'}
            assert (await client.get('/api/execution/permissions', headers=headers)).status_code == 200
            assert (await client.post('/api/execution/permissions/fake', json={'decision': 'once'}, headers=headers)).status_code == 409
            assert (await client.post('/api/execution/permissions/fake', json={'decision': 'all'}, headers=headers)).status_code == 422
        await store.close()
    asyncio.run(scenario())


def test_environment_installs_are_local_and_shared_links_refused(tmp_path):
    (tmp_path/'package.json').write_text('{"scripts":{"dev":"vite"}}')
    (tmp_path/'package-lock.json').write_text('{}')
    assert installation_commands(tmp_path, 'node') == [['npm', 'ci']]
    assert inspect_environment(tmp_path)['runtimes'][0]['manager'] == 'npm'
    shared = tmp_path/'shared'
    shared.mkdir()
    (tmp_path/'node_modules').symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError, match='依赖目录'):
        installation_commands(tmp_path, 'node')
    (tmp_path/'requirements.txt').write_text('fastapi')
    commands = installation_commands(tmp_path, 'python')
    assert commands[0][-1] == str(tmp_path/'.venv')
    # The platform's own virtual environment never becomes the project's base Python.
    assert not Path(commands[0][0]).resolve().is_relative_to(Path(sys.prefix).resolve()) or sys.prefix == sys.base_prefix
    assert commands[1][0] == str(tmp_path/'.venv/bin/python')
    assert '--user' not in commands[1]


def test_file_search_paging_and_external_read_approval(tmp_path, monkeypatch):
    root = tmp_path/'project'
    root.mkdir()
    (root/'a.py').write_text('needle 1\nneedle 2\nneedle 3\n')
    (root/'.env').write_text('needle SECRET')
    (root/'node_modules').mkdir()
    (root/'node_modules'/'a').write_text('needle dependency')
    external = tmp_path/'config.ini'
    external.write_text('config value')
    (root/'linked').symlink_to(external)
    calls = []
    class Human:
        async def authorize(self, details, **kwargs):
            calls.append((details, kwargs))
    monkeypatch.setattr(policy, 'current', Human())
    token = active_workspace.set(root)
    try:
        browse, search, read, manage = make_file_management(('.',), ('.',))
        assert browse.invoke({'limit': 1})['entries'] == [{'name': 'a.py', 'type': 'file'}]
        page = search.invoke({'query': 'needle', 'limit': 2})
        assert page['next_offset'] == 2 and len(page['matches']) == 2
        assert search.invoke({'query': 'needle', 'offset': 2})['matches'][0]['line'] == 3
        result = asyncio.run(read.ainvoke({'path': str(external), 'reason': 'Inspect config'}))
        assert result['text'] == 'config value' and calls[-1][0]['kind'] == 'read_external_file'
        assert not calls[-1][1].get('human')
        with pytest.raises(ValueError):
            asyncio.run(manage.ainvoke({'action': 'delete', 'path': '.'}))
    finally:
        active_workspace.reset(token)


def test_sandbox_unavailable_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': False, 'detail': 'unavailable'})
    async def forbidden(*args, **kwargs):
        raise AssertionError('Must not spawn')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            with pytest.raises(sandbox.SandboxUnavailable):
                await Processes().run(['echo', 'hello'])
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())


def test_process_output_timeout_concurrency_scope_cleanup(tmp_path, monkeypatch):
    class Allow:
        async def authorize(self, *args, **kwargs):
            pass
    # Test-only transport: production has no unsandboxed switch.
    monkeypatch.setattr(policy, 'current', Allow())
    monkeypatch.setattr(sandbox, 'capability', lambda: {'available': True})
    monkeypatch.setattr(sandbox, 'command_line', lambda argv, *args, **kwargs: argv)
    async def scenario():
        token = active_workspace.set(tmp_path)
        manager = Processes()
        try:
            result = await manager.run([sys.executable, '-c', 'print("out");raise SystemExit(7)'])
            assert result['exit_code'] == 7 and 'out' in result['output'] and result['error']
            long = await manager.run([sys.executable, '-c', 'print("a" * 200000)'])
            assert len(long['output']) <= 30000
            a = await manager.start([sys.executable, '-c', 'import time;time.sleep(20)'], name='frontend')
            b = await manager.start([sys.executable, '-c', 'import time;time.sleep(20)'], name='backend')
            assert a.process.returncode is None and b.process.returncode is None
            active_workspace.set(tmp_path/'other')
            with pytest.raises(ValueError):
                manager.owned(a.id)
            active_workspace.set(tmp_path)
            await manager.stop(a)
            assert b.process.returncode is None
            timed = await manager.run([sys.executable, '-c', 'import time;print("before",flush=True);time.sleep(20)'], timeout=1)
            assert timed['timed_out'] and 'before' in timed['output']
            await manager.close()
            assert b.process.returncode is not None
        finally:
            await manager.close()
            active_workspace.reset(token)
    asyncio.run(scenario())


def test_browser_input_validation():
    for payload in [
        {'url': 'file:///etc/passwd'},
        {'url': 'http://localhost:3000', 'actions': [{'action': 'evaluate', 'selector': 'body'}]},
        {'url': 'http://localhost:3000', 'allowed_origins': ['http://localhost/private']},
    ]:
        with pytest.raises(ValueError):
            BrowserCheck.model_validate(payload)


def test_directory_move_never_replaces_existing_target(tmp_path):
    from app.execution.file_ops import move_no_replace
    source = tmp_path/'source'
    source.mkdir()
    (source/'evidence.txt').write_text('keep')
    destination = tmp_path/'destination'
    destination.mkdir()
    with pytest.raises(OSError):
        move_no_replace(source, destination)
    assert (source/'evidence.txt').read_text() == 'keep'
    destination.rmdir()
    move_no_replace(source, destination)
    assert not source.exists() and (destination/'evidence.txt').read_text() == 'keep'


def test_protected_symlink_and_parent_swap_cannot_bypass_file_boundary(tmp_path):
    from app.execution.file_ops import read_regular
    from app.agents.tools._workspace import resolve_allowed
    secret = tmp_path/'.env'
    secret.write_text('secret')
    (tmp_path/'innocent.txt').symlink_to(secret)
    token = active_workspace.set(tmp_path)
    try:
        with pytest.raises(ValueError):
            resolve_allowed('innocent.txt', ('.',))
    finally:
        active_workspace.reset(token)
    original = tmp_path/'original'
    original.mkdir()
    (original/'value.txt').write_text('approved')
    approved = original/'value.txt'
    original.rename(tmp_path/'moved')
    original.symlink_to(tmp_path/'moved', target_is_directory=True)
    with pytest.raises(OSError):
        read_regular(approved, 100)


def test_child_environment_does_not_inherit_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'must-not-reach-child')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'must-not-reach-child')
    monkeypatch.setenv('PYTHONPATH', '/untrusted')
    environment = sandbox.clean_environment(tmp_path, tmp_path)
    assert all(key not in environment for key in ('DASHSCOPE_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'PYTHONPATH'))
    assert environment['HOME'] == str(tmp_path)


def test_registered_development_tool_schemas_are_strict():
    from app.agents.team import Team
    team = Team()
    for role in ('backend', 'frontend', 'testing'):
        tools = {tool.name: tool for tool in team.workers[role].spec.tools}
        assert {'execute_command', 'manage_process', 'detect_environment', 'read_external_file'} <= tools.keys()
        assert not {'start_service', 'install_dependencies'} & tools.keys()
        for tool in tools.values():
            assert tool.args_schema.model_config['extra'] == 'forbid'
    assert 'browser_verify' in {t.name for t in team.workers['testing'].spec.tools}


def test_project_parent_cannot_expose_control_plane(tmp_path, monkeypatch):
    import platformdirs
    from app.agents.tools.read_project_file import make_read_project_file
    private = tmp_path/'application-data'
    private.mkdir()
    (private/'execution-token').write_text('PRIVATE_TOKEN')
    monkeypatch.setattr(platformdirs, 'user_data_path', lambda *args: private)
    token = active_workspace.set(tmp_path)
    try:
        with pytest.raises(PermissionError):
            make_read_project_file(('.',)).invoke({'path': 'application-data/execution-token'})
        browse, search, _, manage = make_file_management(('.',), ('.',))
        assert not browse.invoke({})['entries']
        assert not search.invoke({'query': 'PRIVATE_TOKEN'})['matches']
    finally:
        active_workspace.reset(token)


def test_environment_does_not_read_manifest_symlinks(tmp_path):
    external = tmp_path/'external.json'
    external.write_text('{"scripts":{"private":"secret"}}')
    project = tmp_path/'project'
    project.mkdir()
    (project/'package.json').symlink_to(external)
    with pytest.raises(OSError):
        inspect_environment(project)


def test_failed_duplicate_start_restores_the_live_backend_token(tmp_path, monkeypatch):
    from app.execution import api
    path = tmp_path / 'app-data' / 'execution-token'
    path.parent.mkdir(parents=True)
    path.write_text('live-backend-token')
    monkeypatch.setattr(api, '_previous_token', None)
    api.write_control_token()
    assert path.read_text() == api.control_token
    # Uvicorn runs shutdown after a failed bind; the live backend's token returns.
    api.restore_control_token()
    assert path.read_text() == 'live-backend-token'
    # A newer process's token is never replaced by an exiting older process.
    api.write_control_token()
    path.write_text('newer-backend-token')
    api.restore_control_token()
    assert path.read_text() == 'newer-backend-token'


def test_commands_do_not_run_with_the_platform_virtualenv(tmp_path, monkeypatch):
    """The backend runs inside its own venv; project commands must resolve python3 from
    the host, not from Wdrunge's dependencies."""
    import sys
    platform_bin = tmp_path / 'wdrunge-venv' / 'bin'
    host_bin = tmp_path / 'host' / 'bin'
    monkeypatch.setattr(sys, 'prefix', str(platform_bin.parent))
    monkeypatch.setenv('VIRTUAL_ENV', str(platform_bin.parent))
    monkeypatch.setenv('PATH', f'{platform_bin}:{host_bin}:/usr/bin')
    assert sandbox.clean_environment(tmp_path, tmp_path)['PATH'] == f'{host_bin}:/usr/bin'
    project_venv = tmp_path / '.venv'
    (project_venv / 'bin').mkdir(parents=True)
    # A project's own .venv still comes first.
    assert sandbox.clean_environment(tmp_path, tmp_path)['PATH'] == f'{project_venv / "bin"}:{host_bin}:/usr/bin'


def test_project_venv_uses_the_task_interpreter_and_absolute_interpreters_need_none(tmp_path):
    from app.execution.environment import base_python
    from app.execution.processes import brings_own_python, snapshot_python
    base = Path(getattr(sys, '_base_executable', sys.executable))
    venv = tmp_path / 'snapshot' / '.venv'
    venv.mkdir(parents=True)
    (venv / 'pyvenv.cfg').write_text(f'home = {base.parent}\nexecutable = {base}\nversion = 3.12.4\n')
    assert snapshot_python(venv) == str(base)
    assert snapshot_python(tmp_path / 'none') is None
    assert base_python(tmp_path, str(base)) == str(base)
    project = tmp_path / 'project'
    (project / '.venv' / 'bin').mkdir(parents=True)
    # A command run by an interpreter outside the project .venv never loads that .venv.
    assert brings_own_python(str(base), project)
    assert not brings_own_python('python3', project)
    assert not brings_own_python(str(project / '.venv' / 'bin' / 'python'), project)
