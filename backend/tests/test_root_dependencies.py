"""Dependency folders at the project root and in task snapshots (Blog10): the platform
installs at the root without a live service in the folder, keeps it matching the
manifests, puts it back when an installation fails, and tells the Leader whose problem a
failure is; a snapshot gets its own writable copy."""
import asyncio
import json
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.tools._workspace import active_workspace, source_workspace
from app.agents.tools.validation import command_failure
from app.collaboration.environment import install_fingerprint
from app.collaboration.service import Collaboration
from app.collaboration.workspaces import IsolatedWorkspace
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.environment import INSTALLED, dependency_fingerprint
from app.execution.processes import PREVIOUS, Processes
from app.storage import ChatStore


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


SERVE = ('import sys, http.server\nhttp.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, '
         'port=int(sys.argv[1]), bind="127.0.0.1")\n')


def script(path, body):
    path.write_text(f'#!{sys.executable}\n{body}')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


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
    manager.reports = []

    async def report(job, detail, stage):
        manager.reports.append((job.service, stage, detail))
    manager.failure_handler = report
    monkeypatch.setattr(process_module, 'processes', manager)
    return manager


def site(tmp_path, serve=SERVE):
    """A project whose site/ has a manifest, with a task snapshot of it."""
    root = tmp_path / 'project'
    snapshot = root / '.wdrunge-runs' / 'frontend' / 'work'
    for folder in (snapshot / 'site', root / 'site'):
        folder.mkdir(parents=True)
        script(folder / 'serve', serve)
        (folder / 'package.json').write_text('{"name": "site"}')
    (snapshot / 'site' / 'node_modules').mkdir()
    return root.resolve(), snapshot.resolve()


async def start(manager, workspace, root, port, name='web'):
    tokens = active_workspace.set(workspace), source_workspace.set(root)
    try:
        job = await manager.start(['./serve', str(port)], 'site', 120, name, network=True, keep=True)
        assert await manager.wait_ready(job, port, 10)
        job.service, job.started_by = name, 'frontend'
        return job
    finally:
        source_workspace.reset(tokens[1])
        active_workspace.reset(tokens[0])


def installer(manager, monkeypatch, root, exit_code=0, during=None):
    """Stands in for the package manager: records each call and what it found."""
    calls, original = [], manager.run

    async def run(argv, cwd, *args, **kwargs):
        if argv[0] not in {'npm', 'pnpm', 'yarn', 'bun'}:
            return await original(argv, cwd, *args, **kwargs)
        target = root / cwd / 'node_modules'
        calls.append({'argv': argv, 'cwd': cwd, 'folder_present': target.exists(),
                      'running_at_root': [job.name for job in manager.jobs.values()
                                          if job.process.returncode is None and not job.from_snapshot]})
        if during:
            during()
        if exit_code == 0:
            target.mkdir(exist_ok=True)
            (target / 'installed.txt').write_text(f'install {len(calls)}')
        else:
            target.mkdir(exist_ok=True)
            (target / 'partial.txt').write_text('half an installation')
        return {'exit_code': exit_code, 'timed_out': False, 'output': 'npm error code EUSAGE' if exit_code else ''}
    monkeypatch.setattr(manager, 'run', run)
    return calls


def test_a_root_service_stops_for_the_installation_and_starts_again(tmp_path, manager, monkeypatch):
    """Blog10 04:24: `npm ci` ran beside the development server kept at the root, which
    wrote its cache into node_modules, and failed with ENOTEMPTY. The server now stops
    first, the installation starts from an empty folder, and the server comes back."""
    root, _ = site(tmp_path)
    (root / 'site' / 'node_modules').mkdir()
    (root / 'site' / 'node_modules' / 'old.txt').write_text('before the delivery')
    calls = installer(manager, monkeypatch, root)
    port, other_port = free_port(), free_port()

    async def scenario():
        web = await start(manager, root, root, port)
        neighbour = await start(manager, root, root, other_port, 'web-admin')  # same directory, not being restarted
        try:
            new = await manager.relaunch(web, root)
            assert calls == [{'argv': ['npm', 'install'], 'cwd': 'site', 'folder_present': False, 'running_at_root': []}]
            assert web.process.returncode is not None and neighbour.process.returncode is not None
            assert new.process.returncode is None and new.ports == {port} and new.service == 'web'
            again = manager.latest(neighbour)
            assert again is not neighbour and again.process.returncode is None and again.ports == {other_port}
            assert again.service == 'web-admin' and again.keep
            folder = root / 'site' / 'node_modules'
            assert (folder / 'installed.txt').exists() and not (folder / 'old.txt').exists()
            assert (folder / INSTALLED).read_text() == dependency_fingerprint(root / 'site', 'node')
            assert not (root / 'site' / PREVIOUS).exists() and manager.reports == [] and not manager.moving

            # Same manifests: the next restart installs nothing.
            newer = await manager.relaunch(new, root)
            assert len(calls) == 1 and newer.process.returncode is None
            # A manifest changed by any merge, not only the one that triggers a restart.
            (root / 'site' / 'package.json').write_text('{"name": "site", "dependencies": {"vue": "3"}}')
            assert (await manager.relaunch(newer, root)).process.returncode is None
            assert len(calls) == 2 and (folder / 'installed.txt').read_text() == 'install 2'
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_failed_installation_puts_the_folder_back_and_the_service_serves_again(tmp_path, manager, monkeypatch):
    root, _ = site(tmp_path)
    folder = root / 'site' / 'node_modules'
    folder.mkdir()
    (folder / 'old.txt').write_text('before the delivery')
    calls = installer(manager, monkeypatch, root, exit_code=1)
    port = free_port()

    async def scenario():
        web = await start(manager, root, root, port)
        try:
            assert await manager.relaunch(web, root) is None
            assert len(calls) == 2 and not any(call['folder_present'] for call in calls)  # one retry, each from empty
            assert (folder / 'old.txt').read_text() == 'before the delivery' and not (folder / 'partial.txt').exists()
            assert not (folder / INSTALLED).exists() and not (root / 'site' / PREVIOUS).exists()
            current = manager.latest(web)
            assert current is not web and current.process.returncode is None and current.ports == {port}
            [(name, stage, detail)] = manager.reports
            assert (name, stage) == ('web', 'install') and '已自动重试 1 次' in detail and 'EUSAGE' in detail
            assert '服务已在项目根目录用原来的依赖重新启动' in detail
            # Not recorded as installed: the next restart tries again.
            await manager.relaunch(current, root)
            assert len(calls) == 4
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_snapshot_copy_keeps_serving_while_the_root_cannot_install(tmp_path, manager, monkeypatch):
    root, snapshot = site(tmp_path)
    calls = installer(manager, monkeypatch, root, exit_code=1)
    port = free_port()

    async def scenario():
        job = await start(manager, snapshot, root, port)
        try:
            assert await manager.relaunch_delivered(snapshot, root) == []
            assert job.process.returncode is None and job.relaunch == 'failed' and len(calls) == 2
            assert not (root / 'site' / 'node_modules').exists()
            [(_, stage, detail)] = manager.reports
            assert stage == 'install' and '服务仍由任务快照里的交付前副本提供' in detail
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_root_copy_that_does_not_serve_gets_its_dependencies_rebuilt_once(tmp_path, manager, monkeypatch):
    """Blog10 05:05: the root folder was damaged by the failed installation, nothing
    installed it again and the service started with "vite: command not found"."""
    needs = ('import sys, os\nif not os.path.exists("node_modules/installed.txt"):\n'
             '    print("sh: vite: command not found"); sys.exit(127)\n') + SERVE
    root, snapshot = site(tmp_path, needs)
    folder = root / 'site' / 'node_modules'
    folder.mkdir()  # there, recorded as current, and without what the service needs
    (folder / INSTALLED).write_text(dependency_fingerprint(root / 'site', 'node'))
    (snapshot / 'site' / 'node_modules' / 'installed.txt').write_text('the task installed its own')
    calls = installer(manager, monkeypatch, root)
    port = free_port()

    async def scenario():
        job = await start(manager, snapshot, root, port)
        try:
            [moved] = await manager.relaunch_delivered(snapshot, root)
            assert len(calls) == 1 and not moved.from_snapshot and moved.process.returncode is None
            assert job.process.returncode is not None and manager.reports == []
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_start_that_fails_on_fresh_dependencies_is_the_projects_problem(tmp_path, manager, monkeypatch):
    """Dependencies installed from the manifests a moment ago are not installed a second
    time: the delivered copy serves again from its snapshot and the Leader hears why."""
    root, snapshot = site(tmp_path)
    script(root / 'site' / 'serve', 'import sys\nprint("ImportError: no module named flask")\nsys.exit(1)\n')
    calls = installer(manager, monkeypatch, root)
    port = free_port()

    async def scenario():
        job = await start(manager, snapshot, root, port)
        try:
            [restored] = await manager.relaunch_delivered(snapshot, root)
            assert len(calls) == 1 and job.process.returncode is not None
            assert restored.from_snapshot and restored.process.returncode is None and restored.ports == {port}
            [(_, stage, detail)] = manager.reports
            assert stage == 'start' and 'no module named flask' in detail and '重装依赖后又启动了一次' not in detail
            assert '服务仍由任务快照里的交付前副本提供' in detail
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_two_restarts_at_one_root_install_once(tmp_path, manager, monkeypatch):
    """A merge restarts every root service it touches at once; they share a folder."""
    root, _ = site(tmp_path)
    calls = installer(manager, monkeypatch, root)
    port, other_port = free_port(), free_port()

    async def scenario():
        web = await start(manager, root, root, port)
        admin = await start(manager, root, root, other_port, 'web-admin')
        (root / 'site' / 'node_modules').mkdir()
        try:
            manager.refresh_after_publish(root, root / 'elsewhere', ['site/package.json'])
            assert manager.restarting(root, name='web') and manager.restarting(root, name='web-admin')
            await asyncio.gather(*manager.tasks)
            assert len(calls) == 1
            running = sorted(job.name for job in manager.project_services(root))
            assert running == ['web', 'web-admin'] and manager.reports == [] and not manager.moving
            assert {port, other_port} == {p for job in manager.project_services(root) for p in job.ports}
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_the_leader_is_told_whose_problem_a_failed_restart_is(tmp_path):
    async def scenario():
        root = tmp_path / 'project'
        (root / 'site').mkdir(parents=True)
        (root / 'site' / 'package.json').write_text('{"name": "site"}')
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "notice.db"}')
        project = (await store.create_project('Notice', str(root)))['id']
        service = Collaboration(store)
        await service.initialize()
        await service.register_service(project, 'frontend', 'web', port=free_port(), argv=['npm', 'run', 'dev'], cwd='site')
        job = SimpleNamespace(scope=str(root), workspace=str(root), cwd=str(root / 'site'), argv=['npm', 'run', 'dev'],
                              service='web', name='web', request='r1', conversation='')

        async def notices():
            from sqlalchemy import text
            async with service.reading() as c:
                return [json.loads(row) for row in await c.scalars(text("SELECT data FROM collaboration_events WHERE recipient='leader' ORDER BY seq"))]
        await service.report_service_failure(job, 'npm error ENOTEMPTY', 'install')
        # The check before the merge installed these manifests in a clean copy.
        async with service.transaction() as c:
            record = await service._get(c, 'service', project, 'web')
            record['root_checked'] = install_fingerprint(job.argv, 'site', root)
            await service._put(c, 'service', project, 'web', record)
        await service.report_service_failure(job, 'npm error ENOTEMPTY', 'install')
        await service.report_service_failure(job, 'sh: vite: command not found', 'start')
        unchecked, checked, start = [event['summary'] for event in await notices()]
        assert '先看上面的输出再决定' in unchecked and '环境问题，平台在之后每次交付或服务重启时会再安装' in unchecked
        assert '不是交付内容的问题' in checked and '不要因此退回任务' in checked
        assert '让负责的成员修复' in start and '让负责的成员修复' not in checked
        assert all('ENOTEMPTY' in text or 'vite' in text for text in (unchecked, checked, start))
        await store.close()
    asyncio.run(scenario())


def test_a_snapshot_has_its_own_writable_copy_of_node_modules(tmp_path, monkeypatch):
    """Blog10 06:21: node_modules in the snapshot was a read-only link into the project,
    so a build could neither write its cache there nor read packages hoisted to the
    workspace root."""
    monkeypatch.setenv('WDRUNGE_RUNS_DIR', str(tmp_path / 'runs'))
    root = tmp_path / 'project'
    (root / 'node_modules' / '.bin').mkdir(parents=True)
    (root / 'node_modules' / 'vue').mkdir()
    (root / 'node_modules' / 'vue' / 'index.js').write_text('export default 1')
    (root / 'node_modules' / '.bin' / 'vue').symlink_to('../vue/index.js')
    (root / 'node_modules' / 'site').symlink_to('../frontend')  # a workspace member
    (root / 'frontend' / 'node_modules').mkdir(parents=True)
    (root / 'frontend' / 'package.json').write_text('{}')
    work = IsolatedWorkspace(root, 'one').prepare()
    for folder in (work / 'node_modules', work / 'frontend' / 'node_modules'):
        assert folder.is_dir() and not folder.is_symlink()
    assert (work / 'node_modules' / 'vue' / 'index.js').read_text() == 'export default 1'
    assert os.readlink(work / 'node_modules' / '.bin' / 'vue') == '../vue/index.js'
    assert (work / 'node_modules' / 'site').resolve() == (work / 'frontend').resolve()
    # The task's writes stay in its copy and are never merged.
    (work / 'node_modules' / 'vue' / 'index.js').write_text('changed in the task')
    (work / 'frontend' / 'node_modules' / '.vite-temp').mkdir()
    assert (root / 'node_modules' / 'vue' / 'index.js').read_text() == 'export default 1'
    assert not (root / 'frontend' / 'node_modules' / '.vite-temp').exists()
    assert IsolatedWorkspace(root, 'one').plan() == []


@pytest.mark.parametrize('output,expected', [
    # Refusals recorded in Blog4, Blog9 and Blog10, with the snapshot and project paths of this test.
    ("Error: EPERM: operation not permitted, open '{root}/node_modules/vue-tsc/bin/vue-tsc.js'", 'is in the project root'),
    ('ls: {root}: Operation not permitted', 'is in the project root'),
    ('error: failed to read from file `{root}/pyproject.toml`: Operation not permitted (os error 1)', 'is in the project root'),
    ("Error: EPERM: operation not permitted, open '{work}/frontend/node_modules/.vite-temp/vite.config.ts.mjs'",
     'frontend/node_modules is a link to'),
    ('ls: ../../..: Operation not permitted', 'climbs out of your workspace'),
    ('mkdir: /Users/someone/.cache: Operation not permitted', 'is outside your workspace'),
    ('fatal: cannot mkdir .git: Operation not permitted', '.git may not be created'),
    ('cat: backend/.env: Operation not permitted', 'Secret .env files'),
    ('sh: ./run.sh: Permission denied', "the file's own mode"),
    ('Error: listen EPERM: operation not permitted 127.0.0.1:8101', 'network=true'),
    ('urllib.error.URLError: <urlopen error [Errno 1] Operation not permitted>', 'network=true'),
])
def test_a_refused_path_is_explained_by_the_boundary_it_crossed(tmp_path, output, expected):
    root, work = tmp_path / 'project', tmp_path / 'runs' / 'one' / 'work'
    (root / 'frontend' / 'node_modules').mkdir(parents=True)
    (work / 'frontend').mkdir(parents=True)
    (work / 'frontend' / 'node_modules').symlink_to(root / 'frontend' / 'node_modules')  # a snapshot from before copies
    tokens = active_workspace.set(work), source_workspace.set(root)
    try:
        result = command_failure('Command exited 1', output.format(root=root.resolve(), work=work.resolve()))
    finally:
        source_workspace.reset(tokens[1])
        active_workspace.reset(tokens[0])
    assert result['error_details']['code'] == 'os_permission_denied'
    assert expected in result['error_details']['next_action']
