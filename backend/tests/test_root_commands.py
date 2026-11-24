"""Task snapshots live outside the project, and what a task registers must run at the
project root after its delivery merges (Blog9: commands "verified" in a snapshot whose
.venv never reached the root; uv and pytest failing on the root's pyproject.toml)."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.tools._workspace import active_workspace, source_workspace
from app.collaboration import workspaces
from app.collaboration.environment import placement_problem
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.collaboration.workspaces import IsolatedWorkspace, project_runs, runs_root
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.permissions import task_created
from app.execution.processes import Processes
from app.storage import ChatStore, StorageConflictError


def test_snapshots_live_outside_the_project(tmp_path):
    root = tmp_path / 'Blog Project'
    (root / 'backend').mkdir(parents=True)
    (root / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    (root / 'backend' / 'app.py').write_text('print(1)\n')
    snapshot = IsolatedWorkspace(root, 'abc')
    work = snapshot.prepare()
    assert work.is_relative_to(runs_root()) and not work.is_relative_to(root)
    assert work.parent.parent == project_runs(root) and project_runs(root).name.startswith('Blog-Project-')
    assert (work / 'pyproject.toml').is_file() and (work / 'backend' / 'app.py').is_file()
    assert task_created(str(work / 'new.py'), work) is True and task_created(str(work / 'backend'), work) is False
    # A snapshot taken inside the project by an earlier version keeps working, and moves
    # out when its task runs again.
    legacy = root / '.wdrunge-runs' / 'old1'
    (legacy / 'work').mkdir(parents=True)
    (legacy / 'base').mkdir()
    (legacy / 'work' / 'mine.py').write_text('edited\n')
    (legacy / 'manifest.json').write_text('{}')
    found = IsolatedWorkspace(root, 'old1')
    assert found.work == legacy / 'work'
    found.prepare(relocate=False)
    assert legacy.exists()
    found.prepare()
    assert not (root / '.wdrunge-runs').exists()
    assert IsolatedWorkspace(root, 'old1').work == project_runs(root) / 'old1' / 'work'
    assert (project_runs(root) / 'old1' / 'work' / 'mine.py').read_text() == 'edited\n'


def test_a_runs_directory_inside_the_project_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('WDRUNGE_RUNS_DIR', str(tmp_path / 'project' / 'runs'))
    (tmp_path / 'project').mkdir()
    with pytest.raises(ValueError, match='outside the project'):
        IsolatedWorkspace(tmp_path / 'project', 'abc')


def snapshot_with(tmp_path):
    work = tmp_path / 'work'
    (work / 'backend' / '.venv' / 'bin').mkdir(parents=True)
    (work / 'backend' / 'requirements.txt').write_text('fastapi\n')
    for name in ('python', 'uvicorn'):
        script = work / 'backend' / '.venv' / 'bin' / name
        script.write_text('#!/bin/sh\n')
        script.chmod(0o755)
    (work / 'frontend' / 'node_modules' / '.bin').mkdir(parents=True)
    (work / 'frontend' / 'package.json').write_text('{}')
    vite = work / 'frontend' / 'node_modules' / '.bin' / 'vite'
    vite.write_text('#!/bin/sh\n')
    vite.chmod(0o755)
    (work / 'target' / 'release').mkdir(parents=True)
    (work / 'target' / 'release' / 'server').write_text('#!/bin/sh\n')
    (work / 'target' / 'release' / 'server').chmod(0o755)
    return work


def test_registration_refuses_executables_the_root_will_not_have(tmp_path):
    work = snapshot_with(tmp_path)
    # Blog9 init#1: a .venv at the snapshot root, its manifest in backend/.
    (work / '.venv' / 'bin').mkdir(parents=True)
    (work / '.venv' / 'bin' / 'python').write_text('#!/bin/sh\n')
    (work / '.venv' / 'bin' / 'python').chmod(0o755)
    problem = placement_problem(['.venv/bin/python', '-m', 'alembic', 'upgrade', 'head'], '.', work)
    assert 'cwd "." has no pyproject.toml or requirements.txt' in problem and 'found in: backend' in problem
    problem = placement_problem(['backend/.venv/bin/python', 'app.py'], '.', work)
    assert 'register the command with cwd="backend" and argv[0]=".venv/bin/python"' in problem
    assert 'never merged' in placement_problem(['target/release/server'], '.', work)
    assert 'absolute path into your task snapshot' in placement_problem([str(work / 'backend' / '.venv' / 'bin' / 'python')], 'backend', work)
    # What the platform installs at the root: .venv or node_modules in cwd beside the manifest.
    assert placement_problem(['.venv/bin/python', '-m', 'pytest'], 'backend', work) is None
    assert placement_problem(['uvicorn', 'app:app'], 'backend', work) is None  # found through backend/.venv on PATH
    assert placement_problem(['node_modules/.bin/vite'], 'frontend', work) is None
    assert placement_problem(['npm', 'run', 'dev'], 'frontend', work) is None
    assert placement_problem([sys.executable, '-V'], '.', work) is None


async def setup(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "root.db"}')
    project = (await store.create_project('Root', str(workspace)))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, workspace


def test_task_registrations_are_checked_and_marked_ran_in_task(tmp_path):
    async def run():
        store, s, p, _ = await setup(tmp_path)
        work = snapshot_with(tmp_path)
        request = (await s.claim('one'))[2] if await s.create_request(p, 'leader', 'backend', 'init', 'init') else None
        context = Context(s, p, 'backend', '', f"request:{request['id']}:1", request['id'])
        context.instance_id = request['instance_id']
        tokens = active.set(context), active_workspace.set(work)
        try:
            refused = await execute_collaboration('register_command', {'name': 'migrate', 'argv': ['backend/.venv/bin/python', '-m', 'alembic'], 'cwd': '.'})
            assert 'cwd="backend"' in refused['error']
            refused = await execute_collaboration('register_service', {'name': 'api', 'argv': ['target/release/server'], 'cwd': '.'})
            assert 'never merged' in refused['error']
            command = await execute_collaboration('register_command', {'name': 'test', 'argv': ['.venv/bin/python', '-m', 'pytest'], 'cwd': 'backend'})
            assert command['status'] == 'declared' and command['task'] == request['id']
        finally:
            active_workspace.reset(tokens[1])
            active.reset(tokens[0])
        await store.close()
    asyncio.run(run())


@pytest.fixture
def real_sandbox(monkeypatch):
    if not sandbox.capability()['available']:
        pytest.skip('OS sandbox unavailable')
    class Approve:
        store = SimpleNamespace(sqlite_path=None)
        async def authorize(self, *args, **kwargs):
            pass
    monkeypatch.setattr(policy, 'current', Approve())
    manager = Processes()
    monkeypatch.setattr(process_module, 'processes', manager)
    return manager


def test_delivery_installs_in_a_clean_copy_before_anything_merges(tmp_path, real_sandbox):
    """Real sandbox and a real virtual environment: the check builds the .venv the way the
    project root gets it, from the manifest in the command's cwd, and only then merges."""
    async def run():
        store, s, p, root = await setup(tmp_path)
        request = await s.create_request(p, 'leader', 'backend', 'init', 'init')
        claimed = (await s.claim('one'))[2]
        snapshot = await s.prepare_workspace(p, request['id'])
        work = snapshot.work
        (work / 'backend').mkdir()
        (work / 'backend' / 'requirements.txt').write_text('')  # installs nothing, needs no network
        (work / 'backend' / 'app.py').write_text('print("ok")\n')
        # The task's own .venv, made from the backend's base interpreter (inside the sandbox's runtimes).
        subprocess.run([getattr(sys, '_base_executable', sys.executable), '-m', 'venv', '--without-pip', str(work / 'backend' / '.venv')], check=True)
        uvicorn = work / 'backend' / '.venv' / 'bin' / 'uvicorn'
        uvicorn.write_text('#!/bin/sh\n')
        uvicorn.chmod(0o755)
        await s.register_command(p, 'backend', 'run', ['.venv/bin/python', 'app.py'], 'backend', ran=True, task=request['id'], workspace=work)
        await s.check_root_commands(p, claimed, str(root), work)
        async with s.reading() as c:
            record = await s._get(c, 'command', p, 'run')
        assert record['status'] == 'verified' and record['root_checked']
        assert not any(path.name.endswith('-check') for path in project_runs(root).iterdir())

        # Unchanged command and manifests: not installed again.
        async def unexpected(*args, **kwargs):
            raise AssertionError('checked again')
        real_sandbox.prepare_root, prepare = unexpected, real_sandbox.prepare_root
        await s.check_root_commands(p, claimed, str(root), work)
        real_sandbox.prepare_root = prepare

        # uvicorn exists only in the task's .venv; requirements.txt does not declare it.
        await s.register_service(p, 'backend', 'api', argv=['uvicorn', 'app:app'], cwd='backend', task=request['id'], workspace=work)
        with pytest.raises(ValueError, match='找不到启动命令 uvicorn') as refused:
            await s.publish_artifacts(p, request['id'], claimed['instance_id'])
        assert 'Not merged' in str(refused.value)
        assert not (root / 'backend').exists()
        # Declaring it is not enough here (no network in the test), so register what the
        # manifest provides; the delivery then merges.
        await s.register_service(p, 'backend', 'api', argv=['.venv/bin/python', 'app.py'], cwd='backend', task=request['id'], workspace=work)
        await s.publish_artifacts(p, request['id'], claimed['instance_id'])
        assert (root / 'backend' / 'app.py').read_text() == 'print("ok")\n' and not (root / 'backend' / '.venv').exists()
        await store.close()
    asyncio.run(run())


def test_a_registration_that_skipped_the_check_is_caught_at_delivery(tmp_path, real_sandbox):
    """A record from before the registration check (or written by the user) is still
    checked when its task delivers: Blog9 init#1 is refused before it merges."""
    async def run():
        store, s, p, root = await setup(tmp_path)
        request = await s.create_request(p, 'leader', 'backend', 'init', 'init')
        claimed = (await s.claim('one'))[2]
        work = (await s.prepare_workspace(p, request['id'])).work
        (work / 'backend').mkdir()
        (work / 'backend' / 'requirements.txt').write_text('')
        (work / '.venv' / 'bin').mkdir(parents=True)
        (work / '.venv' / 'bin' / 'python').symlink_to(sys.executable)
        await s.register_command(p, 'backend', 'migrate', ['.venv/bin/python', '-m', 'alembic', 'upgrade', 'head'], '.', ran=True, task=request['id'])
        with pytest.raises(ValueError, match='found in: backend'):
            await s.publish_artifacts(p, request['id'], claimed['instance_id'])
        assert not (root / 'backend').exists()
        await store.close()
    asyncio.run(run())


# --- P0-3: when dependents start and when they merge ------------------------------

async def deliver_to_review(s, p, identifier):
    async with s.transaction() as c:
        r = await s._get(c, 'request', p, identifier)
        r.update(state='review', result='delivered')
        await s._put(c, 'request', p, identifier, r)


def test_one_live_session_per_member_and_same_member_waits_for_acceptance(tmp_path, monkeypatch):
    monkeypatch.delenv('WDRUNGE_ROLE_CONCURRENCY', raising=False)
    async def run():
        store, s, p, _ = await setup(tmp_path)
        assert s.role_concurrency == 1
        init = await s.create_request(p, 'leader', 'backend', 'init', 'init')
        login = await s.create_request(p, 'leader', 'backend', 'login', 'login', dependencies=[init['id']])
        page = await s.create_request(p, 'leader', 'frontend', 'page', 'page', dependencies=[init['id']])
        assert (await s.claim('one'))[2]['id'] == init['id']
        assert await s.claim('two') is None  # login waits for init; page too
        await deliver_to_review(s, p, init['id'])
        # Another member may build on a delivery awaiting review; the same member may not.
        assert (await s.claim('three'))[2]['id'] == page['id']
        assert await s.claim('four') is None
        await s.transition(p, init['id'], 'leader', 'queued', result='rework: fix the migration')
        assert (await s.get_request(p, login['id']))['state'] == 'queued'
        async with s.transaction() as c:
            r = await s._get(c, 'request', p, init['id'])
            r.update(state='completed')
            await s._put(c, 'request', p, init['id'], r)
        assert (await s.claim('five'))[2]['id'] == login['id']
        await store.close()
    asyncio.run(run())


def test_a_delivery_on_unaccepted_work_is_held_and_resumes_after_acceptance(tmp_path):
    async def run():
        store, s, p, root = await setup(tmp_path)
        api = await s.create_request(p, 'leader', 'backend', 'api', 'api')
        page = await s.create_request(p, 'leader', 'frontend', 'page', 'page', dependencies=[api['id']])
        await s.claim('one')
        await deliver_to_review(s, p, api['id'])
        claimed = (await s.claim('two'))[2]
        assert claimed['id'] == page['id']
        work = (await s.prepare_workspace(p, page['id'])).work
        (work / 'page.html').write_text('<h1>page</h1>\n')
        context = Context(s, p, 'frontend', '', f"request:{page['id']}:1", page['id'])
        context.instance_id = claimed['instance_id']
        token = active.set(context)
        try:
            receipt = await execute_collaboration('transition', {'identifier': page['id'], 'state': 'review', 'result': 'page done', 'artifacts': ['page.html']})
        finally:
            active.reset(token)
        assert receipt['state'] == 'blocked' and receipt['waiting_for'] == [api['id']] and 'Not merged yet' in receipt['note']
        assert not (root / 'page.html').exists()
        held = await s.get_request(p, page['id'])
        assert held['blocked_reason'] == 'awaiting_acceptance' and held['resume']
        from sqlalchemy import text
        async with s.reading() as c:
            rows = (await c.execute(text("SELECT data FROM collaboration_events WHERE project=:p AND recipient='leader'"), {'p': p})).scalars().all()
        events = [e for e in map(json.loads, rows) if e.get('type') == 'delivery_waiting']
        assert events and events[-1]['waiting_for'] == [api['id']]
        # Still awaiting review, then sent back for rework: the held task stays suspended.
        assert await s.claim('three') is None
        await s.transition(p, api['id'], 'leader', 'queued', result='rework')
        assert (await s.claim('four'))[2]['id'] == api['id']
        assert await s.claim('five') is None
        async with s.transaction() as c:
            r = await s._get(c, 'request', p, api['id'])
            r.update(state='completed')
            await s._put(c, 'request', p, api['id'], r)
        resumed = (await s.claim('six'))[2]
        assert resumed['id'] == page['id'] and resumed['resume'] and 'accepted' in resumed['interrupted_reason']
        assert (await s.prepare_workspace(p, page['id'])).work == work and (work / 'page.html').is_file()
        await store.close()
    asyncio.run(run())


def test_a_turn_ending_on_unaccepted_work_is_held_not_a_conflict(tmp_path, monkeypatch):
    """Blog9 16:03: the delivery at the end of a turn met an unaccepted dependency and the
    task became a merge conflict, which the scheduler never resumes."""
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    from langgraph.checkpoint.memory import InMemorySaver

    async def run():
        store, s, p, root = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team)
        async def stream(self, task, **kwargs):
            (active_workspace.get() / 'page.html').write_text('<h1>page</h1>\n')
            yield 'content', {'content': 'page done'}
        monkeypatch.setattr(WorkerAgent, 'stream', stream)
        api = await s.create_request(p, 'leader', 'backend', 'api', 'api')
        page = await s.create_request(p, 'leader', 'frontend', 'page', 'page', dependencies=[api['id']])
        await s.claim('one')
        await deliver_to_review(s, p, api['id'])
        await scheduler.execute(*(await s.claim(scheduler.owner)))
        page = await s.get_request(p, page['id'])
        assert page['state'] == 'blocked' and page['blocked_reason'] == 'awaiting_acceptance'
        assert not (root / 'page.html').exists()
        await store.close()
    asyncio.run(run())
