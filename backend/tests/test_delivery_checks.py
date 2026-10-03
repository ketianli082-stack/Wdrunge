"""Delivery checks flag Wdrunge paths and ports, registry mismatches, misplaced evidence,
size and unregistered generated files, and reach the reviewer without blocking delivery.
Wording about the platform or the team is left to the prompts."""
import asyncio
import json
from pathlib import Path

import pytest

from app.collaboration import environment
from app.collaboration.delivery_checks import delivery_findings, findings_summary
from app.collaboration.service import Collaboration
from app.collaboration.tools import Context, active, execute_collaboration
from app.storage import ChatStore


@pytest.fixture(autouse=True)
def platform_port(monkeypatch):
    monkeypatch.setattr(environment, '_platform_ports', {17420})


async def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/checks.db')
    project = (await store.create_project('Checks', str(workspace)))['id']
    conversation = (await store.create_conversation(project))['id']
    service = Collaboration(store)
    await service.initialize()
    return store, service, project, conversation, workspace


def write(root: Path, path: str, text: str):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def kinds(findings):
    return {(f['kind'], f['path'], f['line']) for f in findings}


def test_findings_cover_leaks_ports_evidence_size_and_generated_files(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        api = await s.register_service(p, 'backend', 'api')
        port = api['port']
        goal = await s.create_goal(p, 'leader', conv, 'Hello world', ['page shows the message'], 'g', scale='small')
        write(workspace, 'backend/app.py', '\n'.join([
            '# served on http://127.0.0.1:%d' % port,
            f"# moved from {workspace}/.wdrunge-runs/abc/work",
            '# refresh_api_contract does not apply here',
            '# platform API at http://127.0.0.1:17420/api',
            *['pass'] * 420]))
        write(workspace, 'backend/openapi.json', '{}')
        write(workspace, 'frontend/node_modules/lib/index.js', 'execute_command')  # dependency trees are skipped
        write(workspace, 'docs/api-contract.md', '\n'.join([
            '# API', f'Base URL http://localhost:{port}', 'Ask 林青禾（前端开发） before changing fields.',
            'Old port http://localhost:8000 is wrong.', '## 8. 验证证据（实测原始输出）',
            'HTTP/1.1 200 OK', 'HTTP/1.1 204 No Content', 'HTTP/1.1 404 Not Found',
            '在沙箱内绑定 0.0.0.0 会被拒绝']))
        await s.publish(p, 'backend', [{'path': 'docs/api-contract.md', 'base_version': 0, 'summary': 'API', 'external': True}])
        request = await s.create_request(p, 'leader', 'backend', 'API', 'api', conversation=conv, goal_id=goal['id'],
                                         acceptance_criteria=['works'], artifacts=['backend/', 'frontend/'], documents=['docs/api-contract.md'])
        findings = await delivery_findings(s, p, request, workspace)
        found = kinds(findings)
        assert ('platform_leak', 'backend/app.py', 2) in found        # Wdrunge workspace path
        assert ('platform_leak', 'backend/app.py', 4) in found        # the platform's own port
        # Wording (tool names, team members, sandbox notes) is not checked.
        assert not {('platform_leak', 'backend/app.py', 3), ('platform_leak', 'docs/api-contract.md', 3),
                    ('platform_leak', 'docs/api-contract.md', 9)} & found
        assert ('port_mismatch', 'docs/api-contract.md', 4) in found
        assert not any(f['line'] in {1, 2} and f['kind'] == 'port_mismatch' for f in findings)  # registered port agrees
        assert ('evidence_in_contract', 'docs/api-contract.md', 5) in found
        assert ('evidence_in_contract', 'docs/api-contract.md', 0) in found
        assert ('generated_without_command', 'backend/openapi.json', 0) in found
        assert any(f['kind'] == 'oversized' for f in findings)
        assert not any('node_modules' in f['path'] for f in findings)
        assert findings[0]['severity'] == 'high'
        summary = findings_summary(findings)
        assert summary.startswith('Delivery findings') and 'platform_leak' in summary
        # Registering how the generated file is produced clears that finding.
        await s.register_command(p, 'backend', 'export-openapi', ['python3', 'scripts/export.py', '-o', 'backend/openapi.json'])
        again = await delivery_findings(s, p, request, workspace)
        assert not any(f['kind'] == 'generated_without_command' for f in again)
        await store.close()
    asyncio.run(run())


def test_lockfiles_are_not_counted_and_platform_wording_is_not_checked(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        goal = await s.create_goal(p, 'leader', conv, 'Hello world', ['page shows the message'], 'g', scale='small')
        write(workspace, 'frontend/package-lock.json', '{\n' + '"x": 1,\n' * 1500 + '}')
        write(workspace, 'frontend/vite.config.js', '\n'.join([
            "// 服务 web：固定监听 127.0.0.1:8101（不使用保留端口 1420 / 17420）",
            "// 服务名 api（服务注册表中的名称）",
            "// 均为注册表 verified 状态",
            "export default {}"]))
        request = await s.create_request(p, 'leader', 'frontend', 'Web', 'web', conversation=conv, goal_id=goal['id'],
                                         acceptance_criteria=['works'], artifacts=['frontend/'])
        findings = await delivery_findings(s, p, request, workspace)
        assert not any(f['kind'] == 'oversized' for f in findings)
        assert not any(f['path'].endswith('package-lock.json') for f in findings)
        assert not any(f['kind'] == 'platform_leak' for f in findings)
        await store.close()
    asyncio.run(run())


def test_the_wdrunge_repository_itself_is_not_flagged(tmp_path, monkeypatch):
    from app.agents.tools import _workspace
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        monkeypatch.setattr(_workspace, 'WORKSPACE_ROOT', workspace)
        write(workspace, 'backend/tools.py', 'def execute_command(): pass  # Wdrunge sandbox\n')
        request = await s.create_request(p, 'leader', 'backend', 'Tools', 'tools', conversation=conv, artifacts=['backend/'])
        assert await delivery_findings(s, p, request, workspace) == []
        await store.close()
    asyncio.run(run())


def test_submission_attaches_findings_for_the_reviewer_without_blocking(tmp_path):
    async def run():
        store, s, p, conv, workspace = await setup(tmp_path)
        write(workspace, 'README.md', 'Run it; the platform API is at http://127.0.0.1:17420/\n')
        request = await s.create_request(p, 'leader', 'backend', 'Docs', 'docs', conversation=conv, artifacts=['README.md'])
        job = (await s.claim('owner'))[2]
        from app.agents.tools._workspace import active_workspace
        snapshot = await s.prepare_workspace(p, request['id'])
        tokens = (active.set(Context(s, p, 'backend', conv, 'ctx', request=request['id'], instance_id=job['instance_id'])),
                  active_workspace.set(snapshot.work))
        try:
            checked = await execute_collaboration('check_delivery', {})
            assert checked['findings'][0]['kind'] == 'platform_leak'
            delivered = await execute_collaboration('transition', {'identifier': request['id'], 'state': 'review', 'result': 'README written'})
            assert delivered['state'] == 'review' and delivered['findings'][0]['path'] == 'README.md'
        finally:
            active_workspace.reset(tokens[1])
            active.reset(tokens[0])
        stored = await s.get_request(p, request['id'])
        assert stored['findings'][0]['kind'] == 'platform_leak'
        events = await s.inbox(p, 'leader', 'leader-ctx', conv)
        status = next(e for e in events if e.get('type') == 'request_status')
        assert 'platform_leak' in status['findings']
        await store.close()
    asyncio.run(run())
