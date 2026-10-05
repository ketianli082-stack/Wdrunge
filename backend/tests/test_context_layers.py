"""Layered context compaction: deterministic folding is lossless-by-pointer and
keeps decision facts, so model summaries are rarely needed and lose less."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents import context_fold
from app.agents.base import Agent, AgentSpec
from app.agents.context_fold import fold, is_ledger, supersession
from app.agents.memory import ContextMemory, message_tokens
from app.agents.tools.recall_context import make_recall_context, recall
from app.agents.tools.validation import ToolFailure
from app.agents.worker import WorkerAgent
from app.storage import ChatStore

counter = iter(range(10**6))


@pytest.fixture(autouse=True)
def small_tiers(monkeypatch):
    """Tiny verbatim tier so a few small turns exercise folding; every older turn is an excerpt."""
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '1000')
    monkeypatch.setenv('WDRUNGE_FOLD_EXCERPT_TOKENS', '1000000')


def kept(content):
    """The facts part of a folded result, without its recall note."""
    return content.split('\n[Excerpt')[0].split('\n[Folded')[0]


def turn(*calls):
    """One model turn: an AI message with tool calls and their results."""
    ids = [f'c{next(counter)}' for _ in calls]
    if not calls:
        return [AIMessage(content='thinking', id=f'ai-{next(counter)}')]
    ai = AIMessage(content='', id=f'ai-{ids[0]}', tool_calls=[{'id': i, 'name': name, 'args': args} for i, (name, args, _) in zip(ids, calls)])
    results = [ToolMessage(content=result if isinstance(result, str) else json.dumps(result), tool_call_id=i, id=f'r-{i}',
                           status='error' if isinstance(result, dict) and result.get('error') else 'success')
               for i, (_, _, result) in zip(ids, calls)]
    return [ai, *results]


def read(path, text, **args):
    return ('read_project_file', {'path': path, **args}, text)


def filler():
    """A turn larger than the test verbatim tier: everything before it is folded."""
    return turn(read('notes.txt', 'n' * 2400))


def command(argv, exit_code=0, output='', error=None, **args):
    result = {'id': f'p{next(counter)}', 'name': 'command', 'argv': argv, 'state': 'exited', 'exit_code': exit_code, 'output': output}
    if error:
        result.update(error=error, error_details={'code': 'connection_failed', 'category': 'service_unavailable'})
    return ('execute_command', {'command': argv[0], 'arguments': argv[1:], **args}, result)


def agent(window=131072):
    return Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md', context_window=window))


def by_id(messages):
    return {m.id: m for m in messages}


# --- layer 1 -------------------------------------------------------------------

def test_current_turn_is_never_folded_even_with_many_parallel_results():
    old = turn(read('a.py', 'A' * 5000))
    current = turn(*[('collaboration_read', {'document_id': 'd', 'section_id': f's{i}'}, {'content': 'S' * 3000}) for i in range(9)])
    body = [HumanMessage(content='task', id='task'), *old, *turn(read('b.py', 'B' * 10)), *turn(read('c.py', 'C' * 10)), *current]
    folded = by_id(fold(body))
    assert len(folded[old[1].id].content) < 1500 and 'recall_context(message_id="' + old[1].id in folded[old[1].id].content
    assert all(folded[m.id].content == m.content for m in current)


def test_superseded_results_become_stubs_but_the_current_turn_stays():
    first = turn(read('app.py', 'v1 ' * 400))
    partial = turn(read('app.py', 'part ' * 200, offset=200, limit=1000))
    full = turn(read('app.py', 'v1 ' * 400))
    probe_fail = turn(command(['curl', 'http://127.0.0.1:8765/'], 7, 'curl: (7) Failed to connect', error='Command exited 7'))
    probe_ok = turn(command(['curl', 'http://127.0.0.1:8765/'], 0, '{"message": "Hello"}'))
    write = turn(('write_project_file', {'path': 'app.py', 'content': 'v2'}, 'Wrote app.py (2 characters; sha256=abc).'))
    reread = turn(read('app.py', 'v2'))
    body = [HumanMessage(content='task', id='task'), *first, *partial, *full, *probe_fail, *probe_ok, *write, *reread]
    stubs = supersession(body, len(body) - 2)
    index = {m.id: i for i, m in enumerate(body)}
    assert 'newer full read of app.py' in stubs[index[first[1].id]] and index[partial[1].id] in stubs
    assert 'rewritten by message' in stubs[index[full[1].id]]
    assert 'Superseded by newer execute_command' in stubs[index[probe_fail[1].id]]
    assert index[probe_ok[1].id] not in stubs and index[reread[1].id] not in stubs
    # The newest result can never be stubbed by an even newer one in the same turn.
    assert supersession(body, index[reread[0].id]).get(index[reread[1].id]) is None


def test_digests_keep_the_facts_agents_act_on():
    long_output = '\n'.join([f'line {i}' for i in range(300)] + ['ERROR: port 8765 refused', 'done'])
    check = turn(command(['npm', 'test'], 1, long_output))
    page = turn(('browser_verify', {'url': 'http://localhost:5173/'}, {
        'url': 'http://localhost:5173/', 'title': 'Hello', 'status': 200, 'passed': False, 'expected_errors': False,
        'actions': [{'action': 'assert_text', 'selector': '.state', 'passed': True}], 'text': 'Hello World ' * 200,
        'console': [{'level': 'error', 'text': 'Failed to load resource'}] * 5, 'page_errors': ['GET /api: refused'] * 5,
        'http_errors': [], 'screenshot': 'docs/testing/browser-1.png', 'counts': {'console': 5},
        'error': 'Page verification found browser/HTTP errors', 'error_details': {'code': 'page_errors'}}))
    source = turn(read('frontend/app.js', '[sha256=deadbeef; characters=9000]\n' + 'x = 1\n' * 1500, include_hash=True))
    unknown = turn(('future_tool', {}, {'items': list(range(100)), 'note': 'n' * 2000}))
    text = turn(('other_tool', {}, 'plain ' * 400))
    body = [HumanMessage(content='task', id='task'), *check, *page, *source, *unknown, *text, *turn(), *turn(), *turn()]
    folded = by_id(fold(body))
    run = json.loads(kept(folded[check[1].id].content))
    assert run['exit_code'] == 1 and 'ERROR: port 8765 refused' in run['output'] and run['argv'] == 'npm test'
    browser = json.loads(kept(folded[page[1].id].content))
    assert browser['passed'] is False and browser['screenshot'] == 'docs/testing/browser-1.png'
    assert browser['actions'] == [{'action': 'assert_text', 'selector': '.state', 'passed': True}] and browser['code'] == 'page_errors'
    file = json.loads(kept(folded[source[1].id].content))
    assert file['path'] == 'frontend/app.js' and file['sha256'] == 'deadbeef' and file['lines'] == 1501
    assert json.loads(kept(folded[unknown[1].id].content))['items'][-1] == '…95 more'
    assert len(folded[text[1].id].content) <= 1500 and 'recall_context' in folded[text[1].id].content


def test_folded_call_arguments_cannot_be_sent_back_as_values():
    from app.agents.tools.validation import folded_call_failure
    [call] = context_fold.fold_call(AIMessage(content='', tool_calls=[
        {'id': 'w', 'name': 'write_project_file', 'args': {'path': 'backend/server.py', 'content': 'x = 1\n' * 800}}])).tool_calls
    [edit] = context_fold.fold_call(AIMessage(content='', tool_calls=[{'id': 'e', 'name': 'collaboration_publish', 'args': {
        'changes': [{'path': 'docs/README.md', 'edits': [{'old_text': 'a', 'new_text': 'y' * 1000}]}]}}])).tool_calls
    # A model copying either folded call from history is refused before anything is written.
    assert folded_call_failure(call['args'])['error_details']['code'] == 'folded_placeholder'
    assert folded_call_failure(edit['args'])['error'].startswith('changes.0.edits.0.new_text is a placeholder')
    # Real values that merely mention such words still pass.
    assert folded_call_failure({'path': 'notes.md', 'content': 'The file is on disk>, and 12 characters folded'}) is None
    assert folded_call_failure({'path': 'a.py', 'content': 'x' * 3000}) is None


def test_ledger_records_files_processes_checks_and_open_failures():
    body = [HumanMessage(content='task', id='task'),
            *turn(('write_project_file', {'path': 'backend/app.py', 'content': 'x' * 3000}, 'Wrote backend/app.py (3000 characters; sha256=abcdef1234567890ff).')),
            *turn(('execute_command', {'command': 'python3', 'arguments': ['backend/app.py'], 'background': True, 'ready_port': 8765},
                   {'id': 'svc1', 'name': 'api', 'argv': ['python3', 'backend/app.py'], 'state': 'running', 'ready': True})),
            *turn(command(['curl', 'http://127.0.0.1:8765/api/hello'], 0, '{"message": "Hello World"}')),
            *turn(command(['lsof', '-i'], 1, '', error='Command exited 1')),
            *turn(('browser_verify', {}, {'url': 'http://localhost:5173/', 'passed': True, 'screenshot': 'docs/testing/b.png'})),
            *filler()]
    folded = fold(body)
    ledger = next(m for m in folded if is_ledger(m))
    assert folded.index(ledger) == context_fold.verbatim_boundary(body)
    assert 'backend/app.py wrote sha256 abcdef1234567890' in ledger.content
    assert 'api (svc1) running ports [8765]' in ledger.content
    assert '`curl http://127.0.0.1:8765/api/hello` → exit 0' in ledger.content
    assert 'Unresolved tool failures: execute_command' in ledger.content and 'http://localhost:5173/ passed=True' in ledger.content
    # Folding again with nothing new keeps the very same ledger message.
    assert next(m for m in fold(folded) if is_ledger(m)) is ledger
    # A later success of the same check resolves the failure.
    later = [*folded, *turn(command(['lsof', '-i'], 0, 'node 123')), *filler()]
    assert 'Unresolved tool failures' not in next(m for m in fold(later) if is_ledger(m)).content


def test_fold_waits_for_a_large_batch_and_records_only_changed_messages(tmp_path):
    async def run():
        store = ChatStore(f'sqlite+aiosqlite:///{tmp_path}/fold.db')
        member = agent()
        member._store = store
        memory = ContextMemory(member, 'thread-1')
        small = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'),
                 *turn(read('a.py', 'a' * 4000)), *turn(), *turn(), *turn()]
        assert not (await memory.prepare(small)).changed
        big = [*small, *[m for i in range(6) for m in turn(read(f'f{i}.py', 'b' * 8000))], *turn(), *turn(), *turn()]
        prepared = await memory.prepare(big)
        assert prepared.stage == 'fold'
        record = (await store.load_compactions('thread-1'))[-1]
        changed = {m['data']['id'] for m in record['source']}
        assert 'task' not in changed and big[3].id in changed and len(changed) == 7
        # recall_context returns the exact original from the audit, scoped to the thread.
        original = await recall(store, 'thread-1', big[3].id)
        assert original.endswith('a' * 4000) and 'characters 4000' in original
        context_fold_marker = by_id(prepared.messages)[big[3].id].content
        assert f'recall_context(message_id="{big[3].id}")' in context_fold_marker
        from app.agents import memory as memory_module
        memory_module._ORIGINALS.clear()
        assert (await recall(store, 'thread-1', big[3].id, offset=3990, limit=5)).endswith('aaaaa')
        with pytest.raises(ToolFailure, match='No compacted message'):
            await recall(store, 'other-thread', big[3].id)
        tool = make_recall_context(lambda: store)
        result = await tool.ainvoke({'message_id': big[3].id, 'limit': 10}, {'configurable': {'thread_id': 'thread-1'}})
        assert 'next_offset=10' in result
        await store.close()
    asyncio.run(run())


class Summarizer:
    def __init__(self):
        self.inputs = []

    async def ainvoke(self, messages, **kwargs):
        self.inputs.append(messages)
        return AIMessage(content='Goal: API. Evidence: curl 200. Next: tests.',
                         usage_metadata={'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40})


def test_summary_keeps_the_task_recent_turns_and_ledger_verbatim():
    async def run():
        work = [m for i in range(12) for m in turn(
            ('write_project_file', {'path': f'src/m{i}.py', 'content': 'w' * 2400}, f'Wrote src/m{i}.py (2400 characters; sha256=ab{i:02d}).'),
            ('future_tool', {'q': i}, 'r' * 2400))]
        recent = [*turn(command(['pytest'], 0, '3 passed')), *turn(), *turn()]
        history = [SystemMessage(content='sys'), HumanMessage(content='Project handoff ' * 300, id='handoff'),
                   HumanMessage(content='Assigned task: build the API', id='task'), *work, *recent]
        # Folding alone fits a larger window (Layer 1 is enough there); this one needs a summary.
        member = agent(6000)
        assert (await ContextMemory(agent(24000), 'scope').prepare(history)).stage == 'fold'
        model = Summarizer()
        member.model = lambda: model
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'summary'
        messages = result.messages
        assert is_ledger(messages[1]) and 'src/m11.py wrote' in messages[1].content
        task = next(i for i, m in enumerate(messages) if m.id == 'task')
        assert messages[task - 1].additional_kwargs.get('context_summary')  # handoff history, summarized
        assert messages[task + 1].additional_kwargs.get('context_summary')  # older work since the task
        assert [m.id for m in messages[task + 2:]] == [m.id for m in recent]
        assert all('src/m11.py wrote' in call[0].content for call in model.inputs)
        assert message_tokens(messages) <= ContextMemory(member, 'scope').budget
    asyncio.run(run())


def test_clean_handoff_and_checkpoint_keep_the_ledger():
    async def run():
        member = agent(4000)
        member.model = lambda: Summarizer()
        history = [SystemMessage(content='sys'), HumanMessage(content='巨大的当前任务' * 1500, id='task'),
                   *turn(('write_project_file', {'path': 'a.py', 'content': 'x'}, 'Wrote a.py (1 characters; sha256=ff).'),
                         ('future_tool', {}, 'y' * 12000))]
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'clean' and is_ledger(result.messages[1]) and 'a.py wrote' in result.messages[1].content
        assert len(result.messages) == 3
        checkpoint = await ContextMemory(agent(), 'scope').checkpoint(history, 'Goal: finish')
        assert is_ledger(checkpoint.messages[1]) and 'Goal: finish' in checkpoint.messages[2].content
    asyncio.run(run())


def test_retry_handoff_carries_the_previous_attempts_facts():
    async def run():
        worker = WorkerAgent(AgentSpec(id='testing', name='Testing', prompt_file='testing.md'))
        messages = [HumanMessage(content='task', id='task'),
                    *turn(('write_project_file', {'path': 'docs/testing/report.txt', 'content': 'r'},
                           'Wrote docs/testing/report.txt (1 characters; sha256=12ab).')),
                    *turn(('browser_verify', {}, {'url': 'http://localhost:5173/', 'passed': True, 'screenshot': 'docs/testing/s.png'}))]
        class Saver:
            async def aget_tuple(self, config):
                assert config['configurable']['thread_id'] == 'old-thread'
                return SimpleNamespace(checkpoint={'channel_values': {'messages': messages}})
        worker._checkpointer = Saver()
        worker._threads['request'] = 'old-thread'
        progress = await worker.previous_progress('request')
        assert 'previous attempt' in progress and 'docs/testing/report.txt wrote' in progress
        assert 'docs/testing/s.png' in progress
        assert await worker.previous_progress('unknown') == ''
    asyncio.run(run())


def test_the_newest_outline_of_each_document_keeps_every_section_id_when_folded():
    outline = [{'id': f's{i}', 'title': f'Section {i}', 'level': 2, 'characters': 900} for i in range(40)]
    older = turn(('collaboration_read', {'document_id': 'design'}, {'document_id': 'design', 'version': 3, 'outline': outline[:30]}))
    publish = turn(('collaboration_publish', {'changes': [{'document_id': 'design', 'base_version': 3, 'summary': 'x'}]},
                    {'revisions': [{'document_id': 'design', 'version': 4, 'path': 'docs/design.md', 'characters': 36000,
                                    'outline': outline, 'budget': 15000}]}))
    body = [HumanMessage(content='task', id='task'), *older, *publish, *turn(), *turn(), *turn()]
    folded = by_id(fold(body))
    outline_kept = json.loads(kept(folded[publish[1].id].content))
    assert [s["id"] for s in outline_kept["revisions"][0]["outline"]] == [s['id'] for s in outline]
    assert outline_kept["revisions"][0]["budget"] == 15000
    # The older outline of the same document is folded to the usual short form.
    assert len(json.loads(kept(folded[older[1].id].content))['outline']) == 20


def test_folded_writes_and_reads_keep_the_file_structure():
    from app.agents.tools.validation import folded_call_failure
    source = 'import os\n\n\nclass Store:\n    def get(self, key):\n        return key\n\n\ndef create_app():\n    return 1\n' + '# pad\n' * 200
    [call] = context_fold.fold_call(AIMessage(content='', tool_calls=[
        {'id': 'w', 'name': 'write_project_file', 'args': {'path': 'backend/app.py', 'content': source}}])).tool_calls
    placeholder = call['args']['content']
    assert 'L4 class Store:' in placeholder and 'L5 def get(self, key):' in placeholder and 'L9 def create_app():' in placeholder
    assert folded_call_failure(call['args'])['error_details']['code'] == 'folded_placeholder'
    body = [HumanMessage(content='task', id='task'), *(page := turn(read('backend/app.py', '[sha256=ab12; characters=9]\n' + source))),
            *filler()]
    digest = json.loads(kept(by_id(fold(body))[page[1].id].content))
    assert 'L9 def create_app():' in digest['structure']
