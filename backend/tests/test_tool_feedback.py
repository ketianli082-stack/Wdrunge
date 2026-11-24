"""Recovery information must reach the model through the actual graph boundary."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, MessagesState, START, END

from app.agents.base import AgentSpec
from app.agents.worker import WorkerAgent
from app.agents.tools._workspace import active_workspace
from app.agents.tools.validation import command_failure, exception_result, tool_error
from app.storage import StorageConflictError
from app.agents.tools.write_project_file import make_write_project_file


def test_edits_sent_as_a_json_encoded_string_are_decoded_instead_of_refused(tmp_path, monkeypatch):
    """A known habit of Qwen. Refusing it cost a model turn for a call whose meaning was never in doubt."""
    target = tmp_path / 'a.txt'
    target.write_text('original')
    write = make_write_project_file(('.',))
    calls = []

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            calls.append(results)
            if not results:
                args = {'path': 'a.txt', 'edits': json.dumps([{'old_text': 'original', 'new_text': 'updated'}])}
            elif len(results) == 1:
                assert results[-1].status == 'success' and target.read_text() == 'updated'
                # A string that is not a list still fails, naming the field with one example.
                args = {'path': 'a.txt', 'edits': 'replace updated with final'}
            elif len(results) == 2:
                assert results[-1].status == 'error' and target.read_text() == 'updated'
                failure = json.loads(results[-1].content)['error_details']
                assert failure['category'] == 'invalid_arguments' and 'retry_policy' not in failure and 'tool' not in failure
                field = failure['fields'][0]
                assert field['field'] == 'edits' and isinstance(field['example'], list) and 'expected_schema' not in field
                args = {'path': 'a.txt', 'edits': [{'old_text': 'updated', 'new_text': 'final'}]}
            else:
                assert results[-1].status == 'success'
                return AIMessage(content='Recovered')
            return AIMessage(content='', tool_calls=[{'id': f'edit-{len(results)}', 'name': write.name, 'args': args}])

    worker = WorkerAgent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md', tools=(write,)))
    monkeypatch.setattr(worker, 'model', lambda: Model())

    async def scenario():
        token = active_workspace.set(tmp_path)
        try:
            result = await worker.build_graph().ainvoke({'messages': [HumanMessage(content='Edit the file')]},
                {'configurable': {'thread_id': 'feedback'}, 'recursion_limit': 30})
            assert result['messages'][-1].content == 'Recovered'
            assert target.read_text() == 'final'
            assert len(calls) == 4
        finally:
            active_workspace.reset(token)
    asyncio.run(scenario())


def test_nested_missing_field_reports_its_structure_without_input_contents(tmp_path):
    write = make_write_project_file(('.',))
    async def scenario():
        builder = StateGraph(MessagesState)
        builder.add_node('tools', ToolNode([write], handle_tool_errors=tool_error))
        builder.add_edge(START, 'tools')
        builder.add_edge('tools', END)
        result = await builder.compile().ainvoke({'messages': [AIMessage(content='',
            tool_calls=[{'id': 'bad', 'name': write.name, 'args': {
                'path': 'a.txt', 'edits': [{'old_text': 'PRIVATE ORIGINAL TEXT'}]}}])]})
        message = result['messages'][-1]
        field = json.loads(message.content)['error_details']['fields'][0]
        assert message.status == 'error'
        assert field['field'] == 'edits.0.new_text' and field['message'] == 'Field required'
        assert field['example'] == '<text>' and 'expected_schema' not in field
        assert 'PRIVATE ORIGINAL TEXT' not in message.content
    asyncio.run(scenario())


@pytest.mark.parametrize('error,category,advised', [
    (ConnectionRefusedError('Connection refused'), 'service_unavailable', True),
    (TimeoutError('Service timed out'), 'service_unavailable', True),
    (PermissionError('User denied access'), 'permission_denied', True),
    (TypeError("object ServerWebSocketRoute can't be used in await expression"), 'internal_error', True),
    (StorageConflictError('Dependency changed: doc is v3'), 'operation_rejected', True),
    # The message of a rejected value is the diagnosis: no generic advice is attached.
    (ValueError('old_text must match exactly once'), 'operation_rejected', False),
])
def test_runtime_errors_do_not_tell_model_to_fix_valid_argument_types(error, category, advised):
    result = exception_result(error, tool_name='some_tool')
    assert result['error'] == str(error)
    assert result['error_details']['category'] == category
    assert ('next_action' in result['error_details']) == advised
    # Nothing the model cannot act on: no tool name it just called, no exception class, no policy label.
    assert not {'fields', 'tool', 'exception_type', 'retry_policy'} & set(result['error_details'])


@pytest.mark.parametrize('output,category,code', [
    ('curl: (7) Failed to connect: Could not connect to server', 'service_unavailable', 'connection_failed'),
    ('Error: listen EADDRINUSE: address already in use', 'service_unavailable', 'port_in_use'),
    ('Error: listen EPERM: operation not permitted', 'permission_denied', 'os_permission_denied'),
    ('AssertionError: expected 200, got 400', 'command_failed', 'nonzero_exit'),
    ('', 'command_failed', 'nonzero_exit'),
])
def test_command_feedback_uses_logs_without_inventing_an_outage(output, category, code):
    result = command_failure('Command exited 1', output)
    assert result['error_details']['category'] == category
    assert result['error_details']['code'] == code
    # Blog5: 27 non-zero exits each carried the same sentence of advice.
    assert ('next_action' in result['error_details']) == (code != 'nonzero_exit')


def test_browser_classifies_navigation_action_and_internal_failures():
    pytest.importorskip('playwright')
    from playwright.async_api import Error, TimeoutError
    from app.execution.browser_runner import browser_failure, AssertionFailed
    navigation = browser_failure(Error('Page.goto: net::ERR_CONNECTION_REFUSED'), 'navigation')
    action = browser_failure(TimeoutError('Locator.wait_for: timeout'), 'action', 2)
    internal = browser_failure(TypeError('invalid await'), 'action', 0)
    assertion = browser_failure(AssertionFailed('Expected Hello; got Error'), 'action', 1)
    assert navigation['error_details']['category'] == 'service_unavailable'
    assert action['error_details']['code'] == 'action_timeout'
    assert action['error_details']['action_index'] == 2
    assert internal['error_details']['category'] == 'internal_error'
    assert assertion['error_details']['code'] == 'assertion_failed'
    # The selector's own match count separates "not on the page" from "there, but not usable".
    from app.execution.browser_runner import SelectorProblem
    absent = browser_failure(TimeoutError('Locator.click: Timeout 10000ms exceeded.'), 'action', 0, matches=0)
    blocked = browser_failure(TimeoutError('Locator.click: Timeout 10000ms exceeded.'), 'action', 0, matches=1)
    several = browser_failure(SelectorProblem('click needs one element', 'ambiguous_selector', 'append >> nth=0'), 'action', 3)
    assert absent['error_details']['code'] == 'selector_not_found' and '"dom"' in absent['error_details']['next_action']
    assert blocked['error_details']['code'] == 'action_timeout' and blocked['error_details']['selector_matches'] == 1
    assert several['error_details'] == {'category': 'invalid_arguments', 'code': 'ambiguous_selector', 'next_action': 'append >> nth=0',
                                        'stage': 'action', 'action_index': 3}


def test_worker_reports_service_failure_with_error_status_and_preserves_diagnostics(monkeypatch):
    from app.agents.tools.validation import tool, failure_result
    @tool
    def service_check() -> dict:
        """Check an unavailable local service."""
        return {**failure_result('Connection refused', 'service_unavailable', 'connection_failed',
            'Check process logs and readiness.', 'after_service_ready'), 'output': 'curl: connection refused'}
    calls = []
    class Model:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[{'id': 'service', 'name': service_check.name, 'args': {}}])
            reply = messages[-1]
            assert isinstance(reply, ToolMessage) and reply.status == 'error'
            result = json.loads(reply.content)
            assert result['output'] == 'curl: connection refused'
            assert result['error_details']['category'] == 'service_unavailable'
            assert result['error_details']['next_action'] == 'Check process logs and readiness.'
            return AIMessage(content='Inspect readiness instead of correcting valid parameters')
    worker = WorkerAgent(AgentSpec(id='testing', name='Testing', prompt_file='testing.md', tools=(service_check,)))
    monkeypatch.setattr(worker, 'model', lambda: Model())
    async def scenario():
        result = await worker.build_graph().ainvoke({'messages': [HumanMessage(content='Verify')]},
            {'configurable': {'thread_id': 'service-feedback'}})
        assert result['messages'][-1].content.startswith('Inspect readiness')
        assert len(calls) == 2
    asyncio.run(scenario())


def test_leader_receives_field_schema_when_delegation_validation_fails(monkeypatch):
    from app.agents.team import Team
    team = Team()
    async def reply(messages, **kwargs):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return AIMessage(content='', tool_calls=[{'id': 'delegate', 'name': 'delegate_task',
                'args': {'worker': 'not-a-role', 'task': 'work', 'summary': 'work', 'handoff': 'context'}}])
        assert results[-1].status == 'error'
        field = json.loads(results[-1].content)['error_details']['fields'][0]
        assert field['field'] == 'worker' and "'backend'" in field['message']
        return AIMessage(content='Correct the role')
    monkeypatch.setattr(team.leader, 'next_message', reply)
    async def scenario():
        events = [event async for event in team._run([HumanMessage(content='Assign work')], 'leader-feedback')]
        assert any(kind == 'delta' and data['content'] == 'Correct the role' for kind, data in events)
    asyncio.run(scenario())


def test_legacy_collaboration_feedback_includes_nested_field_schema():
    from app.collaboration.tools import Context, active, collaborate
    async def scenario():
        token = active.set(Context(object(), 'project', 'leader', 'conversation', 'context'))
        try:
            result = await collaborate.ainvoke({'action': 'request', 'payload': {
                'recipient': 'backend', 'goal': 'work', 'key': 'stable', 'dependencies': '["request-id"]'}})
            field = result['error_details']['fields'][0]
            assert result['code'] == 'invalid_arguments'
            assert field['field'] == 'dependencies' and 'valid list' in field['message']
            assert field['example'] == ['<text>']
        finally:
            active.reset(token)
    asyncio.run(scenario())


def test_explicit_command_timeout_takes_precedence_over_earlier_log_errors():
    result = command_failure('Command timed out', 'Earlier attempt: connection refused', timed_out=True)
    assert result['error_details']['category'] == 'timeout'


def test_protected_config_is_permission_failure_not_a_relative_path_hint(tmp_path):
    from app.agents.tools._workspace import resolve_allowed
    token = active_workspace.set(tmp_path)
    try:
        with pytest.raises(ValueError) as rejected:
            resolve_allowed('frontend/.env.local', ('.',))
        result = exception_result(rejected.value, tool_name='write_project_file')
        assert result['error_details']['category'] == 'permission_denied'
        assert result['error_details']['code'] == 'protected_path'
        assert 'Do not bypass' in result['error_details']['next_action']
        assert '.env.example' in result['error_details']['next_action']
        assert resolve_allowed('frontend/.env.example', ('.',)) == tmp_path/'frontend'/'.env.example'
    finally:
        active_workspace.reset(token)


def test_missing_project_python_is_environment_failure(tmp_path):
    from app.execution.environment import verification_python
    with pytest.raises(ValueError) as rejected:
        verification_python(tmp_path)
    result = exception_result(rejected.value)
    assert result['error_details']['category'] == 'environment_unavailable'
    assert result['error_details']['code'] == 'missing_project_python'
