"""A developer's delivery is verified by the platform's own run of the registered test
and build commands, in the turn that delivers; screenshots a delivery cites must exist.
Real sandbox: the commands run the way a task's commands do."""
import asyncio
from types import SimpleNamespace

import pytest

from app.collaboration.environment import VERIFICATION
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.collaboration.workspaces import IsolatedWorkspace
from app.execution import permissions as policy, sandbox
from app.execution import processes as process_module
from app.execution.processes import Processes
from app.storage import ChatStore


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


async def task(tmp_path, recipient='backend'):
    root = tmp_path / 'project'
    (root / 'backend').mkdir(parents=True)
    (root / 'backend' / 'calc.py').write_text('def double(n):\n    return n\n')
    # The "test suite": passes once double() doubles.
    (root / 'backend' / 'run_tests.sh').write_text('grep -q "return n \\* 2" calc.py || { echo "FAILED test_double: expected 4, got 2"; exit 1; }\necho "1 passed"\n')
    (root / 'frontend').mkdir()
    (root / 'frontend' / 'build.sh').write_text('echo built\n')
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "verify.db"}')
    p = (await store.create_project('Verify', str(root)))['id']
    s = Collaboration(store)
    await s.initialize()
    await s.register_command(p, 'backend', 'backend-test', ['/bin/sh', 'run_tests.sh'], 'backend', verified=True)
    await s.register_command(p, 'frontend', 'frontend-build', ['/bin/sh', 'build.sh'], 'frontend', verified=True)
    await s.register_command(p, 'backend', 'export-openapi', ['/bin/sh', '-c', 'exit 3'], 'backend', verified=True)
    request = await s.create_request(p, 'leader', recipient, 'double', 'double')
    claimed = (await s.claim('one'))[2]
    work = (await s.prepare_workspace(p, request['id'])).work
    return store, s, p, root, request['id'], claimed['instance_id'], work


def test_only_test_and_build_commands_verify_a_delivery():
    names = ['test', 'tests', 'build', 'backend-test', 'frontend-build', 'frontend-test', 'test.unit', 'build_web']
    assert all(VERIFICATION.search(name) for name in names)
    assert not any(VERIFICATION.search(name) for name in ['migrate', 'export-openapi', 'seed', 'setup', 'latest', 'rebuilder', 'contest'])


def test_a_failing_test_command_refuses_the_delivery_in_the_same_turn(tmp_path, real_sandbox):
    async def run():
        store, s, p, root, identifier, instance, work = await task(tmp_path)
        (work / 'backend' / 'api.py').write_text('from calc import double\n')
        token = active.set(Context(s, p, 'backend', '', 'task', request=identifier, instance_id=instance))
        try:
            refused = await execute_collaboration('transition', {'identifier': identifier, 'state': 'review', 'result': 'done'})
            message = refused['error']   # what the member reads as the tool result, in its own turn
            assert message.startswith('Not delivered, nothing merged')
            assert 'backend-test (/bin/sh run_tests.sh, cwd backend) fails in your workspace: exit 1' in message
            assert 'FAILED test_double: expected 4, got 2' in message
            assert 'frontend-build' not in message and 'export-openapi' not in message   # nothing changed under frontend/; not a check
            assert not (root / 'backend' / 'api.py').exists() and (await s.get_request(p, identifier))['state'] == 'running'

            # Fixed in the same turn: the delivery goes through and the reviewer gets the platform's result.
            (work / 'backend' / 'calc.py').write_text('def double(n):\n    return n * 2\n')
            receipt = await execute_collaboration('transition', {'identifier': identifier, 'state': 'review', 'result': 'done'})
            assert receipt['state'] == 'review' and (root / 'backend' / 'api.py').is_file()
        finally:
            active.reset(token)
        [notice] = await s.inbox(p, 'leader', 'leader:c')
        [check] = notice['facts']['checks']
        assert check.startswith('backend-test (/bin/sh run_tests.sh, cwd backend): passed (exit 0, ') and 'run by the platform at delivery' in check
        await real_sandbox.close()
        await store.close()
    asyncio.run(run())


def test_a_failure_the_task_cannot_fix_is_delivered_with_the_fact(tmp_path, real_sandbox):
    """Refusals are bounded: a check broken from outside the task must not hold it forever."""
    async def run():
        store, s, p, root, identifier, instance, work = await task(tmp_path)
        (work / 'backend' / 'api.py').write_text('from calc import double\n')
        for _ in range(2):
            with pytest.raises(ValueError, match='Not delivered'):
                await s.publish_artifacts(p, identifier, instance, references='done', refuse=True)
        await s.publish_artifacts(p, identifier, instance, references='the suite fails in calc.py, which is not mine', refuse=True)
        request = await s.get_request(p, identifier)
        assert (root / 'backend' / 'api.py').is_file() and 'check_refusals' not in request
        assert 'FAILED (exit 1, run by the platform at delivery); output ends:' in request['checks'][0]
        assert 'expected 4, got 2' in request['checks'][0]
        await real_sandbox.close()
        await store.close()
    asyncio.run(run())


def test_documents_and_other_directories_run_no_checks_and_a_turn_end_delivery_is_not_refused(tmp_path, real_sandbox):
    async def run():
        store, s, p, root, identifier, instance, work = await task(tmp_path)
        (work / 'docs').mkdir()
        (work / 'docs' / 'notes.md').write_text('# notes\n')
        assert await s.run_verification(p, await s.get_request(p, identifier), root, work) == []
        # The frontend directory changed: its build runs, the backend's failing test does not.
        (work / 'frontend' / 'main.js').write_text('1')
        [build] = await s.run_verification(p, await s.get_request(p, identifier), root, work)
        assert build['name'] == 'frontend-build' and build['ok'] and build['exit_code'] == 0
        # The turn ended without a delivery call: the platform delivers, records, and refuses nothing.
        (work / 'backend' / 'api.py').write_text('x = 1\n')
        await s.publish_artifacts(p, identifier, instance, references='done')
        checks = (await s.get_request(p, identifier))['checks']
        assert [line.split(' ')[0] for line in checks] == ['backend-test', 'frontend-build']
        assert 'FAILED' in checks[0] and 'passed' in checks[1]
        # A command registered but never run is named, not run.
        await s.register_command(p, 'frontend', 'frontend-test', ['/bin/sh', '-c', 'exit 9'], 'frontend')
        (work / 'frontend' / 'main.js').write_text('2')
        results = await s.run_verification(p, await s.get_request(p, identifier), root, work)
        assert [(r['name'], r['ok']) for r in results] == [('frontend-build', True), ('frontend-test', None)]
        # Code no registered command covers is said so, never passed over in silence.
        (work / 'scripts').mkdir()
        (work / 'scripts' / 'seed.py').write_text('print(1)\n')
        from app.collaboration.environment import verification_line
        first = (await s.run_verification(p, await s.get_request(p, identifier), root, work))[0]
        (work / 'README.md').write_text('notes\n')
        assert verification_line(first) == ('no registered test or build command covers the files this task changed in scripts/, '
                                            'so the platform ran none for them')
        again = await s.run_verification(p, await s.get_request(p, identifier), root, work)
        assert again[0]['uncovered'] == ['scripts'], 'a file at the root is configuration, not uncovered code'
        await real_sandbox.close()
        await store.close()
    asyncio.run(run())


def test_a_cited_screenshot_that_does_not_exist_is_caught_at_delivery(tmp_path, real_sandbox):
    """Blog10: browser-e2169e1e25d51c45.png cited, …c42.png on disk; found by the Leader a review later."""
    async def run():
        store, s, p, root, identifier, instance, work = await task(tmp_path, 'testing')
        shots = work / 'docs' / 'testing'
        shots.mkdir(parents=True)
        (shots / 'shot-admin-login-3f2a.png').write_bytes(b'\x89PNG one')
        (shots / 'shot-home-91bc.png').write_bytes(b'\x89PNG two')
        # One cited by its path with a digit wrong, one by its name alone with a letter missing.
        (shots / 'verification.md').write_text('| 登录 | 通过 | ![](shot-admin-login-3f2.png) |\n| 首页 | 通过 | docs/testing/shot-home-91bd.png |\n')
        with pytest.raises(ValueError, match='Not delivered, nothing merged') as refused:
            await s.publish_artifacts(p, identifier, instance, references='见 docs/testing/verification.md', refuse=True)
        message = str(refused.value)
        assert 'These cited screenshots do not exist: docs/testing/shot-admin-login-3f2.png, docs/testing/shot-home-91bd.png.' in message
        assert 'Screenshots in your workspace: docs/testing/shot-admin-login-3f2a.png, docs/testing/shot-home-91bc.png' in message
        assert not (root / 'docs').exists()
        (shots / 'verification.md').write_text('| 登录 | 通过 | docs/testing/shot-admin-login-3f2a.png |\n| 首页 | 通过 | docs/testing/shot-home-91bc.png |\n')
        await s.publish_artifacts(p, identifier, instance, references='见 docs/testing/verification.md', refuse=True)
        assert sorted(path.name for path in (root / 'docs' / 'testing').iterdir()) == [
            'shot-admin-login-3f2a.png', 'shot-home-91bc.png', 'verification.md']
        assert (await s.get_request(p, identifier))['checks'] == []
        await real_sandbox.close()
        await store.close()
    asyncio.run(run())
