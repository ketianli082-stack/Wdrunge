"""Documents release dependents on delivery or an explicit ready mark, never on a first
progressive publication; publish receipts carry the outline and the length budget."""
import asyncio

from app.collaboration.planning import DOCUMENT_BUDGETS, document_budget
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.storage import ChatStore


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/release.db')
    project = (await store.create_project('Release', str(workspace)))['id']
    conversation = (await store.create_conversation(project))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, conversation


def test_a_partial_requirements_document_does_not_start_design_until_delivered(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        product = await s.create_request(p, 'leader', 'product', '需求', 'req', conversation=conv, documents=['docs/requirements.md'])
        design = await s.create_request(p, 'leader', 'designer', '设计', 'design', conversation=conv,
                                        needs=[{'document': 'docs/requirements.md'}])
        job = (await s.claim('owner'))[2]
        assert job['id'] == product['id']
        [draft] = await s.create_document_drafts(p, product)
        # The author replaces the draft with the first sections and keeps writing.
        await s.publish(p, 'product', [{'document_id': draft['document_id'], 'base_version': 1, 'summary': 'Scope',
                                        'content': '# 账号\n\n## 需求1：登录\n\n作者登录。\n\n## 验收标准\n\n- 能登录\n'}])
        assert await s.claim('owner') is None
        await s.transition(p, product['id'], 'product', 'review', result='done', expected_instance=job['instance_id'])
        assert (await s.claim('owner'))[2]['id'] == design['id']
        await store.close()
    asyncio.run(run())


def test_documents_nobody_declares_are_released_once_they_exist(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        await s.publish(p, 'product', [{'path': 'docs/existing.md', 'base_version': 0, 'summary': 'Given', 'content': '# Given\n'}])
        reader = await s.create_request(p, 'leader', 'backend', 'Build on it', 'build', conversation=conv,
                                        needs=[{'document': './docs/existing.md'}])
        assert (await s.claim('owner'))[2]['id'] == reader['id']
        await store.close()
    asyncio.run(run())


def test_a_cancelled_producer_does_not_hold_back_an_existing_document(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        product = await s.create_request(p, 'leader', 'product', 'Spec', 'spec', conversation=conv, documents=['docs/spec.md'])
        await s.publish(p, 'product', [{'path': 'docs/spec.md', 'base_version': 0, 'summary': 'Spec', 'content': '# Spec\n'}])
        reader = await s.create_request(p, 'leader', 'backend', 'Build', 'build', conversation=conv, needs=[{'document': 'docs/spec.md'}])
        await s.transition(p, product['id'], 'leader', 'cancelled', result='not needed')
        assert (await s.claim('owner'))[2]['id'] == reader['id']
        await store.close()
    asyncio.run(run())


def test_publish_receipt_returns_outline_and_reports_crossing_the_budget_once(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, 'Blog', ['design published'], 'blog', scale='small')
        task = await s.create_request(p, 'leader', 'designer', 'Design', 'design', conversation=conv, goal_id=goal['id'],
                                      acceptance_criteria=['design published'], documents=['docs/guide.md'])
        waiting = await s.create_request(p, 'leader', 'frontend', 'Page', 'page', conversation=conv,
                                         needs=[{'document': 'docs/guide.md'}])
        job = (await s.claim('owner'))[2]
        brief = await s.task_brief(p, job)
        budget = document_budget('small', 'docs/guide.md')
        assert f'docs/guide.md about {budget:,}' in brief and 'frontend (docs/guide.md)' in brief and 'ready=true' in brief
        token = active.set(Context(s, p, 'designer', conv, 'ctx', request=job['id'], instance_id=job['instance_id']))
        try:
            first = await execute_collaboration('publish', {'changes': [{'path': 'docs/guide.md', 'base_version': 0, 'summary': 'Start',
                                                                         'content': '# Design\n\n## Colors\n\nBlue\n\n## Pages\n\nList\n'}]})
            [receipt] = first['revisions']
            assert [h['title'] for h in receipt['outline']] == ['Design', 'Colors', 'Pages']
            assert receipt['budget'] == budget and 'budget_notice' not in first and 'ready' not in receipt
            pages = next(h['id'] for h in receipt['outline'] if h['title'] == 'Pages')
            # The next edit uses the returned section ID directly.
            long = await execute_collaboration('publish', {'changes': [{'document_id': receipt['document_id'], 'base_version': 1,
                'summary': 'Pages', 'edits': [{'operation': 'replace_section_body', 'section_id': pages, 'new_text': 'x' * budget}]}]})
            assert 'over its budget' in long['budget_notice'] and 'Do not rewrite or delete' in long['budget_notice']
            again = await execute_collaboration('publish', {'changes': [{'document_id': receipt['document_id'], 'base_version': 2,
                'summary': 'More', 'ready': True, 'edits': [{'operation': 'insert_section', 'section_id': 'root', 'new_text': '## States\n\nEmpty\n'}]}]})
            assert 'budget_notice' not in again and again['revisions'][0]['ready'] is True
        finally:
            active.reset(token)
        # Marked ready, the design releases the waiting task although the designer has not submitted.
        assert (await s.claim('owner'))[2]['id'] == waiting['id']
        await store.close()
    asyncio.run(run())


def test_budgets_grow_with_scale_for_every_document_kind():
    for kind in DOCUMENT_BUDGETS['small']:
        assert DOCUMENT_BUDGETS['small'][kind] < DOCUMENT_BUDGETS['medium'][kind] < DOCUMENT_BUDGETS['large'][kind]
    assert document_budget('medium', 'docs/api-contract.md') == DOCUMENT_BUDGETS['medium']['contract']
    assert document_budget(None, 'docs/api-contract.md') is None


def test_the_receipt_suggests_ready_once_when_the_last_draft_section_is_filled(tmp_path):
    """Blog2: the designer finished the content at v6 but never marked it ready and kept
    polishing for 12 versions while the frontend waited."""
    async def run():
        store, s, p, conv = await setup(tmp_path)
        task = await s.create_request(p, 'leader', 'designer', '设计', 'design', conversation=conv, documents=['docs/design.md'])
        job = (await s.claim('owner'))[2]
        assert 'ready=true' in await s.task_brief(p, job)
        [draft] = await s.create_document_drafts(p, job)
        token = active.set(Context(s, p, 'designer', conv, 'ctx', request=job['id'], instance_id=job['instance_id']))
        try:
            outline = (await execute_collaboration('read', {'document_id': draft['document_id']}))['outline']
            sections = [h['id'] for h in outline if h['level'] == 2]
            version = 1
            for index, section in enumerate(sections):
                receipt = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'],
                    'base_version': version, 'summary': f'§{index}', 'edits': [
                        {'operation': 'replace_section_body', 'section_id': section, 'new_text': f'内容 {index}'}]}]})
                version += 1
                last = index == len(sections) - 1
                assert ('ready_hint' in receipt) == last, (index, receipt)
            assert 'ready=true' in receipt['ready_hint']
            polish = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'], 'base_version': version,
                'summary': 'typo', 'edits': [{'operation': 'replace_text', 'section_id': sections[0], 'old_text': '内容 0', 'new_text': '内容零'}]}]})
            assert 'ready_hint' not in polish
            marked = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'],
                                                                          'base_version': version + 1, 'summary': 'ready'}], 'ready': True})
            assert marked['revisions'][0]['ready'] is True and 'ready_hint' not in marked
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


REQUIREMENTS = ('博客：作者写文章，访客阅读。\n\n# 账号与登录\n\n## 需求1：作者登录\n\n作者在登录页输入密码进入后台。\n\n'
                '## 验收标准\n\n- 输入正确密码后进入后台\n- 密码错误时提示\n\n# 文章管理\n\n## 需求1：发布文章\n\n作者在后台写文章并发布。\n\n'
                '## 验收标准\n\n- 发布后文章出现在列表里\n')


def test_a_requirements_document_is_ready_only_when_every_module_is_well_formed(tmp_path):
    """Blog5: one requirements outline for all modules made developers read the whole
    document. Each level-1 heading is a module with requirements and acceptance criteria."""
    from app.collaboration.planning import document_kind, draft_content, requirement_modules, requirements_problems
    assert document_kind('docs/product/requirements.md') == 'requirements'  # was 'design': "req-ui-rements"
    assert [m['title'] for m in requirement_modules(REQUIREMENTS)] == ['账号与登录', '文章管理']
    assert requirements_problems(REQUIREMENTS) == []
    draft = draft_content('docs/product/requirements.md', '产品', True)
    assert draft.count('\n# <') == 2 and '## 验收标准' in draft and any('占位符' in p for p in requirements_problems(draft))
    cases = {
        '没有一级标题': '概述\n\n## 需求1：登录\n\n内容\n',
        '模块“文章管理”缺少二级标题“## 验收标准”': REQUIREMENTS.replace('## 验收标准\n\n- 发布后文章出现在列表里\n', ''),
        '模块“文章管理”的“验收标准”是空的': REQUIREMENTS.replace('- 发布后文章出现在列表里\n', ''),
        '模块“账号与登录”没有需求': REQUIREMENTS.replace('## 需求1：作者登录\n\n作者在登录页输入密码进入后台。\n\n', ''),
        '占位符': REQUIREMENTS.replace('发布文章', '<一句话标题>'),
        '“概述”是一级标题，但下面没有需求和验收标准': '# 概述\n\n' + REQUIREMENTS,
        '模块“账号与登录”有 6 条验收标准': REQUIREMENTS.replace('- 密码错误时提示\n', '- 密码错误时提示\n- a\n- b\n- c\n- d\n'),
    }
    for expected, content in cases.items():
        assert any(expected in problem for problem in requirements_problems(content)), expected

    async def run():
        store, s, p, conv = await setup(tmp_path)
        token = active.set(Context(s, p, 'product', conv, 'ctx'))
        try:
            broken = await execute_collaboration('publish', {'changes': [{'path': 'docs/product/requirements.md', 'base_version': 0,
                'summary': '需求', 'ready': True, 'content': cases['模块“文章管理”缺少二级标题“## 验收标准”']}]})
            assert '文章管理' in broken['error'] and '验收标准' in broken['error'] and await s.catalog(p) == []
            # Work in progress is saved without the check; only the ready mark is held to it.
            saved = await execute_collaboration('publish', {'changes': [{'path': 'docs/product/requirements.md', 'base_version': 0,
                'summary': '需求', 'content': cases['模块“文章管理”缺少二级标题“## 验收标准”']}]})
            document = saved['revisions'][0]['document_id']
            root = (await s.read(p, document))['outline'][0]
            ready = await execute_collaboration('publish', {'changes': [{'document_id': document, 'base_version': 1, 'summary': '补全',
                'ready': True, 'edits': [{'operation': 'insert_section', 'section_id': root['id'],
                                          'new_text': '## 验收标准\n\n- 发布后文章出现在列表里'}]}]})
            assert 'error' not in ready and ready['revisions'][0]['ready'] is True
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


THEME = ('# theme\n\n## 气质\n\n克制、安静、易读\n\n## 色调倾向\n\n明亮的浅色主题，强调色是沉稳的蓝灰；疏朗，造型偏硬朗。\n\n'
         '## 素材清单\n\n| 文件路径 | 用途 | 尺寸 |\n|---|---|---|\n| frontend/public/logo.svg | 站点标识 | 24×24 |\n')


def test_a_theme_is_words_only_and_at_most_400_characters():
    """Blog8: the designer's task asked for colors, type scale and component states, and
    she delivered a 10K-character token sheet with hex colors and px sizes."""
    from app.collaboration.planning import THEME_LIMIT, theme_problems
    assert theme_problems(THEME) == []
    cases = {
        '色值（#46627F）': THEME.replace('沉稳的蓝灰', '#46627F'),
        '颜色函数（rgba(）': THEME.replace('沉稳的蓝灰', 'rgba(70, 98, 127, .35)'),
        '尺寸（1.75em、14px）': THEME.replace('疏朗', '正文 14px、行距 1.75em'),
        '样式属性（line-height）': THEME.replace('疏朗', 'line-height 偏松'),
        f'上限是 {THEME_LIMIT} 字': THEME.replace('克制、安静、易读', '克制' * 200),
        '占位符': THEME.replace('克制、安静、易读', '<3–5 个关键词>'),
    }
    for expected, content in cases.items():
        assert any(expected in problem for problem in theme_problems(content)), (expected, theme_problems(content))
    # Sizes of assets are written without units; the platform's draft note is not counted.
    assert theme_problems('> 草稿：美工 正在编写。' + '注' * 400 + '\n\n' + THEME) == []


def test_a_requirements_overview_is_at_most_400_characters():
    """Blog8: assumptions that were module rules, a trade-off list and the module list
    made the overview 955 characters."""
    from app.collaboration.planning import OVERVIEW_LIMIT, draft_content, requirements_problems
    assert not any('概述' in problem for problem in requirements_problems('目标' * 190 + REQUIREMENTS))
    long = requirements_problems('目标' * 200 + REQUIREMENTS)
    assert any(f'上限是 {OVERVIEW_LIMIT} 字' in problem for problem in long)
    # The draft note left in place by section edits does not count against the author.
    draft = draft_content('docs/product/requirements.md', '产品', True)
    note = draft.split('\n\n')[0]
    assert not any('概述' in problem for problem in requirements_problems(note + '\n\n' + '目标' * 190 + REQUIREMENTS))


def test_a_theme_with_style_values_is_neither_marked_ready_nor_delivered(tmp_path):
    async def run():
        store, s, p, conv = await setup(tmp_path)
        task = await s.create_request(p, 'leader', 'designer', '主题', 'theme', conversation=conv,
                                      documents=['docs/design/theme.md'], artifacts=['frontend/public/logo.svg'])
        job = (await s.claim('owner'))[2]
        assert '400 characters' in await s.task_brief(p, job)
        token = active.set(Context(s, p, 'designer', conv, 'ctx', request=job['id'], instance_id=job['instance_id']))
        try:
            colored = THEME.replace('沉稳的蓝灰', '#46627F')
            refused = await execute_collaboration('publish', {'changes': [{'path': 'docs/design/theme.md', 'base_version': 0,
                'summary': '主题', 'ready': True, 'content': colored}]})
            assert '#46627F' in refused['error'] and await s.catalog(p) == []
            # Saved without ready, it still cannot be delivered.
            saved = await execute_collaboration('publish', {'changes': [{'path': 'docs/design/theme.md', 'base_version': 0,
                'summary': '主题', 'content': colored}]})
            (tmp_path / 'workspace' / 'frontend' / 'public').mkdir(parents=True)
            (tmp_path / 'workspace' / 'frontend' / 'public' / 'logo.svg').write_text('<svg/>')
            try:
                await s.transition(p, task['id'], 'designer', 'review', result='done', expected_instance=job['instance_id'])
                raise AssertionError('delivered a theme with color values')
            except ValueError as error:
                assert '#46627F' in str(error) and '还不能交付' in str(error)
            document = saved['revisions'][0]['document_id']
            tone = next(h['id'] for h in saved['revisions'][0]['outline'] if h['title'] == '色调倾向')
            fixed = await execute_collaboration('publish', {'changes': [{'document_id': document, 'base_version': 1,
                'summary': '去掉色值', 'ready': True, 'edits': [{'operation': 'replace_text', 'section_id': tone,
                                                               'old_text': '#46627F', 'new_text': '沉稳的蓝灰'}]}]})
            assert 'error' not in fixed and fixed['revisions'][0]['ready'] is True
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())
