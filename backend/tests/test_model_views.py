"""What a model is shown of a tool result (the tool-call log keeps the whole result).

Blog5: about 12% of all result text returned the arguments just written, fields at
their default, or identifiers no tool accepts. Each view is pinned here: showing the
model a new field means changing this file."""
import json

from app.agents.tools.development import execute_command
from app.agents.tools.validation import _views, model_result
from app.agents.tools.write_project_file import make_write_project_file
from app.collaboration.tools import COLLABORATION_TOOLS, WORKER_REQUEST


def shown(name, result, **arguments):
    return json.loads(model_result(name, arguments, json.dumps(result, ensure_ascii=False)))


def test_views_are_declared_for_the_tools_whose_results_were_mostly_echo():
    make_write_project_file(('.',))
    assert {execute_command.name, 'write_project_file', WORKER_REQUEST.name, *(
        name for name in COLLABORATION_TOOLS if name in _views)} == {
        'execute_command', 'write_project_file', 'collaboration_request', 'create_task', 'create_tasks', 'create_goal', 'collaboration_send',
        'collaboration_publish', 'collaboration_read', 'collaboration_transition', 'submit_task', 'complete_task',
        'collaboration_register_service'}
    # A tool without a view, and any failure, reaches the model unchanged.
    assert model_result('search_code', {}, '{"matches": []}') == '{"matches": []}'
    assert model_result('collaboration_read', {}, '{"document_id": "d", "content": "cut off') == '{"document_id": "d", "content": "cut off'
    assert model_result('collaboration_read', {}, '"just text"') == '"just text"'  # an unexpected shape is shown whole
    failure = json.dumps({'error': 'exit 1', 'exit_code': 1, 'argv': ['npm'], 'error_details': {'category': 'command_failed', 'code': 'nonzero_exit'}})
    assert model_result('execute_command', {}, failure) == failure


def test_a_command_result_is_its_exit_code_and_output():
    full = {'id': 'p1', 'name': 'command', 'argv': ['/snap/.wdrunge-runs/x/work/.venv/bin/pytest', '-q'], 'cwd': '/snap/.wdrunge-runs/x/work',
            'state': 'exited', 'exit_code': 0, 'timed_out': False, 'output': '3 passed', 'next_offset': 8, 'truncated': False,
            'available_from': 0, 'network': False}
    assert shown('execute_command', full, argv=['pytest', '-q']) == {'exit_code': 0, 'output': '3 passed'}
    cut = {**full, 'output': 'tail', 'truncated': True, 'next_offset': 40000, 'output_truncated': True,
           'full_log': "manage_process(action='logs', process_id='p1', offset=..., limit=...)", 'warning': 'Socket access was denied'}
    assert set(shown('execute_command', cut, argv=['pytest'])) == {
        'exit_code', 'output', 'truncated', 'next_offset', 'output_truncated', 'full_log', 'warning'}
    service = {**full, 'name': 'api', 'state': 'running', 'exit_code': None, 'network': True, 'ready': True, 'ports': [8100], 'kept': True,
               'registered_services': [{'name': 'api', 'port': 8100, 'url': 'http://127.0.0.1:8100', 'status': 'verified'}]}
    assert shown('execute_command', service, argv=['uvicorn'], background=True) == {
        'id': 'p1', 'name': 'api', 'state': 'running', 'ready': True, 'ports': [8100], 'kept': True,
        'registered_services': [{'name': 'api', 'port': 8100, 'url': 'http://127.0.0.1:8100'}], 'output': '3 passed'}


def test_a_write_receipt_drops_the_hash_and_keeps_the_structure():
    make_write_project_file(('.',))
    receipt = 'Wrote src/a.py (120 characters; sha256=' + 'ab' * 32 + ').\nStructure: L1 def main():'
    assert model_result('write_project_file', {}, receipt) == 'Wrote src/a.py (120 characters).\nStructure: L1 def main():'


def test_task_goal_and_message_receipts_do_not_repeat_what_was_sent():
    spec = {'sender': 'leader', 'recipient': 'backend', 'goal': '很长的任务正文' * 100, 'dependencies': ['leader:b1'], 'artifacts': [],
            'acceptance_criteria': ['a', 'b']}
    task = {'id': 'leader:b2', **spec, 'spec': spec, 'state': 'queued', 'created': 1.0, 'attempts': 0, 'result': '', 'delivered': [],
            'needs_review': False, 'origin_message': 'm1', 'module': '文章管理', 'dependencies': ['leader:b1', 'leader:b-init'],
            'advice': 'Criterion "a" repeats task leader:b1\'s "a"'}
    for name in ('create_task', 'collaboration_request'):
        assert shown(name, task) == {'id': 'leader:b2', 'state': 'queued', 'advice': task['advice'], 'added_dependencies': ['leader:b-init']}
    goal = {'id': 'c:g', 'objective': '博客', 'criteria': ['x'], 'spec': {'objective': '博客'}, 'state': 'active', 'created': 1.0,
            'next_reminder_at': 2.0, 'reminder_epoch': 0, 'fingerprint': '', 'reason': '', 'evidence': {}, 'scale': 'medium',
            'task_criteria_limit': 5, 'project': {'backend_initialized': False, 'frontend_initialized': False},
            'requirements': {'document': 'docs/product/requirements.md', 'ready': False}, 'next_action': '只给产品建一个任务'}
    assert set(shown('create_goal', goal)) == {'id', 'state', 'task_criteria_limit', 'project', 'requirements', 'next_action'}
    message = {'id': 'frontend:q1', 'sender': 'frontend', 'recipient': 'backend', 'content': '问题正文' * 200, 'conversation': 'c',
               'reply_to': None, 'target_request': None, 'summary': '请确认', 'chat_message_id': 'abc', 'delivery': 'live',
               'delivery_note': 'The recipient is working and sees it at its next step.'}
    assert shown('collaboration_send', message) == {'id': 'frontend:q1', 'delivery': 'live', 'delivery_note': message['delivery_note']}


def test_document_receipts_and_reads_carry_ids_and_text_without_offsets():
    outline = [{'id': 's1', 'title': '色彩', 'level': 2, 'characters': 40}, {'id': 's2', 'title': '布局', 'level': 2, 'characters': 60}]
    receipt = {'revisions': [{'document_id': 'd1', 'version': 4, 'batch_id': 'b' * 32, 'path': 'docs/design/theme.md', 'characters': 900,
                              'outline': outline, 'new_sections': [{'id': 's2', 'title': '布局'}], 'budget': 4000, 'ready': True}],
               'ready_hint': 'publish with ready=true', 'task': {'state': 'review', 'note': '任务已交付'}}
    assert shown('collaboration_publish', receipt) == {
        'revisions': [{'document_id': 'd1', 'version': 4, 'path': 'docs/design/theme.md', 'ready': True,
                       'new_sections': [{'id': 's2', 'title': '布局'}]}],
        'ready_hint': 'publish with ready=true', 'task': {'state': 'review', 'note': '任务已交付'}}
    heading = {'id': 's2', 'title': '布局', 'level': 2, 'start': 30, 'body_start': 38, 'parent': 'r1', 'body_end': 90, 'end': 90, 'characters': 52}
    section = {'document_id': 'd1', 'version': 4, 'section': heading, 'content': '两栏', 'next_offset': None, 'fragment': False}
    assert shown('collaboration_read', section) == {'document_id': 'd1', 'version': 4, 'section': {'id': 's2', 'title': '布局'}, 'content': '两栏'}
    paged = {**section, 'next_offset': 6000, 'fragment': True, 'sections': [heading]}
    assert shown('collaboration_read', paged) == {'document_id': 'd1', 'version': 4, 'section': {'id': 's2', 'title': '布局'},
                                                  'sections': [{'id': 's2', 'title': '布局'}], 'content': '两栏', 'next_offset': 6000, 'fragment': True}
    root = {'id': 'root', 'title': 'Document', 'level': 0, 'start': 0, 'body_start': 0, 'parent': None, 'body_end': 30, 'end': 90, 'characters': 30}
    listing = {'document_id': 'd1', 'version': 4, 'outline': [root, heading], 'next_offset': None}
    assert shown('collaboration_read', listing) == {'document_id': 'd1', 'version': 4, 'outline': [
        {'id': 'root', 'title': 'Document', 'level': 0}, {'id': 's2', 'title': '布局', 'level': 2}]}
    whole = {'document_id': 'd1', 'version': 4, 'path': 'docs/design/theme.md', 'outline': [], 'content': '后半部分', 'next_offset': None, 'fragment': False}
    assert shown('collaboration_read', whole) == {'document_id': 'd1', 'version': 4, 'path': 'docs/design/theme.md', 'content': '后半部分'}


def test_delivery_and_service_receipts_keep_the_outcome():
    receipt = {'id': 'leader:b1', 'state': 'review', 'sender': 'leader', 'recipient': 'backend', 'goal_id': 'c:g', 'attempts': 1,
               'artifacts': ['backend/'], 'delivered': ['backend/'], 'based_on': [], 'workspace_published': True,
               'merged_paths': ['backend/app/main.py', 'backend/app/db.py'], 'needs_review': False,
               'findings': [{'kind': 'platform_leak', 'path': 'README.md', 'line': 3, 'message': 'x', 'severity': 'medium'}],
               'basis_notes': ['docs/design/theme.md 已到 v7，改动不在你读过的章节里。']}
    for name in ('submit_task', 'complete_task', 'collaboration_transition'):
        assert shown(name, receipt) == {'state': 'review', 'findings': receipt['findings'], 'basis_notes': receipt['basis_notes'], 'merged_files': 2}
    queued = {'id': 'leader:b1', 'state': 'queued', 'next_attempt': 'continue: the assignee resumes its saved context of the earlier attempt'}
    assert shown('collaboration_transition', queued) == {'state': 'queued', 'next_attempt': queued['next_attempt']}
    service = {'name': 'api', 'kind': 'http', 'port': 8100, 'host': '127.0.0.1', 'owner': 'backend', 'claimed': True, 'source': 'agent',
               'status': 'declared', 'updated': 1.0, 'updated_by': 'backend', 'command': {'argv': ['uvicorn'], 'cwd': 'backend'},
               'url': 'http://127.0.0.1:8100'}
    assert shown('collaboration_register_service', service) == {'name': 'api', 'port': 8100, 'url': 'http://127.0.0.1:8100'}
