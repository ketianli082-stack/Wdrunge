"""Planning rules held by the platform: goal scale, typed deliverables, starting on a
deliverable instead of a whole task, and one owner per verification."""
import asyncio

import pytest

from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration, with_parallel_advice
from app.storage import ChatStore


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/plan.db')
    project = (await store.create_project('Plan', str(workspace)))['id']
    conversation = (await store.create_conversation(project))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, conversation


def test_goal_scale_bounds_criteria_and_is_required_from_leader(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        with pytest.raises(ValueError, match='small goal allows at most 3 criteria per goal'):
            await s.create_goal(p, 'leader', conv, 'Hello world', ['a', 'b', 'c', 'd'], 'g0', scale='small')
        goal = await s.create_goal(p, 'leader', conv, 'Hello world', ['page shows API message'], 'g1', scale='small')
        with pytest.raises(ValueError, match='at most 3 criteria per task'):
            await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv, goal_id=goal['id'],
                                   acceptance_criteria=['a', 'b', 'c', 'd'])
        # Goals created without a scale (by the user) keep the general 8-criteria limit.
        free = await s.create_goal(p, 'leader', conv, 'User goal', [f'c{i}' for i in range(8)], 'g2')
        assert free['scale'] is None
        token = active.set(Context(s, p, 'leader', conv, 'leader-ctx'))
        try:
            refused = await execute_collaboration('create_goal', {'objective': 'X', 'criteria': ['x'], 'key': 'g3'})
            assert 'Choose the goal scale' in refused['error']
        finally:
            active.reset(token)
        token = active.set(Context(s, p, 'leader', conv, 'user'))
        try:
            assert 'error' not in await execute_collaboration('create_goal', {'objective': 'Y', 'criteria': ['y'], 'key': 'g4'})
        finally:
            active.reset(token)
        resized = await s.update_goal(p, 'leader', conv, goal['id'], 'active', 'User asked for more', scale='medium')
        assert resized['scale'] == 'medium' and resized['state'] == 'active'
        await store.close()
    asyncio.run(run())


def test_deliverables_are_typed_and_contradictions_rejected(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        with pytest.raises(ValueError, match='declared both as files'):
            await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv,
                                   artifacts=['backend/', 'docs/api.md'], documents=['docs/api.md'])
        with pytest.raises(ValueError, match='registry name'):
            await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv, services=['My API'])
        with pytest.raises(ValueError, match='exactly one document'):
            await s.create_request(p, 'leader', 'frontend', 'UI', 'ui', conversation=conv, needs=[{'document': 'a', 'service': 'b'}])
        request = await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv, artifacts=['backend/'],
                                         documents=['docs/api.md'], services=['api'])
        assert request['documents'] == ['docs/api.md'] and request['services'] == ['api']
        # Callers that do not use the new fields keep the same stored spec.
        plain = await s.create_request(p, 'leader', 'testing', 'Check', 'check', conversation=conv)
        assert 'documents' not in plain['spec'] and 'needs' not in plain['spec']
        await store.close()
    asyncio.run(run())


def test_dependents_start_on_a_published_document_or_verified_service(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        backend = await s.create_request(p, 'leader', 'backend', 'Contract and API', 'api', conversation=conv,
                                         artifacts=['backend/'], documents=['docs/api.md'], services=['api'])
        frontend = await s.create_request(p, 'leader', 'frontend', 'Page', 'page', conversation=conv,
                                          needs=[{'document': 'docs/api.md'}])
        testing = await s.create_request(p, 'leader', 'testing', 'Probe API', 'probe', conversation=conv, needs=[{'service': 'api'}])
        assert (await s.claim('owner'))[2]['id'] == backend['id']
        assert await s.claim('owner') is None  # nothing is published or verified yet
        await s.publish(p, 'backend', [{'path': 'docs/api.md', 'base_version': 0, 'summary': 'Contract', 'content': '# API\n'}])
        # The frontend starts while the backend task is still running.
        assert (await s.claim('owner'))[2]['id'] == frontend['id']
        assert (await s.get_request(p, backend['id']))['state'] == 'running'
        await s.register_service(p, 'backend', 'api')
        assert await s.claim('owner') is None
        await s.observe_service(p, 'backend', (await s.environment(p))['services'][0]['port'], 'api', ['python3', 'app.py'], '.')
        assert (await s.claim('owner'))[2]['id'] == testing['id']
        await store.close()
    asyncio.run(run())


def test_delivery_requires_declared_documents_published_and_services_verified(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        r = await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv, documents=['docs/api.md'], services=['api'])
        job = (await s.claim('owner'))[2]
        with pytest.raises(ValueError, match="not published: \\['docs/api.md'\\]"):
            await s.transition(p, r['id'], 'backend', 'review', result='done', expected_instance=job['instance_id'])
        await s.publish(p, 'backend', [{'path': 'docs/api.md', 'base_version': 0, 'summary': 'Contract', 'content': '# API\n'}])
        with pytest.raises(ValueError, match="service 'api' is not verified"):
            await s.transition(p, r['id'], 'backend', 'review', result='done', expected_instance=job['instance_id'])
        port = (await s.register_service(p, 'backend', 'api'))['port']
        await s.observe_service(p, 'backend', port, 'api', ['python3', 'app.py'], '.')
        delivered = await s.transition(p, r['id'], 'backend', 'review', result='done', expected_instance=job['instance_id'])
        assert delivered['state'] == 'review'
        await store.close()
    asyncio.run(run())


def test_one_owner_per_verification_and_assignees_are_told(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, 'Hello world page', ['page shows the API message'], 'g', scale='small')
        frontend = await s.create_request(p, 'leader', 'frontend', 'Page', 'page', conversation=conv, goal_id=goal['id'],
                                          acceptance_criteria=['页面加载后调用接口并显示返回的消息'])
        e2e = await s.create_request(p, 'leader', 'testing', 'E2E', 'e2e', conversation=conv, goal_id=goal['id'],
                                     acceptance_criteria=['浏览器打开页面显示接口返回的消息'], verification='end_to_end')
        with pytest.raises(ValueError, match='already has an end-to-end verification task'):
            await s.create_request(p, 'leader', 'frontend', 'Also E2E', 'e2e-2', conversation=conv, goal_id=goal['id'],
                                   acceptance_criteria=['x'], verification='end_to_end')
        brief = await s.task_brief(p, frontend)
        assert f"End-to-end verification belongs to task {e2e['id']}" in brief and 'Goal scale small' in brief
        assert 'You own the end-to-end verification' in await s.task_brief(p, e2e)
        duplicate = await s.create_request(p, 'leader', 'frontend', 'Recheck', 'recheck', conversation=conv, goal_id=goal['id'],
                                           acceptance_criteria=['浏览器打开页面显示接口返回的消息'])
        advised = await with_parallel_advice(s, p, duplicate)
        assert 'repeats task' in advised['advice'] and 'one owner' in advised['advice']
        orphan = await with_parallel_advice(s, p, await s.create_request(p, 'leader', 'frontend', 'Wait', 'wait', conversation=conv,
                                                                         needs=[{'document': 'docs/never.md'}]))
        assert 'wait for it indefinitely' in orphan['advice']
        await store.close()
    asyncio.run(run())


def test_task_tools_accept_typed_deliverables_and_needs(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, 'API and page', ['works'], 'g', scale='medium')
        token = active.set(Context(s, p, 'leader', conv, 'leader-ctx'))
        try:
            api = await execute_collaboration('create_task', {'goal_id': goal['id'], 'recipient': 'backend', 'goal': 'Contract then API',
                'criteria': ['contract published', 'GET /api/hello returns the message'], 'key': 'api',
                'artifacts': ['backend/'], 'documents': ['docs/api.md'], 'services': ['api']})
            assert api['documents'] == ['docs/api.md'] and 'error' not in api
            page = await execute_collaboration('create_task', {'goal_id': goal['id'], 'recipient': 'frontend', 'goal': 'Page',
                'criteria': ['page renders the message'], 'key': 'page', 'needs': [{'document': 'docs/api.md'}]})
            assert page['needs'] == [{'document': 'docs/api.md'}] and 'advice' not in page
            serial = await execute_collaboration('create_task', {'goal_id': goal['id'], 'recipient': 'frontend', 'goal': 'Page 2',
                'criteria': ['x'], 'key': 'page2', 'dependencies': [api['id']]})
            assert 'needs=[{document' in serial['error']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())
