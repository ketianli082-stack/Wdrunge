"""A delivery is reviewed on what the platform knows, once it has finished acting on the
merge (Blog10: a delivery sent back for a 404 answered during the platform's own restart;
a read-only task assigned to learn whether cache files had been merged)."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from app.collaboration.review import service_fact
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.execution.processes import Job, processes
from app.storage import ChatStore


async def delivered(tmp_path):
    root = tmp_path / 'project'
    (root / 'frontend').mkdir(parents=True)
    (root / '.gitignore').write_text('.vite/\n')
    (root / 'frontend' / 'main.js').write_text('one')
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "review.db"}')
    p = (await store.create_project('Review', str(root)))['id']
    s = Collaboration(store)
    await s.initialize()
    request = await s.create_request(p, 'leader', 'frontend', 'page', 'page')
    claimed = (await s.claim('one'))[2]
    work = (await s.prepare_workspace(p, request['id'])).work
    (work / 'frontend' / 'main.js').write_text('two')
    (work / 'frontend' / 'List.vue').write_text('<template/>')
    for name in ('a.js', 'b.js', 'c.js'):
        (work / 'frontend' / '.vite' / 'deps').mkdir(parents=True, exist_ok=True)
        (work / 'frontend' / '.vite' / 'deps' / name).write_text('cache')
    (work / 'frontend' / 'dist').mkdir()
    (work / 'frontend' / 'dist' / 'index.html').write_text('built')
    await s.publish_artifacts(p, request['id'], claimed['instance_id'])
    return store, s, p, root, request['id'], claimed['instance_id'], work


def job(root, workspace, request, **fields):
    return Job(id=fields.pop('id', 'job1'), scope=str(root.resolve()), workspace=str(Path(workspace).resolve()), name='web',
               argv=['npm', 'run', 'dev'], cwd=str(Path(workspace).resolve() / 'frontend'), process=SimpleNamespace(returncode=None),
               temporary=None, keep=True, service='web', started_by='frontend', request=request, **fields)


def test_the_review_notice_waits_for_the_platform_and_carries_what_it_knows(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, 'jobs', {})
    monkeypatch.setattr(processes, 'moving', {})
    monkeypatch.setattr(processes, 'merges', {})
    async def run():
        store, s, p, root, identifier, instance, work = await delivered(tmp_path)
        await s.register_service(p, 'frontend', 'web', port=5173)
        # The platform is still restarting a root service to load the merge.
        restart = asyncio.Event()
        processes.track_merge(root, identifier, asyncio.create_task(restart.wait()))
        old = job(root, root, 'earlier', id='old', started_at=time.time() - 600, stale=['frontend/main.js'])
        processes.jobs['old'] = old
        await s.transition(p, identifier, 'frontend', 'review', result='done', expected_instance=instance)
        assert await s.inbox(p, 'leader', 'leader:c') == []            # nothing to wake the Leader for yet
        token = active.set(Context(s, p, 'leader', '', 'leader:c'))
        try:
            early = await execute_collaboration('inspect_request', {'identifier': identifier})
        finally:
            active.reset(token)
        assert 'still migrating or restarting' in early['facts']['in_progress']
        assert 'started before this merge changed frontend/main.js' in early['facts']['services'][0]

        # The restart finished: a new process from the root.
        del processes.jobs['old']
        processes.jobs['new'] = job(root, root, 'earlier', id='new', started_at=time.time() + 1)
        restart.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        [notice] = await s.inbox(p, 'leader', 'leader:c')
        facts = notice['facts']
        assert notice['state'] == 'review' and 'in_progress' not in facts
        assert facts['merged'] == '2 file(s) merged into the project: frontend/List.vue, frontend/main.js'
        assert facts['not_merged'] == ['frontend/.vite/ (3 files): ignored by .gitignore (pattern ".vite/")',
                                       'frontend/dist/ (1 file): build output is never merged']
        assert facts['services'] == ['web (port 5173): running from the project root, restarted after this merge: it serves the merged files']
        assert not (root / 'frontend' / '.vite').exists() and (root / 'frontend' / 'List.vue').is_file()

        # A notice is never held forever: past the limit it goes out and says what is in progress.
        processes.track_merge(root, identifier, asyncio.create_task(asyncio.Event().wait()))
        assert await s.inbox(p, 'leader', 'leader:d') == []
        async with s.transaction() as c:
            from sqlalchemy import text
            await c.execute(text('UPDATE collaboration_events SET created=created-1000'))
        [late] = await s.inbox(p, 'leader', 'leader:d')
        assert 'in_progress' in late['facts']
        for group in list(processes.merges.values()):
            for task in group:
                task.cancel()
        await store.close()
    asyncio.run(run())


def test_a_service_is_described_by_where_it_runs_and_on_which_code():
    request = {'id': 't1', 'merged_at': 1000.0}
    assert service_fact({'state': 'restarting'}, request) == 'the platform is restarting it from the project root right now'
    assert service_fact({'state': 'stopped'}, request) == 'not running'
    assert 'starting it from the project root' in service_fact({'state': 'running', 'from_snapshot': True, 'relaunch': 'pending', 'task': 't1'}, request)
    assert "this task's snapshot, which holds the delivered files" in service_fact({'state': 'running', 'from_snapshot': True, 'task': 't1'}, request)
    assert "task t0's snapshot, which does not have this merge" in service_fact({'state': 'running', 'from_snapshot': True, 'task': 't0'}, request)
    failed = service_fact({'state': 'running', 'from_snapshot': True, 'relaunch': 'failed', 'task': 't1'}, request)
    assert 'could not start it from the project root' in failed and 'service_attention' in failed
    assert 'changed nothing under its directory' in service_fact({'state': 'running', 'since': 900}, request)
    assert 'serves the merged files' in service_fact({'state': 'running', 'since': 1003}, request)


def test_the_leader_checks_a_project_fact_itself(tmp_path):
    """Blog10 05:26: three yes/no questions about project files cost a 17-minute task."""
    from app.agents.leader import PROJECT_READ_TOOLS
    from app.agents.team import Team
    from app.agents.tools._workspace import active_workspace
    root = tmp_path / 'project'
    (root / 'frontend' / '.vite' / 'deps').mkdir(parents=True)
    (root / 'frontend' / '.vite' / 'deps' / 'a.js').write_text('cache')
    (root / 'package.json').write_text('{"scripts": {"setup": "npm ci"}}')
    assert set(PROJECT_READ_TOOLS) == {'read_project_file', 'search_code', 'browse_directory'}
    shown = {tool.name for tool in Team().leader.bound_tools()}
    assert set(PROJECT_READ_TOOLS) <= shown and not shown & {'write_project_file', 'manage_file', 'execute_command'}
    token = active_workspace.set(root)
    try:
        listing = PROJECT_READ_TOOLS['browse_directory'].invoke({'path': 'frontend'})
        assert any('.vite' in str(entry) for entry in listing['entries'])
        assert '"setup": "npm ci"' in PROJECT_READ_TOOLS['read_project_file'].invoke({'path': 'package.json'})
        found = PROJECT_READ_TOOLS['search_code'].invoke({'query': 'npm ci'})
        assert any('package.json' in str(match) for match in found['matches'])
    finally:
        active_workspace.reset(token)
