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
            unstated = await execute_collaboration('create_goal', {'objective': 'X', 'criteria': ['x'], 'key': 'g3', 'scale': 'small'})
            assert 'requirements' in unstated['error']
            scaled = await execute_collaboration('create_goal', {'objective': 'X', 'criteria': ['x'], 'key': 'g3', 'scale': 'small',
                                                                 'requirements': {'none': '修一个已定位的 Bug'}})
            assert scaled['task_criteria_limit'] == 3 and scaled['requirements'] == {'none': '修一个已定位的 Bug'}
            assert scaled['project'] == {'backend_initialized': False, 'frontend_initialized': False}
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
        # A path declared both ways is one shared document, not also a file to merge.
        both = await s.create_request(p, 'leader', 'backend', 'API', 'both', conversation=conv,
                                      artifacts=['backend/', './docs/api.md'], documents=['docs/api.md'])
        assert both['artifacts'] == ['backend/'] and both['documents'] == ['docs/api.md']
        # Task text names project paths relative to the project: assignees work in a snapshot.
        async with s.reading() as c:
            root = (await s._project(c, p))['workspace_path']
        located = await s.create_request(p, 'leader', 'backend', f'Serve {root}/frontend. Workspace: {root}', 'located',
                                         conversation=conv)
        assert located['goal'] == 'Serve frontend. Workspace: the project root'
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
        [draft] = await s.publish(p, 'backend', [{'path': 'docs/api.md', 'base_version': 0, 'summary': 'Contract', 'content': '# API\n'}])
        assert await s.claim('owner') is None  # a first publication only saves the author's progress
        await s.publish(p, 'backend', [{'document_id': draft['document_id'], 'base_version': 1, 'summary': 'Complete', 'ready': True,
                                        'edits': [{'operation': 'insert_section', 'section_id': 'root', 'new_text': '## GET /hello\n'}]}])
        # Marked ready, the contract lets the frontend start while the backend task is still running.
        assert (await s.claim('owner'))[2]['id'] == frontend['id']
        assert (await s.get_request(p, backend['id']))['state'] == 'running'
        await s.register_service(p, 'backend', 'api')
        assert await s.claim('owner') is None
        await s.observe_service(p, 'backend', (await s.environment(p))['services'][0]['port'], 'api', ['python3', 'app.py'], '.')
        assert (await s.claim('owner'))[2]['id'] == testing['id']
        await store.close()
    asyncio.run(run())


def test_delivery_requires_declared_documents_published_and_services_verified(tmp_path, monkeypatch):
    from app.execution.processes import processes
    serving = {}
    monkeypatch.setattr(processes, 'service_job', lambda project, name=None, port=None: serving.get(name))
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
        # Verified once is not enough: the declared service must still be serving.
        with pytest.raises(ValueError, match="service 'api' is not running"):
            await s.transition(p, r['id'], 'backend', 'review', result='done', expected_instance=job['instance_id'])
        serving['api'] = object()
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


def test_leader_prompt_states_the_criteria_limits_it_is_held_to():
    from pathlib import Path
    from app.collaboration.planning import CRITERIA_LIMITS
    text = ' '.join((Path(__file__).parents[1] / 'app/agents/prompts/modules/leader.md').read_text().split())
    goal = '/'.join(str(CRITERIA_LIMITS[s]['goal']) for s in ('small', 'medium', 'large'))
    task = '/'.join(str(CRITERIA_LIMITS[s]['task']) for s in ('small', 'medium', 'large'))
    assert f'at most {goal} goal criteria and {task} per task' in text


def test_a_service_has_one_delivering_task_per_goal(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, 'Hello world', ['page shows API message'], 'g', scale='small')
        task = dict(conversation=conv, goal_id=goal['id'], acceptance_criteria=['works'])
        backend = await s.create_request(p, 'leader', 'backend', 'API and page', 'web', services=['web'], **task)
        assert await s.create_request(p, 'leader', 'backend', 'API and page', 'web', services=['web'], **task) == backend
        with pytest.raises(ValueError, match=r"already delivered by task leader:web.*needs=\[\{service: 'web'\}\]"):
            await s.create_request(p, 'leader', 'testing', 'Verify', 'verify', services=['web'], **task)
        await s.create_request(p, 'leader', 'testing', 'Verify', 'verify', needs=[{'service': 'web'}], **task)
        # Another goal may deliver its own service under the same name.
        other = await s.create_goal(p, 'leader', conv, 'Rework', ['page still works'], 'g2', scale='small')
        await s.create_request(p, 'leader', 'frontend', 'Page', 'page', services=['web'], conversation=conv,
                               goal_id=other['id'], acceptance_criteria=['works'])
        await store.close()
    asyncio.run(run())


REQUIREMENTS = ('博客：作者写文章，访客阅读。\n\n# 账号与登录\n\n## 需求1：作者登录\n\n作者在登录页输入密码进入后台。\n\n'
                '## 验收标准\n\n- 输入正确密码后进入后台\n\n# 文章管理\n\n## 需求1：发布文章\n\n作者在后台写文章并发布。\n\n'
                '## 验收标准\n\n- 发布后文章出现在列表里\n')
PATH = 'docs/product/requirements.md'


async def leader_call(action, **payload):
    return await execute_collaboration(action, payload)


def test_requirements_come_first_and_each_task_builds_one_module(tmp_path):
    """Blog5: the same prompt used product in Blog4 and not in Blog5, and the first backend
    task was skeleton plus three features (143 + 127 turns). The platform holds the flow."""
    async def run():
        store, s, p, conv = await setup(tmp_path)
        token = active.set(Context(s, p, 'leader', conv, 'leader-ctx'))
        try:
            goal = await leader_call('create_goal', objective='博客', criteria=['能发文章'], key='g', scale='medium',
                                     requirements={'document': PATH})
            assert goal['requirements'] == {'document': PATH, 'ready': False} and '只给产品建一个任务' in goal['next_action']
            task = dict(goal_id=goal['id'], criteria=['done'])
            early = await leader_call('create_task', recipient='backend', goal='登录接口', key='b1', module='账号与登录', **task)
            assert '还没有发布' in early['error'] and '只给产品建一个任务' in early['error']
            product = await leader_call('create_task', recipient='product', goal='写需求文档', key='prd', documents=[PATH], **task)
            assert product['state'] == 'queued'
            await s.publish(p, 'product', [{'path': PATH, 'base_version': 0, 'summary': '需求', 'content': REQUIREMENTS, 'ready': True}])
            # Developers get exactly one module, or only the initialization.
            vague = await leader_call('create_task', recipient='backend', goal='全部接口', key='b0', **task)
            assert 'module' in vague['error'] and "['账号与登录', '文章管理']" in vague['error']
            unknown = await leader_call('create_task', recipient='backend', goal='评论', key='b0', module='评论', **task)
            assert '没有模块“评论”' in unknown['error'] and '账号与登录' in unknown['error']
            uninitialized = await leader_call('create_task', recipient='backend', goal='登录接口', key='b1', module='账号与登录', **task)
            assert 'module="init"' in uninitialized['error']
            init = await leader_call('create_task', recipient='backend', goal='后端初始化', key='b-init', module='init',
                                     documents=['docs/backend/api-conventions.md'], **task)
            login = await leader_call('create_task', recipient='backend', goal='登录接口', key='b1', module='账号与登录',
                                      documents=['docs/backend/api-auth.md'], **task)
            # The module waits for its role's initialization without the Leader saying so.
            assert login['dependencies'] == [init['id']] and login['module'] == '账号与登录'
            assert login['module_ref']['path'] == PATH and login['module_ref']['section_id']
            assert await leader_call('create_task', recipient='backend', goal='登录接口', key='b1', module='账号与登录',
                                     documents=['docs/backend/api-auth.md'], **task) == login  # idempotent retry
            brief = await s.task_brief(p, login)
            assert f"section_id=\"{login['module_ref']['section_id']}\", subtree=true" in brief and '账号与登录' in brief
            assert 'module="init"' in await s.task_brief(p, init) and '文章管理' in await s.task_brief(p, init)
            again = await leader_call('create_task', recipient='backend', goal='登录接口再来', key='b1-again', module='账号与登录', **task)
            assert '已经有模块“账号与登录”的任务' in again['error'] and '不新建' in again['error']
            # One contract per module: a later module neither shares nor waits on another module's.
            shared = await leader_call('create_task', recipient='backend', goal='文章接口', key='b2', module='文章管理',
                                       documents=['docs/backend/api-auth.md'], dependencies=[login['id']], **task)
            assert '契约文档一个模块一份' in shared['error']
            posts = await leader_call('create_task', recipient='backend', goal='文章接口', key='b2', module='文章管理',
                                      documents=['docs/backend/api-posts.md'], dependencies=[login['id']], **task)
            assert posts['dependencies'] == [login['id'], init['id']]
            await leader_call('create_task', recipient='frontend', goal='前端初始化', key='f-init', module='init', **task)
            wrong = await leader_call('create_task', recipient='frontend', goal='文章页面', key='f2', module='文章管理',
                                      needs=[{'document': 'docs/backend/api-auth.md'}], **task)
            assert '是模块“账号与登录”的任务' in wrong['error']
            page = await leader_call('create_task', recipient='frontend', goal='文章页面', key='f2', module='文章管理',
                                     needs=[{'document': 'docs/backend/api-posts.md'}, {'document': 'docs/backend/api-conventions.md'}], **task)
            assert 'error' not in page
            # Other roles are not held to a module.
            assert 'error' not in await leader_call('create_task', recipient='testing', goal='验证全部模块', key='e2e', **task)
            # A goal that needs no requirements document says why, on the record.
            fix = await leader_call('create_goal', objective='修复登录按钮', criteria=['按钮可点'], key='fix', scale='small',
                                    requirements={'none': '已定位的 Bug'})
            assert fix['requirements'] == {'none': '已定位的 Bug'} and 'next_action' not in fix
            assert (await s.get_goal(p, fix['id'], conv))['requirements'] == {'none': '已定位的 Bug'}
            patch = await leader_call('create_task', goal_id=fix['id'], criteria=['done'], recipient='frontend', goal='修按钮', key='patch')
            assert patch['state'] == 'queued'
            stray = await leader_call('create_task', goal_id=fix['id'], criteria=['done'], recipient='frontend', goal='x', key='stray', module='文章管理')
            assert '没有登记需求文档' in stray['error']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_a_requirements_file_the_user_wrote_is_adopted_and_initialized_projects_need_no_init(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        workspace = tmp_path / 'workspace'
        (workspace / 'docs/product').mkdir(parents=True)
        (workspace / PATH).write_text(REQUIREMENTS)
        (workspace / 'backend').mkdir()
        (workspace / 'backend/pyproject.toml').write_text('[project]\nname = "x"\n')
        token = active.set(Context(s, p, 'leader', conv, 'leader-ctx'))
        try:
            goal = await leader_call('create_goal', objective='博客', criteria=['能发文章'], key='g', scale='medium',
                                     requirements={'document': PATH})
            assert goal['requirements'] == {'document': PATH, 'ready': True, 'document_id': goal['requirements']['document_id'],
                                            'modules': ['账号与登录', '文章管理']}
            assert (await s.read(p, goal['requirements']['document_id']))['version'] == 1
            assert goal['project'] == {'backend_initialized': True, 'frontend_initialized': False}
            login = await leader_call('create_task', goal_id=goal['id'], criteria=['done'], recipient='backend', goal='登录接口',
                                      key='b1', module='账号与登录')
            assert login['dependencies'] == []
            # A document that does not follow the module structure cannot be planned from.
            (workspace / 'docs/product/loose.md').write_text('# 需求\n\n随便写的一段话。\n')
            loose = await leader_call('create_goal', objective='另一个', criteria=['x'], key='g2', scale='small',
                                      requirements={'document': 'docs/product/loose.md'})
            assert loose['requirements']['ready'] is False and '结构不符合' in loose['next_action']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_a_document_only_task_is_delivered_by_its_ready_publication(tmp_path):
    """Publishing ready only released waiting tasks; with none created yet nobody woke.
    The product's task now reaches review, which wakes the Leader, without a submit."""
    async def run():
        store, s, p, conv = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, '博客', ['能发文章'], 'g', scale='medium', requirements={'document': PATH})
        product = await s.create_request(p, 'leader', 'product', '写需求文档', 'prd', conversation=conv, goal_id=goal['id'],
                                         acceptance_criteria=['done'], documents=[PATH])
        job = (await s.claim('owner'))[2]
        await s.prepare_workspace(p, job['id'])
        [draft] = await s.create_document_drafts(p, job)
        token = active.set(Context(s, p, 'product', conv, 'ctx', request=job['id'], instance_id=job['instance_id']))
        try:
            saved = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'], 'base_version': 1,
                                                                         'summary': '需求', 'content': REQUIREMENTS}]})
            assert 'task' not in saved and (await s.get_request(p, product['id']))['state'] == 'running'
            ready = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'], 'base_version': 2,
                                                                         'summary': '就绪', 'ready': True}]})
            assert ready['task']['state'] == 'review' and '任务已交付' in ready['task']['note']
        finally:
            active.reset(token)
        delivered = await s.get_request(p, product['id'])
        assert delivered['state'] == 'review' and PATH in delivered['result']
        events = await s.inbox(p, 'leader', f'leader:{conv}', conv)
        assert [e['state'] for e in events if e.get('type') == 'request_status'] == ['review']
        # A developer who declares only a contract also delivers code: it submits itself.
        fix = await s.create_goal(p, 'leader', conv, '接口', ['x'], 'g2', scale='small', requirements={'none': '只改接口'})
        await s.create_request(p, 'leader', 'backend', '契约与实现', 'api', conversation=conv, goal_id=fix['id'],
                               acceptance_criteria=['done'], documents=['docs/backend/api.md'])
        job = (await s.claim('owner'))[2]
        await s.prepare_workspace(p, job['id'])
        token = active.set(Context(s, p, 'backend', conv, 'ctx2', request=job['id'], instance_id=job['instance_id']))
        try:
            contract = await execute_collaboration('publish', {'changes': [{'path': 'docs/backend/api.md', 'base_version': 0,
                                                                            'summary': '契约', 'content': '# API\n', 'ready': True}]})
            assert 'task' not in contract and (await s.get_request(p, job['id']))['state'] == 'running'
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_a_whole_plan_is_created_by_one_call_in_order(tmp_path):
    """Blog6: told to create the remaining tasks at once, the Leader created 11 tasks in
    11 model calls. The batch tool takes the list; the Leader is offered only that form."""
    from app.agents.overhead import role_overhead
    from app.agents.tools.validation import model_result
    import json

    async def run():
        store, s, p, conv = await setup(tmp_path)
        workspace = tmp_path / 'workspace'
        (workspace / 'docs/product').mkdir(parents=True)
        (workspace / PATH).write_text(REQUIREMENTS)
        token = active.set(Context(s, p, 'leader', conv, 'leader-ctx'))
        try:
            goal = await leader_call('create_goal', objective='博客', criteria=['能发文章'], key='g', scale='medium',
                                     requirements={'document': PATH})
            base = dict(goal='做', criteria=['done'])
            plan = await leader_call('create_tasks', goal_id=goal['id'], tasks=[
                dict(recipient='backend', key='backend-init', module='init', **base),
                dict(recipient='backend', key='backend-1', module='账号与登录', documents=['docs/backend/api-auth.md'],
                     dependencies=['leader:backend-init'], **base),
                dict(recipient='backend', key='backend-2', module='评论', dependencies=['leader:backend-1'], **base),
                dict(recipient='backend', key='backend-3', module='文章管理', dependencies=['leader:backend-2'], **base),
                dict(recipient='testing', key='e2e', verification='end_to_end', dependencies=['leader:backend-1'], **base)])
            assert plan['created'] == 3 and plan['failed'] == 2 and '只重发失败的任务' in plan['next_action']
            first, second, unknown, orphan, e2e = plan['tasks']
            # Created in order: the second depends on the first of the same call.
            assert (first['id'], second['id'], second['dependencies']) == ('leader:backend-init', 'leader:backend-1', ['leader:backend-init'])
            assert unknown['key'] == 'backend-2' and '没有模块“评论”' in unknown['error']
            assert orphan['key'] == 'backend-3' and 'Dependency leader:backend-2 does not exist' in orphan['error']
            assert e2e['id'] == 'leader:e2e'
            assert [r['id'] for r in (await s.snapshot(p))['request']] == ['leader:backend-init', 'leader:backend-1', 'leader:e2e']
            shown = json.loads(model_result('create_tasks', {}, json.dumps(plan, ensure_ascii=False)))
            assert shown['tasks'][0] == {'id': 'leader:backend-init', 'state': 'queued'} and shown['tasks'][2] == unknown
            # Resending only what failed completes the plan; a repeated entry returns the same task.
            again = await leader_call('create_tasks', goal_id=goal['id'], tasks=[
                dict(recipient='backend', key='backend-1', module='账号与登录', documents=['docs/backend/api-auth.md'],
                     dependencies=['leader:backend-init'], **base),
                dict(recipient='backend', key='backend-3', module='文章管理', dependencies=['leader:backend-1'], **base)])
            assert 'failed' not in again and [t['id'] for t in again['tasks']] == ['leader:backend-1', 'leader:backend-3']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())
    tools = role_overhead()['leader']['tools']
    assert 'create_tasks' in tools and 'create_task' not in tools and 'delegate_tasks' in tools and 'delegate_task' not in tools
