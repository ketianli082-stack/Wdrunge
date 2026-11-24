"""Whole-document reads in one call, and sectioned drafts of declared documents that
are filled progressively; dependents start on delivery or an explicit ready mark."""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.storage import ChatStore


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/docs.db')
    project = (await store.create_project('Docs', str(workspace)))['id']
    conversation = (await store.create_conversation(project))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, conversation, workspace


def test_whole_document_reads_return_text_and_outline_in_one_call(tmp_path):
    async def run():
        store, s, p, conv, _ = await setup(tmp_path)
        body = '# API\n\nIntro\n\n## Endpoints\n\nGET /api/hello\n\n## Errors\n\n' + 'x' * 9000 + '\n'
        document = (await s.publish(p, 'backend', [{'path': 'docs/api.md', 'base_version': 0, 'summary': 'API', 'content': body}]))[0]
        token = active.set(Context(s, p, 'frontend', conv, 'ctx'))
        try:
            first = await execute_collaboration('read', {'document_id': document['document_id'], 'whole': True, 'limit': 8000})
            assert first['content'].startswith('# API') and 'GET /api/hello' in first['content']
            assert [h['title'] for h in first['outline']][-3:] == ['API', 'Endpoints', 'Errors'] and first['next_offset']
            text, offset = first['content'], first['next_offset']
            while offset is not None:
                page = await execute_collaboration('read', {'document_id': document['document_id'], 'whole': True,
                                                            'offset': offset, 'limit': 8000})
                assert page['outline'] == []
                text, offset = text + page['content'], page['next_offset']
            assert text == body
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_an_unfilled_draft_can_be_replaced_whole_but_a_written_document_cannot(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        task = await s.create_request(p, 'leader', 'testing', 'Write the README', 'readme', conversation=conv,
                                      documents=['docs/README.md'])
        [draft] = await s.create_document_drafts(p, task)
        token = active.set(Context(s, p, 'testing', conv, 'ctx'))
        try:
            # The author's own structure replaces the placeholder outline in one publish.
            body = '# Hello\n\n## Start\n\npython3 backend/server.py\n'
            replaced = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'],
                'base_version': draft['version'], 'summary': 'README', 'content': body}]})
            assert 'error' not in replaced
            assert (workspace / 'docs' / 'README.md').read_text() == body and await s.draft_brief(p, task) == ''
            # Once written, it changes by edits only.
            again = await execute_collaboration('publish', {'changes': [{'document_id': draft['document_id'],
                'base_version': draft['version'] + 1, 'summary': 'Rewrite', 'content': '# Other\n'}]})
            assert 'change with edits, not content' in again['error']
        finally:
            active.reset(token)
        await store.close()
    asyncio.run(run())


def test_drafts_are_sectioned_filled_progressively_and_gate_needs_and_delivery(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        report = await s.create_request(p, 'leader', 'testing', '端到端验证并发布验证报告', 'e2e', conversation=conv,
                                        documents=['docs/verification-report.md'])
        reader = await s.create_request(p, 'leader', 'product', 'Summarize the report', 'summary', conversation=conv,
                                        needs=[{'document': 'docs/verification-report.md'}])
        drafts = await s.create_document_drafts(p, report)
        assert len(drafts) == 1 and await s.create_document_drafts(p, report) == []
        text = (workspace / 'docs' / 'verification-report.md').read_text()
        # The report draft is the example of its own structure: a table row of placeholders per module.
        assert '## <模块名>' in text and '| 检查项 | 预期 | 实际 | 结论 | 证据 |' in text and '草稿：' in text
        assert 'docs/verification-report.md' in await s.draft_brief(p, report)
        job = (await s.claim('owner'))[2]
        assert job['id'] == report['id'] and await s.claim('owner') is None  # the placeholder does not satisfy needs
        with pytest.raises(ValueError, match='still the platform drafts'):
            await s.transition(p, report['id'], 'testing', 'review', result='done', expected_instance=job['instance_id'])
        document_id = drafts[0]['document_id']
        outline = (await s.read(p, document_id))['outline']
        scope = next(h for h in outline if h['title'] == '缺陷与阻塞')
        await s.publish(p, 'testing', [{'document_id': document_id, 'base_version': 1, 'summary': 'Scope',
                                        'edits': [{'operation': 'replace_section_body', 'section_id': scope['id'], 'new_text': '接口与页面\n'}]}])
        assert await s.draft_brief(p, report) == ''
        # Filling a section saves progress; the reader waits for the delivered report.
        assert await s.claim('owner') is None
        delivered = await s.transition(p, report['id'], 'testing', 'review', result='done', expected_instance=job['instance_id'])
        assert delivered['state'] == 'review'
        assert (await s.claim('owner'))[2]['id'] == reader['id']
        await store.close()
    asyncio.run(run())


def test_english_tasks_get_english_drafts_by_document_type(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        contract = await s.create_request(p, 'leader', 'backend', 'Publish the API contract', 'contract', conversation=conv,
                                          documents=['docs/api-contract.md', 'docs/notes.md'])
        await s.create_document_drafts(p, contract)
        assert '## Endpoints' in (workspace / 'docs' / 'api-contract.md').read_text()
        assert '## Open items' in (workspace / 'docs' / 'notes.md').read_text()
        await store.close()
    asyncio.run(run())


def test_runtime_creates_drafts_and_tells_the_assignee(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.agents.worker import WorkerAgent
    from app.collaboration.runtime import Scheduler
    seen = []

    class Model:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            seen.append(messages)
            return AIMessage(content='Stopping here.')
    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def scenario():
        store, s, p, conv, workspace = await setup(tmp_path)
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        scheduler = Scheduler(s, team)
        await s.create_request(p, 'leader', 'testing', 'Verify and report', 'verify', conversation=conv,
                               documents=['docs/test-report.md'])
        await scheduler.execute(*(await s.claim(scheduler.owner)))
        assignment = next(m for m in seen[0] if isinstance(m, HumanMessage) and m.content.startswith('Assigned task'))
        assert 'Draft documents were created for your declared documents: docs/test-report.md' in assignment.content
        assert (workspace / 'docs' / 'test-report.md').exists()
        await store.close()
    asyncio.run(scenario())
