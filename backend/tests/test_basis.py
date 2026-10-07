"""What a task is built on is recorded by the platform and compared by section.

Blog5: three deliveries were rejected for a document version the member had typed into
based_on. None of the three documents was a declared dependency of the task; one was the
member's own deliverable. The first rejection led the backend to read the whole design
and add 6.7M tokens of work nobody had assigned."""
import asyncio

from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.storage import ChatStore

DESIGN = '# 设计\n\n## 色彩\n\n蓝灰\n\n## 布局\n\n两栏\n\n## 图标\n\n- logo.svg\n'


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/basis.db')
    project = (await store.create_project('Basis', str(workspace)))['id']
    conversation = (await store.create_conversation(project))['id']
    service = Collaboration(store)
    await service.initialize()
    goal = await service.create_goal(project, 'leader', conversation, '页面', ['能用'], 'g', scale='small',
                                     requirements={'none': '测试'})
    return store, service, project, conversation, goal


async def start(s, p, conv, goal, role, key, **fields):
    """Create a goal task, claim it and return (job, context token)."""
    await s.create_request(p, 'leader', role, f'{key} 任务', key, conversation=conv, goal_id=goal['id'],
                           acceptance_criteria=['done'], **fields)
    job = (await s.claim('owner'))[2]
    assert job['id'] == f'leader:{key}'
    await s.prepare_workspace(p, job['id'])
    return job, active.set(Context(s, p, role, conv, f'ctx-{key}', request=job['id'], instance_id=job['instance_id']))


async def edit(s, p, document, version, title, text, summary):
    section = next(h for h in (await s.read(p, document))['outline'] if h['title'] == title)
    await s.publish(p, 'designer', [{'document_id': document, 'base_version': version, 'summary': summary,
                                     'edits': [{'operation': 'replace_section_body', 'section_id': section['id'], 'new_text': text}]}])
    return section['id']


async def read(document, title=None, **extra):
    outline = (await execute_collaboration('read', {'document_id': document}))['outline']
    if title is None:
        return outline
    section = next(h for h in outline if h['title'] == title)
    return await execute_collaboration('read', {'document_id': document, 'section_id': section['id'], **extra})


def test_only_a_changed_section_the_task_read_blocks_its_delivery(tmp_path):
    async def run():
        store, s, p, conv, goal = await setup(tmp_path)
        design = (await s.publish(p, 'designer', [{'path': 'docs/design/theme.md', 'base_version': 0, 'summary': '设计',
                                                   'content': DESIGN, 'ready': True}]))[0]['document_id']
        job, token = await start(s, p, conv, goal, 'frontend', 'page', needs=[{'document': 'docs/design/theme.md'}])
        try:
            assert (await read(design, '布局'))['content'].strip() == '两栏'
            # A section it never read changes: the delivery goes through and says so.
            await edit(s, p, design, 1, '图标', '- logo.svg\n- empty.svg\n', '加一个空状态插画')
            assert not (await s.get_request(p, job['id']))['needs_review']
            assert not [e for e in await s.inbox(p, 'frontend', 'ctx-page', conv, request=job['id']) if e.get('type') == 'request_basis_changed']
            # The section it read changes: told at once, with the section, and the delivery is refused.
            layout = await edit(s, p, design, 2, '布局', '三栏\n', '改成三栏')
            [event] = [e for e in await s.inbox(p, 'frontend', 'ctx-page', conv, request=job['id']) if e.get('type') == 'request_basis_changed']
            assert [c['section_id'] for c in event['changed_sections']] == [layout] and event['documents'][0]['path'] == 'docs/design/theme.md'
            refused = await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '页面完成'})
            assert refused['error'] == '你读过的 1 个章节在读之后改了，提交没有执行。'
            assert refused['documents'] == [{'document_id': design, 'path': 'docs/design/theme.md', 'title': 'theme',
                                             'read_version': 1, 'current_version': 3}]
            assert refused['changed_sections'] == [{'document_id': design, 'section_id': layout, 'title': '布局',
                                                    'changed_in': [3], 'summary': 'v3: 改成三栏'}]
            assert '只读上面列出的这几节' in refused['next_action'] and refused['error_details']['code'] == 'basis_changed'
            assert (await s.get_request(p, job['id']))['state'] == 'running' and not (await s.get_request(p, job['id'])).get('workspace_published')
            # Reading the named section is all it takes; no version number is typed anywhere.
            await read(design, '布局')
            delivered = await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '页面完成'})
            assert delivered['state'] == 'review' and 'basis_notes' not in delivered
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_unread_changes_are_noted_and_undeclared_or_own_documents_never_conflict(tmp_path):
    async def run():
        store, s, p, conv, goal = await setup(tmp_path)
        design = (await s.publish(p, 'designer', [{'path': 'docs/design/theme.md', 'base_version': 0, 'summary': '设计',
                                                   'content': DESIGN, 'ready': True}]))[0]['document_id']
        other = (await s.publish(p, 'product', [{'path': 'docs/notes.md', 'base_version': 0, 'summary': '备忘',
                                                 'content': '# 备忘\n\n## 一\n\n旧\n'}]))[0]['document_id']
        job, token = await start(s, p, conv, goal, 'backend', 'api', needs=[{'document': 'docs/design/theme.md'}],
                                 documents=['docs/backend/api.md'])
        try:
            await read(design, '色彩')
            await read(other, '一')  # looked at, never declared: not a basis (Blog5, 08:22 and 09:48)
            own = (await execute_collaboration('publish', {'changes': [{'path': 'docs/backend/api.md', 'base_version': 0,
                                                                        'summary': '契约', 'content': '# API\n\n## 登录\n\nPOST /login\n'}]}))['revisions'][0]
            await read(own['document_id'], '登录')
            await edit(s, p, other, 1, '一', '新\n', '改备忘')
            await edit(s, p, design, 1, '图标', '- a.svg\n', '换图标')
            # Its own deliverable moves on after it read it (Blog5, 09:31).
            section = next(h for h in await read(own['document_id']) if h['title'] == '登录')
            await execute_collaboration('publish', {'changes': [{'document_id': own['document_id'], 'base_version': 1, 'summary': '补充',
                'edits': [{'operation': 'replace_section_body', 'section_id': section['id'], 'new_text': 'POST /api/login\n'}]}]})
            delivered = await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '完成'})
            assert delivered['state'] == 'review'
            assert delivered['basis_notes'] == ['docs/design/theme.md 已到 v2，改动不在你读过的章节里。']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_a_removed_section_asks_for_the_outline_and_a_module_task_rests_on_its_module_only(tmp_path):
    async def run():
        store, s, p, conv, _ = await setup(tmp_path)
        requirements = ('概述\n\n# 登录\n\n## 需求1：登录\n\n输入密码\n\n## 验收标准\n\n- 能登录\n\n'
                        '# 文章\n\n## 需求1：发布\n\n写文章\n\n## 验收标准\n\n- 能发布\n')
        path = 'docs/product/requirements.md'
        document = (await s.publish(p, 'product', [{'path': path, 'base_version': 0, 'summary': '需求', 'content': requirements,
                                                    'ready': True}]))[0]['document_id']
        (tmp_path / 'workspace/backend').mkdir()
        (tmp_path / 'workspace/backend/pyproject.toml').write_text('[project]\n')
        goal = await s.create_goal(p, 'leader', conv, '博客', ['能用'], 'blog', scale='medium', requirements={'document': path})
        job, token = await start(s, p, conv, goal, 'backend', 'login', module='登录')
        try:
            reference = job['module_ref']
            module = await execute_collaboration('read', {'document_id': document, 'section_id': reference['section_id'], 'subtree': True})
            assert '输入密码' in module['content'] and '- 能登录' in module['content']
            other = next(h for h in await read(document) if h['title'] == '文章')
            await execute_collaboration('read', {'document_id': document, 'section_id': other['id'], 'subtree': True})
            # Another module changes, even one it read: not this task's basis.
            publish = next(h for h in await read(document) if h['title'] == '需求1：发布')
            await s.publish(p, 'product', [{'document_id': document, 'base_version': 1, 'summary': '改文章模块',
                'edits': [{'operation': 'replace_section_body', 'section_id': publish['id'], 'new_text': '写文章并定时发布\n'}]}])
            assert not (await s.get_request(p, job['id']))['needs_review']
            # A requirement of its own module is renamed: its section ID is gone.
            login = next(h for h in await read(document) if h['title'] == '需求1：登录')
            await s.publish(p, 'product', [{'document_id': document, 'base_version': 2, 'summary': '登录改为短信', 'edits': [
                {'operation': 'delete_section', 'section_id': login['id']},
                {'operation': 'insert_section', 'section_id': reference['section_id'], 'new_text': '## 需求1：短信登录\n\n输入验证码'}]}])
            refused = await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '完成'})
            assert refused['error'] == '你读过的 1 个章节被删除或改名了，提交没有执行。'
            assert [r['title'] for r in refused['removed_sections']] == ['需求1：登录'] and '大纲' in refused['next_action']
            again = await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '完成'})
            assert 'removed_sections' in again
            await read(document)  # the outline shows what is there now
            assert (await execute_collaboration('submit_task', {'identifier': job['id'], 'result': '完成'}))['state'] == 'review'
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_a_conflict_at_the_end_of_a_run_blocks_the_task_by_error_type(tmp_path):
    from app.collaboration.basis import BasisChanged
    from app.storage import StorageConflictError

    async def run():
        store, s, p, conv, goal = await setup(tmp_path)
        design = (await s.publish(p, 'designer', [{'path': 'docs/design/theme.md', 'base_version': 0, 'summary': '设计',
                                                   'content': DESIGN, 'ready': True}]))[0]['document_id']
        job, token = await start(s, p, conv, goal, 'frontend', 'page', needs=[{'document': 'docs/design/theme.md'}])
        try:
            await read(design, '布局')
        finally:
            active.reset(token)
        await edit(s, p, design, 1, '布局', '三栏\n', '改成三栏')
        try:
            await s.publish_artifacts(p, job['id'], job['instance_id'])
            raise AssertionError('expected a basis conflict')
        except BasisChanged as error:
            await s.mark_conflict(p, job['id'], error, job['instance_id'])
        blocked = await s.get_request(p, job['id'])
        assert blocked['blocked_reason'] == 'basis_changed' and '布局' in blocked['result']
        other, token = await start(s, p, conv, goal, 'backend', 'other')
        active.reset(token)
        await s.mark_conflict(p, other['id'], StorageConflictError('Merge conflict in a.py'), other['instance_id'])
        assert (await s.get_request(p, other['id']))['blocked_reason'] == 'merge_conflict'
        await store.close()
    asyncio.run(run())
