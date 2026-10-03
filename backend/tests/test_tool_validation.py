import asyncio
import json
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from app.collaboration.payloads import ACTION_PAYLOADS
from app.collaboration.tools import COLLABORATION_TOOLS, Context, active, collaborate
from app.agents.tools.validation import tool, tool_error


@pytest.mark.parametrize('action', ACTION_PAYLOADS)
def test_every_collaboration_action_rejects_unknown_fields_before_io(action):
    async def scenario():
        # No service implementation: validation must happen before any I/O.
        token = active.set(Context(object(), 'p', 'leader', 'c', 'ctx'))
        try:
            result = await collaborate.ainvoke({'action': action, 'payload': {'unexpected': True}})
            assert 'error' in result
            assert 'unexpected' in result['error']
        finally:
            active.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize('action,payload', [
    ('publish', {'changes': [{'path': 'docs/a.md', 'summary': {}, 'content': 'a'}]}),
    ('publish', {'changes': [{'document_id': 'd', 'summary': 'edit', 'edits': [{'operation': 'replace_text', 'section_id': 'root', 'old_text': 'a', 'new_text': {}}]}]}),
    ('transition', {'identifier': 'r', 'state': 'completed', 'result': {}}),
    ('send', {'recipient': 'backend', 'content': {}, 'key': 'k'}),
    ('request', {'recipient': 'backend', 'goal': 'a', 'key': 'k', 'dependencies': 'r'}),
    ('request', {'recipient': 'backend', 'goal': 'a', 'key': 'k', 'parent': 'forged'}),
    ('read', {'document_id': 'd', 'offset': 'bad'}),
    ('subscribe', {'document_id': 'd', 'enabled': 'false'}),
    ('decision', {'title': {}, 'rationale': 'x', 'links': []}),
    ('schedule', {'identifier': 'r', 'deadline': float('nan')}),
])
def test_nested_collaboration_validation(action, payload):
    async def scenario():
        token = active.set(Context(object(), 'p', 'leader', 'c', 'ctx'))
        try:
            assert 'error' in await collaborate.ainvoke({'action': action, 'payload': payload})
        finally:
            active.reset(token)
    asyncio.run(scenario())


def test_all_role_tools_reject_extra_arguments():
    from app.agents.team import Team
    team = Team()
    all_tools = {t.name: t for agent in (team.leader, *team.workers.values()) for t in agent.spec.tools}
    all_tools['collaborate'] = collaborate
    for name, candidate in all_tools.items():
        with pytest.raises(ValueError):
            candidate.args_schema.model_validate({'unexpected': True})
        assert candidate.args_schema.model_config['extra'] == 'forbid', name


def test_tool_node_recovers_errors_but_propagates_cancellation():
    @tool
    async def flaky(path: str) -> str:
        """A filesystem failure should be a tool result."""
        if path == 'cancel':
            await asyncio.Event().wait()
        raise PermissionError('Read-only file')
    async def scenario():
        from langgraph.graph import StateGraph, MessagesState, START, END
        graph = StateGraph(MessagesState)
        graph.add_node('tools', ToolNode([flaky], handle_tool_errors=tool_error))
        graph.add_edge(START, 'tools')
        graph.add_edge('tools', END)
        node = graph.compile()
        for args in ({'path': {}}, {'path': 'a'}, {'path': 'a', 'extra': 1}):
            result = await node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name': 'flaky', 'id': 't', 'args': args}])]})
            message = result['messages'][-1]
            assert message.status == 'error'
        task = asyncio.create_task(node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name': 'flaky', 'id': 't', 'args': {'path': 'cancel'}}])]}))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_legacy_delivery_result_can_be_accepted(tmp_path):
    from app.storage import ChatStore
    from app.collaboration.service import Collaboration
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "legacy.db"}')
        project = await store.create_project('Legacy', str(tmp_path))
        service = Collaboration(store)
        request = await service.create_request(project['id'], 'leader', 'backend', 'work', 'key')
        async with service.transaction() as c:
            request.update(state='review', result={'evidence': ['tests passed']})
            await service._put(c, 'request', project['id'], request['id'], request)
        result = await service.transition(project['id'], request['id'], 'leader', 'completed')
        assert json.loads(result['result']) == {'evidence': ['tests passed']}
        assert result['state'] == 'completed'
        await store.close()
    asyncio.run(scenario())


def test_leader_recovers_invalid_outer_tool_arguments(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.storage import ChatStore
    from app.collaboration.service import Collaboration
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "leader.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        async def reply(messages, **kwargs):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content='', tool_calls=[{'name': 'collaborate', 'id': 'bad', 'args': {'action': 'unknown', 'payload': []}}])
            assert 'error' in results[-1].content.lower()
            return AIMessage(content='Recovered')
        monkeypatch.setattr(team.leader, 'next_message', reply)
        token = active.set(Context(service, project['id'], 'leader', '', 'ctx'))
        try:
            events = [event async for event in team._run([HumanMessage(content='Inspect')], 'ctx')]
            assert any(kind == 'delta' and data['content'] == 'Recovered' for kind, data in events)
            assert any(kind == 'activity' and data['phase'] == 'failed' for kind, data in events)
        finally:
            active.reset(token)
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('output,failed', [
    ({'error': 'Invalid result'}, True),
    (ToolMessage(content='{"error":"Invalid result"}', tool_call_id='t'), True),
    (ToolMessage(content='{"passed":false,"status":500}', tool_call_id='t'), True),
    (ToolMessage(content='{"passed":true}', tool_call_id='t'), False),
    (ToolMessage(content='regular content', tool_call_id='t'), False),
])
def test_rejected_tool_results_are_not_logged_as_completed(output, failed):
    from app.agents.tools.validation import tool_result_failed
    assert tool_result_failed(output) is failed


@pytest.mark.parametrize('name', COLLABORATION_TOOLS)
def test_typed_collaboration_schemas_reject_unknown_fields(name):
    from app.collaboration.tools import COLLABORATION_TOOLS
    from langchain_core.utils.function_calling import convert_to_openai_tool
    candidate = COLLABORATION_TOOLS[name]
    parameters = convert_to_openai_tool(candidate)['function']['parameters']
    assert 'payload' not in parameters['properties']
    with pytest.raises(ValueError):
        candidate.args_schema.model_validate({'unexpected': True})


def test_model_schemas_never_offer_null_for_optional_fields():
    # Qwen JSON-encodes arrays advertised as anyOf [array, null] (edits, artifacts).
    from app.agents.team import Team
    from app.agents.tools.validation import model_tool_schemas
    from app.agents.tools.write_project_file import FileWrite
    team = Team()
    tools = {t.name: t for agent in (team.leader, *team.workers.values()) for t in agent.spec.tools}
    tools.update(COLLABORATION_TOOLS)
    schemas = model_tool_schemas(tools.values())
    def nullable(schema):
        if isinstance(schema, list):
            return any(map(nullable, schema))
        return isinstance(schema, dict) and (any(c.get('type') == 'null' for c in schema.get('anyOf', ()))
                                             or any(map(nullable, schema.values())))
    assert [s['function']['name'] for s in schemas if nullable(s['function']['parameters'])] == []
    write = next(s for s in schemas if s['function']['name'] == 'write_project_file')['function']['parameters']
    assert write['properties']['edits']['type'] == 'array' and 'edits' not in write['required']
    assert FileWrite.model_validate({'path': 'a', 'content': 'x'}).edits is None
    with pytest.raises(ValueError):
        FileWrite.model_validate({'path': 'a', 'edits': '[{"old_text": "x", "new_text": "y"}]'})


def test_model_sees_nested_operations_and_required_message_key():
    from app.collaboration.tools import COLLABORATION_TOOLS, model_collaboration_tools
    from langchain_core.utils.function_calling import convert_to_openai_tool
    parameters = convert_to_openai_tool(COLLABORATION_TOOLS['collaboration_publish'])['function']['parameters']
    edit = parameters['properties']['changes']['items']['properties']['edits']['items']
    assert edit['properties']['operation']['enum'] == ['replace_text', 'replace_section_body', 'insert_section', 'delete_section']
    send = convert_to_openai_tool(COLLABORATION_TOOLS['collaboration_send'])['function']['parameters']
    assert 'key' in send['required']
    token = active.set(Context(object(), 'p', 'frontend', 'c', 'ctx'))
    try:
        names = {t.name for t in model_collaboration_tools()}
        assert 'collaboration_recover' not in names
        assert 'collaboration_wait' not in names
        assert 'collaborate' not in names
        active.get().request = 'r'
        assert 'collaboration_wait' in {t.name for t in model_collaboration_tools()}
        active.get().member = 'leader'
        assert 'collaboration_recover' in {t.name for t in model_collaboration_tools()}
    finally:
        active.reset(token)


def test_large_invalid_input_does_not_hide_the_actual_validation_error():
    from app.agents.tools.write_project_file import FileWrite, make_write_project_file
    from langgraph.prebuilt.tool_node import ToolInvocationError
    payload = {'path': 'file.txt', 'edits': 'sensitive-input-' * 10000}
    make_write_project_file(('.',))
    try:
        FileWrite.model_validate(payload)
    except ValueError as exc:
        result = tool_error(ToolInvocationError('write_project_file', exc, payload))
    assert 'edits:' in result and 'valid list' in result
    assert 'sensitive-input' not in result
    assert len(result) < 2200
    details = json.loads(result)['error_details']
    assert details['category'] == 'invalid_arguments'
    assert details['fields'][0]['received_type'] == 'string'
    assert details['fields'][0]['expected_schema']['type'] == 'array'
    assert details['fields'][0]['example'] == [{'old_text': 'exact original text', 'new_text': 'replacement text'}]


@pytest.mark.parametrize('edit', [
    {'operation': 'replace_text', 'section_id': 'root', 'old_text': 'x'},
    {'operation': 'replace_section_body', 'section_id': 'root'},
    {'operation': 'insert_section', 'section_id': 'root'},
])
def test_missing_replacement_never_silently_deletes_content(edit):
    from app.collaboration.payloads import DocumentEdit
    with pytest.raises(ValueError, match='explicit new_text'):
        DocumentEdit.model_validate(edit)


def test_existing_documents_require_an_explicit_base_version():
    from app.collaboration.payloads import DocumentChange
    with pytest.raises(ValueError, match='base_version'):
        DocumentChange.model_validate({'document_id': 'doc', 'summary': 'edit'})


@pytest.mark.parametrize('module_name,tool_name,args,directory,required', [
    ('run_project_check', 'run_project_check', {'check': 'backend_tests'}, 'backend', None),
    ('refresh_api_contract', 'refresh_api_contract', {}, 'backend', 'scripts/export_openapi.py'),
    ('refresh_frontend_client', 'refresh_frontend_client', {}, 'frontend', 'package.json'),
])
def test_command_failures_are_structured_results(tmp_path, monkeypatch, module_name, tool_name, args, directory, required):
    import importlib
    from app.agents.tools._workspace import active_workspace
    from app.agents.tools.validation import tool_result_failed
    module = importlib.import_module(f'app.agents.tools.{module_name}')
    root = tmp_path / directory
    root.mkdir()
    # These tests stub execution; give the new project-environment resolver a
    # local interpreter path without borrowing the backend host environment.
    interpreter = root / '.venv' / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    if required:
        path = root / required
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    async def run(*args, **kwargs):
        return 1, 'command failed'
    monkeypatch.setattr(module, 'run_process', run)
    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            result = await getattr(module, tool_name).ainvoke(args)
            assert result['exit_code'] == 1 and result['output'] == 'command failed'
            assert tool_result_failed(result)
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())


def test_workers_see_their_profile_and_load_groups_on_demand():
    from app.agents.tool_profiles import GROUPS, select_tools
    from app.collaboration.tools import model_collaboration_tools
    token = active.set(Context(object(), 'p', 'testing', 'c', 'ctx', request='r'))
    try:
        tools = model_collaboration_tools()
        names = lambda messages, task=None: {t.name for t in select_tools('testing', tools, messages, task)}
        initial = names([], {'goal_id': 'g'})
        # Communication stays in every worker's core: report a defect and request its fix.
        assert {'collaboration_send', 'collaboration_request', 'collaboration_wait', 'collaboration_publish', 'submit_task'} <= initial
        assert 'enable_tools' in initial and 'collaboration_transition' not in initial
        assert not initial & (GROUPS['documents_more'] | GROUPS['team_more'] | GROUPS['planning_more'])
        # After requesting a colleague's fix, the worker can accept it.
        asked = AIMessage(content='', tool_calls=[{'id': '0', 'name': 'collaboration_request', 'args': {}}])
        assert 'collaboration_transition' in names([asked], {'goal_id': 'g'})
        assert 'submit_task' not in names([]) and 'collaboration_transition' in names([])
        enabled = AIMessage(content='', tool_calls=[{'id': '1', 'name': 'enable_tools', 'args': {'groups': ['documents_more']}}])
        used = AIMessage(content='', tool_calls=[{'id': '2', 'name': 'collaboration_snapshot', 'args': {}}])
        assert GROUPS['documents_more'] <= names([enabled]) and 'collaboration_snapshot' in names([used])
        legacy = AIMessage(content='', tool_calls=[{'id': '3', 'name': 'enable_tools', 'args': {'names': ['collaboration_search']}}])
        assert 'collaboration_search' in names([legacy])
        everything = AIMessage(content='', tool_calls=[{'id': '4', 'name': 'enable_tools', 'args': {'groups': list(GROUPS)}}])
        assert 'enable_tools' not in names([everything])
        # Hidden tools stay executable; the model just does not pay for their schemas.
        assert GROUPS['team_more'] <= {t.name for t in tools}
        # Workers schedule colleague work with the slim request schema.
        request = next(t for t in tools if t.name == 'collaboration_request')
        assert 'needs' not in request.args_schema.model_fields and 'serial_reason' not in request.args_schema.model_fields
    finally:
        active.reset(token)


def test_layout_bound_generators_are_hidden_outside_their_layout(tmp_path):
    import json as _json
    from app.agents.tools._workspace import active_workspace, tool_available
    token = active_workspace.set(tmp_path)
    try:
        assert not tool_available('refresh_api_contract') and not tool_available('refresh_frontend_client')
        assert tool_available('execute_command')
        (tmp_path/'backend'/'scripts').mkdir(parents=True)
        (tmp_path/'backend'/'scripts'/'export_openapi.py').write_text('')
        (tmp_path/'frontend').mkdir()
        (tmp_path/'frontend'/'package.json').write_text(_json.dumps({'scripts': {'dev': 'vite'}}))
        assert tool_available('refresh_api_contract') and not tool_available('refresh_frontend_client')
        (tmp_path/'frontend'/'package.json').write_text(_json.dumps({'scripts': {'api:generate': 'openapi-ts'}}))
        assert tool_available('refresh_frontend_client')
    finally:
        active_workspace.reset(token)


def test_leader_tool_calls_are_persisted_once_with_arguments_and_errors(tmp_path, monkeypatch):
    from app.agents.team import Team
    from app.storage import ChatStore
    from app.collaboration.service import Collaboration
    from langgraph.checkpoint.memory import InMemorySaver
    async def scenario():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "leader-log.db"}')
        project = await store.create_project('P', str(tmp_path))
        service = Collaboration(store)
        await service.initialize()
        team = Team()
        await team.configure_persistence(store, InMemorySaver())
        bad = {'goal_id': 'missing', 'recipient': 'frontend', 'goal': 'Page', 'criteria': ['renders'], 'key': 'page'}
        async def reply(messages, **kwargs):
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content='', tool_calls=[{'name': 'create_task', 'id': 'call-1', 'args': bad}])
            return AIMessage(content='Recovered')
        monkeypatch.setattr(team.leader, 'next_message', reply)
        token = active.set(Context(service, project['id'], 'leader', '', 'ctx'))
        try:
            [event async for event in team._run([HumanMessage(content='Plan')], 'conv-1')]
            [event async for event in team._run([HumanMessage(content='Plan')], 'conv-1')]
        finally:
            active.reset(token)
        rows = await store.load_tool_calls('conv-1')
        assert len(rows) == 1 and rows[0]['tool'] == 'create_task' and rows[0]['status'] == 'error'
        assert json.loads(rows[0]['arguments'])['goal_id'] == 'missing' and 'error' in rows[0]['result']
        await store.close()
    asyncio.run(scenario())


def test_using_a_core_tool_keeps_the_tool_list_stable():
    """A changed tool list invalidates the prompt cache from the start, so using a tool
    the role always sees must not load the rest of its group."""
    from types import SimpleNamespace
    from app.agents.tool_profiles import GROUPS, select_tools
    tools = [SimpleNamespace(name=name) for name in sorted({n for g in GROUPS.values() for n in g} | {'enable_tools'})]
    call = lambda name: AIMessage(content='', tool_calls=[{'id': name, 'name': name, 'args': {}}])
    names = lambda role, *used: [t.name for t in select_tools(role, tools, [call(n) for n in used], {'goal_id': 'g'})]
    assert names('leader', 'create_task') == names('leader', 'create_task', 'collaboration_inspect_request')
    for role in ('backend', 'frontend'):
        before = names(role)
        assert 'manage_file' in before and 'list_project_files' not in before
        assert names(role, 'manage_file') == before
    # A hidden tool that was used still loads its group.
    assert 'collaboration_catalog' in names('backend', 'collaboration_search')
