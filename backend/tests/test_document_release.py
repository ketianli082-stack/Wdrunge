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
                                        'content': '# 需求\n\n## 范围\n\n博客\n\n## 验收\n\n（待续）\n'}])
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
                                      acceptance_criteria=['design published'], documents=['docs/design.md'])
        waiting = await s.create_request(p, 'leader', 'frontend', 'Page', 'page', conversation=conv,
                                         needs=[{'document': 'docs/design.md'}])
        job = (await s.claim('owner'))[2]
        brief = await s.task_brief(p, job)
        budget = document_budget('small', 'docs/design.md')
        assert f'docs/design.md about {budget:,}' in brief and 'frontend (docs/design.md)' in brief and 'ready=true' in brief
        token = active.set(Context(s, p, 'designer', conv, 'ctx', request=job['id'], instance_id=job['instance_id']))
        try:
            first = await execute_collaboration('publish', {'changes': [{'path': 'docs/design.md', 'base_version': 0, 'summary': 'Start',
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
