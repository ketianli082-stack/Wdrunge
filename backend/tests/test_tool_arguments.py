"""Tolerant handling of model-written tool arguments (app.agents.tools.arguments).

Blog5: 14 argument errors in 790 calls, each costing a model turn. ready="true" was
refused five times under strict typing; an undeclared `force` (suggested by the
platform's own notice) refused a read. Nothing here guesses a missing argument or
repairs output that was cut off."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.base import AgentSpec
from app.agents.tools._workspace import active_workspace
from app.agents.tools.arguments import (TRUNCATED_TEXT, normalize_arguments, repair_json, screen_response, strip_notice,
                                        with_notice)
from app.agents.tools.read_project_file import make_read_project_file
from app.agents.tools.write_project_file import make_write_project_file
from app.agents.worker import WorkerAgent
from app.collaboration.tools import COLLABORATION_TOOLS


@pytest.mark.parametrize('raw,expected,repair', [
    ('```json\n{"path": "a.py"}\n```', {'path': 'a.py'}, 'code fence'),
    ('Here is the call: {"path": "a.py"} hope it helps', {'path': 'a.py'}, 'text before the JSON'),
    ('{"path": "a.py", "limit": 10,}', {'path': 'a.py', 'limit': 10}, 'trailing comma'),
    ("{'path': 'a.py', 'edits': [{'old_text': 'it''s', 'new_text': 'x'},]}", None, None),
    ("{'path': 'a.py'}", {'path': 'a.py'}, 'single quotes'),
    ('{"ready": True, "note": None, "force": False}', {'ready': True, 'note': None, 'force': False}, 'Python literals'),
    ('{path: "a.py", limit: 5}', {'path': 'a.py', 'limit': 5}, 'unquoted keys'),
    ('{"content": "line one\nline two\tend"}', {'content': 'line one\nline two\tend'}, 'raw line breaks in a string'),
    ('{"changes": [{"path": "docs/a.md", "summary": "x"}', {'changes': [{'path': 'docs/a.md', 'summary': 'x'}]}, 'missing closing bracket'),
    ('{"text": "say \\"hi\\" \\u4f60\\n"}', {'text': 'say "hi" 你\n'}, None),
])
def test_syntax_that_does_not_change_meaning_is_repaired(raw, expected, repair):
    value, repairs, reason = repair_json(raw)
    if expected is None:
        assert value is None and reason  # 'it''s' is not something to guess at
        return
    assert value == expected and reason is None
    assert repair is None or repair in repairs


@pytest.mark.parametrize('raw,reason', [
    ('{"path": "a.py", "content": "def main():\\n    print(', TRUNCATED_TEXT),   # cut inside a string
    ('{"path": "a.py", "content":', TRUNCATED_TEXT),                             # cut before a value
    ('{"path": "a.py", "con', TRUNCATED_TEXT),
    ('["a.py"]', '不是一个对象'),
    ('just read the file please', '不是一个对象'),
    ('{"path" "a.py"}', '不是合法的 JSON'),
    ('', '参数是空的'),
])
def test_truncated_or_meaningless_arguments_are_never_repaired(raw, reason):
    value, repairs, why = repair_json(raw)
    assert value is None and reason in why


def test_undeclared_arguments_are_dropped_at_any_depth_and_reported():
    read = make_read_project_file(('.',))
    top = normalize_arguments(read, {'path': 'a.py', 'encoding': 'utf-8'})
    assert top.args == {'path': 'a.py'} and top.failure is None
    assert top.notice('read_project_file') == 'encoding 不是 read_project_file 的参数，已忽略。' and top.notes() == {'ignored': ['encoding']}
    publish = COLLABORATION_TOOLS['collaboration_publish']
    # Blog5: an insert_section edit wrote `content` for new_text. The call still fails
    # (new_text is required), now saying both things.
    nested = normalize_arguments(publish, {'changes': [{'document_id': 'd', 'base_version': 2, 'summary': 's', 'edits': [
        {'operation': 'insert_section', 'section_id': 'root', 'content': '## New'}]}], 'ready': 'true'})
    assert nested.ignored == ['changes[0].edits[0].content'] and nested.failure is None
    assert nested.args['ready'] == 'true'  # accepted beside changes without being advertised
    with pytest.raises(ValueError, match='insert_section requires explicit new_text'):
        publish.args_schema.model_validate(nested.args)
    clean = normalize_arguments(read, {'path': 'a.py', 'offset': 0})
    assert clean.args == {'path': 'a.py', 'offset': 0} and clean.notice('read_project_file') is None and clean.notes() is None


def test_a_misspelled_argument_is_refused_with_the_likely_name():
    """Dropping `dependancies` would create a task without its dependencies before the notice is read."""
    create = COLLABORATION_TOOLS['create_task']
    base = {'goal_id': 'g', 'recipient': 'backend', 'goal': 'x', 'criteria': ['c'], 'key': 'k'}
    for wrong, right in (('dependancies', 'dependencies'), ('goalId', 'goal_id'), ('artifact', 'artifacts')):
        payload = {**{k: v for k, v in base.items() if k != right}, wrong: ['t1']}
        result = normalize_arguments(create, payload)
        assert result.failure and f'你是不是想写 {right}' in result.failure['error'], wrong
        assert result.failure['error_details']['code'] == 'unknown_argument'
    # A name that resembles an argument already given is just an extra.
    assert normalize_arguments(create, {**base, 'dependencies': [], 'dependency': 't1'}).failure is None


def test_json_encoded_arrays_are_decoded_and_lax_types_accepted_but_nothing_is_guessed():
    create = COLLABORATION_TOOLS['create_task']
    base = {'goal_id': 'g', 'recipient': 'backend', 'goal': 'x', 'criteria': ['c'], 'key': 'k'}
    decoded = normalize_arguments(create, {**base, 'artifacts': '["a.py"]', 'needs': '[{"document": "docs/a.md"}]'})
    assert decoded.args['artifacts'] == ['a.py'] and decoded.args['needs'] == [{'document': 'docs/a.md'}]
    assert decoded.notes() == {'repaired': ['artifacts: JSON string decoded', 'needs: JSON string decoded']}
    create.args_schema.model_validate(decoded.args)
    # Seen with the real model: module "" on tasks that have no module. Empty text for an
    # optional argument that may not be empty can only mean it is not given.
    blank = normalize_arguments(COLLABORATION_TOOLS['create_tasks'], {'goal_id': 'g', 'tasks': [
        {'recipient': 'designer', 'goal': 'x', 'criteria': ['c'], 'key': 'k', 'module': '', 'summary': '', 'serial_reason': ''}]})
    assert blank.args['tasks'][0] == {'recipient': 'designer', 'goal': 'x', 'criteria': ['c'], 'key': 'k'}
    assert blank.notes() == {'repaired': [f'tasks[0].{name}: empty string taken as not given' for name in ('module', 'summary', 'serial_reason')]}
    COLLABORATION_TOOLS['create_tasks'].args_schema.model_validate(blank.args)
    # A required argument that is empty is still an error.
    with pytest.raises(ValueError):
        create.args_schema.model_validate(normalize_arguments(create, {**base, 'goal': ''}).args)
    publish = COLLABORATION_TOOLS['collaboration_publish'].args_schema
    # Blog5: ready as a string (5 times) and a numeric string for a version.
    change = publish.model_validate({'changes': [{'document_id': 'd', 'base_version': '3', 'summary': 's', 'ready': 'true'}]}).changes[0]
    assert change.ready is True and change.base_version == 3
    # Not done, by decision: a single value for an array, a number for text, a wrapped
    # payload, a missing required argument, an ambiguous top-level version.
    for payload in ({**base, 'dependencies': 't1'}, {**base, 'summary': 5}, {'arguments': base},
                    {k: v for k, v in base.items() if k != 'goal_id'}):
        normalized = normalize_arguments(create, payload)
        with pytest.raises(ValueError):
            if normalized.failure:
                raise ValueError(normalized.failure['error'])
            create.args_schema.model_validate(normalized.args)
    with pytest.raises(ValueError, match='ambiguous'):
        publish.model_validate({'base_version': 2, 'changes': [{'document_id': 'a', 'summary': 's'}, {'document_id': 'b', 'summary': 's'}]})


def test_a_notice_goes_first_and_is_removable_from_text_results():
    assert json.loads(with_notice('{"version": 2}', 'x 已忽略。')) == {'notice': 'x 已忽略。', 'version': 2}
    text = with_notice('file body', 'x 已忽略。')
    assert text == '[notice: x 已忽略。]\nfile body' and strip_notice(text) == 'file body' and strip_notice('file body') == 'file body'
    assert with_notice('file body', None) == 'file body'


def test_unparsable_and_truncated_calls_are_kept_answerable():
    invalid = AIMessage(content='', tool_calls=[{'id': 'ok', 'name': 'read_project_file', 'args': {'path': 'a.py'}}],
                        invalid_tool_calls=[{'id': 'fix', 'name': 'read_project_file', 'args': "{'path': 'b.py',}", 'error': 'x', 'type': 'invalid_tool_call'},
                                            {'id': 'bad', 'name': 'write_project_file', 'args': '{"path": "c.py", "content": "abc', 'error': 'x', 'type': 'invalid_tool_call'}])
    screened = screen_response(invalid)
    assert [(c['id'], c['args']) for c in screened.tool_calls] == [('ok', {'path': 'a.py'}), ('fix', {'path': 'b.py'}), ('bad', {})]
    assert screened.invalid_tool_calls == [] and list(screened.additional_kwargs['call_problems']) == ['bad']
    assert screened.additional_kwargs['call_problems']['bad']['kind'] == 'truncated'
    assert screened.additional_kwargs['call_repairs'] == {'fix': ['single quotes', 'trailing comma']}
    # The length limit cut the output: the streaming parser completed the JSON, so the
    # call looks valid; it must not run.
    cut = AIMessage(content='', response_metadata={'finish_reason': 'length'},
                    tool_calls=[{'id': 'w', 'name': 'write_project_file', 'args': {'path': 'a.py', 'content': 'def main():'}}])
    assert screen_response(cut).additional_kwargs['call_problems']['w']['kind'] == 'truncated'
    fine = AIMessage(content='', tool_calls=[{'id': 'ok', 'name': 'read_project_file', 'args': {'path': 'a.py'}}])
    assert screen_response(fine) is fine


class Log:
    def __init__(self):
        self.rows = []

    async def record_tool_calls(self, conversation, agent, rows):
        self.rows += rows


def run_worker(tmp_path, monkeypatch, responses):
    """Run a worker whose model returns `responses` in order; returns (tool messages, log rows)."""
    tools = (make_read_project_file(('.',)), make_write_project_file(('.',)))
    seen = []

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            seen[:] = results
            index = sum(isinstance(m, AIMessage) for m in messages)
            return responses[index] if index < len(responses) else AIMessage(content='done')

    worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md', tools=tools))
    monkeypatch.setattr(worker, 'model', lambda: Model())
    worker._store = Log()

    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            await worker.build_graph().ainvoke({'messages': [HumanMessage(content='work')]},
                                               {'configurable': {'thread_id': 'arguments'}, 'recursion_limit': 30})
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())
    return seen, worker._store.rows


def test_the_worker_runs_a_call_with_an_extra_argument_and_tells_the_model(tmp_path, monkeypatch):
    (tmp_path / 'a.py').write_text('print(1)\n')
    results, rows = run_worker(tmp_path, monkeypatch, [AIMessage(content='', tool_calls=[
        {'id': 'r1', 'name': 'read_project_file', 'args': {'path': 'a.py', 'encoding': 'utf-8'}},
        {'id': 'w1', 'name': 'write_project_file', 'args': {'path': 'b.py', 'content': 'x = 1\n', 'overwrite': True}}])])
    read, write = results
    assert read.status == 'success' and read.content == '[notice: encoding 不是 read_project_file 的参数，已忽略。]\nprint(1)\n'
    assert write.status == 'success' and write.content.startswith('[notice: overwrite 不是 write_project_file 的参数，已忽略。]\nWrote b.py')
    assert (tmp_path / 'b.py').read_text() == 'x = 1\n'
    # The log keeps the call as the model sent it and what was ignored.
    assert [(r['id'], r['status'], r['notes']) for r in rows] == [('r1', 'success', {'ignored': ['encoding']}),
                                                                 ('w1', 'success', {'ignored': ['overwrite']})]
    assert json.loads(rows[0]['arguments']) == {'path': 'a.py', 'encoding': 'utf-8'} and rows[0]['result'] == 'print(1)\n'


def test_the_worker_answers_unparsable_and_truncated_calls_without_running_them(tmp_path, monkeypatch):
    results, rows = run_worker(tmp_path, monkeypatch, [
        AIMessage(content='', invalid_tool_calls=[{'id': 'bad', 'name': 'write_project_file', 'error': 'x', 'type': 'invalid_tool_call',
                                                   'args': '{"path": "a.py" "content": "x"}'}]),
        AIMessage(content='', response_metadata={'finish_reason': 'length'},
                  tool_calls=[{'id': 'cut', 'name': 'write_project_file', 'args': {'path': 'a.py', 'content': 'half a fi'}}]),
        AIMessage(content='', invalid_tool_calls=[{'id': 'fix', 'name': 'write_project_file', 'error': 'x', 'type': 'invalid_tool_call',
                                                   'args': "{'path': 'a.py', 'content': 'whole file',}"}])])
    unparsable, truncated, repaired = results
    first = json.loads(unparsable.content)
    assert unparsable.status == 'error' and first['error_details']['code'] == 'unparsable_arguments' and '"path": "a.py"' in first['error']
    second = json.loads(truncated.content)
    assert truncated.status == 'error' and second['error_details']['code'] == 'truncated_call' and TRUNCATED_TEXT in second['error']
    # Neither wrote anything; the call that only needed syntax repair did.
    assert repaired.status == 'success' and (tmp_path / 'a.py').read_text() == 'whole file'
    assert [(r['id'], r['status'], r['notes']) for r in rows] == [
        ('bad', 'error', {'problem': 'unparsable'}), ('cut', 'error', {'problem': 'truncated'}),
        ('fix', 'success', {'syntax': ['single quotes', 'trailing comma']})]


def test_the_leader_ignores_an_extra_argument_and_logs_it(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.collaboration.service import Collaboration
    from app.collaboration.tools import Context, active
    from app.storage import ChatStore

    async def scenario():
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/leader.db')
        project = (await store.create_project('P', str(workspace)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        team._store, team.collaboration = store, service
        seen = []

        async def reply(messages, **kwargs):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            seen[:] = results
            if not results:
                return AIMessage(content='', tool_calls=[
                    {'id': 'goal', 'name': 'create_goal', 'args': {'objective': '修按钮', 'criteria': ['可点'], 'key': 'g', 'scale': 'small',
                                                                   'requirements': {'none': '已定位的 Bug'}, 'priority': 'high'}},
                    {'id': 'typo', 'name': 'create_goal', 'args': {'objective': '另一个', 'criteria': ['x'], 'key': 'g2', 'scale': 'small',
                                                                   'requirement': {'none': 'x'}}}])
            return AIMessage(content='ok')
        monkeypatch.setattr(team.leader, 'next_message', reply)
        token = active.set(Context(service, project, 'leader', conversation, f'leader:{conversation}'))
        active_token = active_workspace.set(workspace)
        try:
            [event async for event in team._run([HumanMessage(content='修按钮')], conversation)]
        finally:
            active.reset(token)
            active_workspace.reset(active_token)
        created, refused = (json.loads(m.content) for m in seen)
        assert created['notice'] == 'priority 不是 create_goal 的参数，已忽略。' and created['state'] == 'active'
        assert '你是不是想写 requirements' in refused['error']
        rows = {row['tool_call_id']: row for row in await store.load_tool_calls(conversation)}
        assert json.loads(rows['goal']['notes']) == {'ignored': ['priority']} and rows['goal']['status'] == 'success'
        assert 'notice' not in json.loads(rows['goal']['result']) and json.loads(rows['goal']['arguments'])['priority'] == 'high'
        assert rows['typo']['status'] == 'error'
        await store.close()
    asyncio.run(scenario())


# A command is one array, argv. Shapes recorded in Blog3-Blog9 when execute_command had
# separate command and arguments fields (14 of 150 failed calls), and the plain command
# line a model may send instead of an array.
@pytest.mark.parametrize('args,argv,corrected', [
    ({'argv': ['uv', 'sync']}, ['uv', 'sync'], False),
    ({'argv': ['sh', '-c', 'ls | wc -l']}, ['sh', '-c', 'ls | wc -l'], False),
    ({'argv': 'uv sync --frozen'}, ['uv', 'sync', '--frozen'], True),
    ({'argv': '["uv", "sync"]'}, ['uv', 'sync'], False),
    ({'argv': "python3 -c 'print(1)'"}, ['python3', '-c', 'print(1)'], True),
    ({'argv': ['.venv/bin/python -m pytest', '-q']}, ['.venv/bin/python', '-m', 'pytest', '-q'], True),
    ({'command': 'npm', 'arguments': ['run', 'build']}, ['npm', 'run', 'build'], True),
    ({'command': 'sh', 'arguments': ['sh', '-c', 'echo hi']}, ['sh', '-c', 'echo hi'], True),
    ({'command': '.venv/bin/python -m alembic', 'arguments': ['current']}, ['.venv/bin/python', '-m', 'alembic', 'current'], True),
    ({'command': 'grep', 'arguments': '["-n", "x", "a.css"]'}, ['grep', '-n', 'x', 'a.css'], True),
    ({'arguments': ['.venv/bin/python', 'backend/run_server.py']}, ['.venv/bin/python', 'backend/run_server.py'], True),
    ({'command': ['npm', 'run', 'dev']}, ['npm', 'run', 'dev'], True),
])
def test_execute_command_takes_one_argv_array(args, argv, corrected):
    tool = next(t for t in development_tools() if t.name == 'execute_command')
    report = normalize_arguments(tool, args)
    assert report.failure is None and report.args['argv'] == argv
    assert bool(report.corrected) == corrected and (report.notice('execute_command') is not None) == corrected
    tool.args_schema.model_validate(report.args)


@pytest.mark.parametrize('args,code', [
    ({'argv': 'curl -s http://x/openapi.json | grep -o /api'}, 'shell_command_line'),
    ({'command': 'grep -ohE "^x" a.css | sort -u'}, 'shell_command_line'),
    ({'argv': 'ls *.py'}, 'shell_command_line'),
    ({'argv': 'echo $HOME'}, 'shell_command_line'),
    ({'command': 'grep', 'arguments': '\n["-n", "-e", "x", frontend/a.css]\n'}, 'invalid_argv'),
])
def test_command_lines_only_a_shell_understands_are_refused(args, code):
    tool = next(t for t in development_tools() if t.name == 'execute_command')
    report = normalize_arguments(tool, args)
    assert report.failure['error_details']['code'] == code
    assert '["sh", "-c"' in report.failure['error_details']['next_action'] or code == 'invalid_argv'


def test_a_first_element_with_spaces_that_exists_is_kept(tmp_path):
    executable = tmp_path / 'Agent Test Workspace' / 'run'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\n')
    tool = next(t for t in development_tools() if t.name == 'execute_command')
    report = normalize_arguments(tool, {'argv': [str(executable), '-q']})
    assert report.args['argv'] == [str(executable), '-q'] and not report.corrected


def test_registered_commands_and_services_use_argv_too():
    command = COLLABORATION_TOOLS['collaboration_register_command']
    service = COLLABORATION_TOOLS['collaboration_register_service']
    assert normalize_arguments(command, {'name': 'test', 'argv': 'uv run pytest -q'}).args['argv'] == ['uv', 'run', 'pytest', '-q']
    legacy = normalize_arguments(service, {'name': 'api', 'command': ['npm', 'run', 'dev'], 'cwd': 'frontend'})
    assert legacy.args == {'name': 'api', 'argv': ['npm', 'run', 'dev'], 'cwd': 'frontend'} and legacy.corrected
    assert normalize_arguments(service, {'name': 'api'}).args == {'name': 'api'}


def test_recorded_calls_of_either_shape_read_as_one_command():
    from app.agents.tools.arguments import call_argv
    assert call_argv({'argv': ['npm', 'test']}) == ['npm', 'test']
    assert call_argv({'command': 'npm', 'arguments': ['test']}) == ['npm', 'test']


def development_tools():
    from app.agents.tools.development import development_tools
    return development_tools
