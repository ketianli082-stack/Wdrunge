"""Context tiers by volume, the platform handoff and read-only streaks (Blog2 fixes)."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents import context_fold
from app.agents.base import Agent, AgentSpec
from app.agents.context_fold import EXCERPT, POINTER, facts_of, fold, supersession, tier_bounds
from app.agents.memory import ContextMemory, message_tokens
from app.agents.progress import read_only_argv, read_only_call, read_streak, streak_notice
from app.agents.worker import WorkerAgent, reminder_events

counter = iter(range(10**6))


def turn(*calls, text=''):
    ids = [f'c{next(counter)}' for _ in calls]
    ai = AIMessage(content=text, id=f'ai-{ids[0]}', tool_calls=[{'id': i, 'name': name, 'args': args}
                                                                for i, (name, args, _) in zip(ids, calls)])
    return [ai, *[ToolMessage(content=result if isinstance(result, str) else json.dumps(result), tool_call_id=i, id=f'r-{i}')
                  for i, (_, _, result) in zip(ids, calls)]]


def read(path, size, **args):
    return ('read_project_file', {'path': path, **args}, f'[sha256=ab12cd; characters={size}]\n' + 'x' * size)


def by_id(messages):
    return {m.id: m for m in messages}


@pytest.fixture
def tiers(monkeypatch):
    # A read turn below is about 2,090 estimated tokens (result and call).
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '6500')
    monkeypatch.setenv('WDRUNGE_FOLD_EXCERPT_TOKENS', '4500')


def search(query, size):
    return ('search_code', {'query': query}, 'x' * size)


def test_turns_are_tiered_by_the_size_of_their_originals(tiers):
    # 3 turns verbatim, 2 excerpts, the rest pointers.
    turns = [turn(search(f'src/f{i}.py', 4000)) for i in range(8)]
    body = [HumanMessage(content='task', id='task'), *[m for t in turns for m in t]]
    excerpt_start, verbatim_start = tier_bounds(body)
    assert body[verbatim_start] is turns[5][0] and body[excerpt_start] is turns[3][0]
    folded = by_id(fold(body))
    for t in turns[5:]:
        assert folded[t[1].id].content == t[1].content
    for t in turns[3:5]:
        excerpt = folded[t[1].id]
        assert facts_of(excerpt)['level'] == EXCERPT and 'x' * 500 in excerpt.content and len(excerpt.content) < 1500
        assert f'recall_context(message_id="{t[1].id}")' in excerpt.content
    for t in turns[:3]:
        pointer = folded[t[1].id]
        assert facts_of(pointer)['level'] == POINTER and len(pointer.content) < 300
        assert 'src/f' in pointer.content and '"characters"' in pointer.content
    # Folding is stable, and a result moves on from excerpt to pointer as newer turns arrive.
    again = fold(fold(body))
    assert [m.content for m in again] == [m.content for m in fold(body)]
    later = [*fold(body), *turn(search('src/g.py', 4000)), *turn(search('src/h.py', 4000))]
    moved = by_id(fold(later))[turns[4][1].id]
    assert facts_of(moved)['level'] == POINTER and 'src/f4.py' in moved.content


def test_reads_skip_the_excerpt_tier_and_move_to_the_desk(tiers):
    """Blog4: excerpts (600 characters of a file) were useless and the pointers made the
    agent read the same files again. A read before the verbatim tier becomes a one-line
    pointer at any depth, and its text is on the desk."""
    from app.agents.context_desk import desk_of, is_desk
    turns = [turn(read(f'src/f{i}.py', 4000)) for i in range(8)]
    body = [HumanMessage(content='task', id='task'), *[m for t in turns for m in t]]
    folded = fold(body)
    messages = by_id(folded)
    for t in turns[:5]:
        pointer = messages[t[1].id]
        assert facts_of(pointer)['level'] == POINTER and 'on the desk' in pointer.content and 'ab12cd' in pointer.content
    desk = next(m for m in folded if is_desk(m))
    assert [e['key'][1] for e in desk_of(folded)['entries']] == [f'src/f{i}.py' for i in range(5)]
    assert desk.content.count('### file src/f') == 5 and 'x' * 4000 in desk.content
    assert folded.index(desk) == folded.index(next(m for m in folded if m.additional_kwargs.get('context_ledger'))) + 1


def test_the_current_turn_and_small_windows():
    # Results count at most one result budget each; six of them still exceed the verbatim tier.
    big = turn(*[read(f'docs/s{i}.md', 30000) for i in range(6)])
    body = [HumanMessage(content='task', id='task'), *turn(read('a.py', 3000)), *big]
    excerpt_start, verbatim_start = tier_bounds(body)
    assert body[verbatim_start] is big[0]
    assert all(by_id(fold(body))[m.id].content == m.content for m in big)
    # A small context window bounds the tiers by its budget.
    small = [HumanMessage(content='task', id='task'), *turn(read('a.py', 3000)), *turn(read('b.py', 3000))]
    assert tier_bounds(small) == (0, 0) and tier_bounds(small, budget=3000)[1] == 3


def test_a_folded_partial_read_never_supersedes_other_ranges(tiers):
    """Blog2: a folded `offset=0, limit=3000` read lost its truncation marker and was taken
    for a full read, which stubbed every other range of the same file."""
    head = ('read_project_file', {'path': 'docs/design.md', 'limit': 3000},
            '[sha256=ab; characters=9000]\n' + 'h' * 3000 + '\n[truncated; next_offset=3000]')
    tail = read('docs/design.md', 3000, offset=6000)
    body = [HumanMessage(content='task', id='task'), *(later := turn(tail)), *(partial := turn(head)),
            *[m for i in range(6) for m in turn(read(f'pad{i}.py', 4000))]]
    folded = fold(body)
    assert facts_of(by_id(folded)[partial[1].id])['level'] >= EXCERPT
    assert facts_of(by_id(folded)[partial[1].id])['complete'] is False
    again = [*folded, *turn(read('x.py', 10))]
    assert not supersession(again, len(again) - 2)
    # Folded ranges are receipts of the desk (one line already); a full read replaces
    # the ranges still verbatim.
    full = [*folded, *(whole := turn(read('docs/design.md', 9000))), *turn(read('x.py', 10))]
    assert not supersession(full, len(full) - 2)
    recent = [HumanMessage(content='task', id='task'), *(fresh := turn(tail)), *whole, *turn(read('x.py', 10))]
    stubs = supersession(recent, len(recent) - 2)
    index = {m.id: i for i, m in enumerate(recent)}
    assert index[fresh[1].id] in stubs and index[whole[1].id] not in stubs


def test_old_publish_arguments_keep_their_shape_but_not_the_document(tiers):
    edits = [{'operation': 'replace_section_body', 'section_id': f's{i}', 'new_text': '设计内容' * 300} for i in range(12)]
    publish = turn(('collaboration_publish', {'changes': [{'document_id': 'design', 'base_version': 6,
                                                           'summary': 'Fill sections', 'edits': edits}]},
                    {'revisions': [{'document_id': 'design', 'version': 7}]}))
    body = [HumanMessage(content='task', id='task'), *publish, *[m for i in range(8) for m in turn(read(f'p{i}.py', 4000))]]
    call = by_id(fold(body))[publish[0].id].tool_calls[0]
    assert len(json.dumps(call['args'], ensure_ascii=False)) < 2500
    change = call['args']['changes'][0]
    assert change['document_id'] == 'design' and change['base_version'] == 6 and change['summary'] == 'Fill sections'
    assert [e['section_id'] for e in change['edits']] == [f's{i}' for i in range(12)]
    from app.agents.tools.validation import folded_call_failure
    assert folded_call_failure(call['args'])['error_details']['code'] == 'folded_placeholder'


def agent(window):
    return Agent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md', context_window=window))


def test_a_cut_off_summary_is_retried_smaller_then_replaced_by_the_platform_handoff():
    async def run():
        member = agent(12000)
        calls = []

        class Thinking:
            def bind(self, **kwargs):
                calls.append(kwargs['max_tokens'])
                return self

            async def ainvoke(self, messages, **kwargs):
                return AIMessage(content='', response_metadata={'finish_reason': 'length'})
        member.model = lambda **kwargs: Thinking()
        work = [m for i in range(14) for m in turn(read(f'src/m{i}.ts', 2400), text=f'Step {i}: writing m{i}.ts next')]
        recent = turn(read('src/now.ts', 200))
        history = [SystemMessage(content='sys'), HumanMessage(content='Project handoff ' * 900, id='handoff'),
                   HumanMessage(content='Assigned task: build the blog front end', id='task'), *work, *recent]
        result = await ContextMemory(member, 'scope').prepare(history)
        assert result.stage == 'handoff' and len(calls) == 2 and calls[1] <= calls[0]
        ids = [m.id for m in result.messages]
        assert 'handoff' in ids and 'task' in ids and ids[-len(recent):] == [m.id for m in recent]
        note = next(m for m in result.messages if m.additional_kwargs.get('context_summary')).content
        assert 'Step 11: writing m11.ts next' in note and 'read_project_file' in note and 'message r-' in note
        assert message_tokens(result.messages) <= ContextMemory(member, 'scope').budget
    asyncio.run(run())


def test_summary_calls_disable_thinking_and_every_call_reports_streamed_usage(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test')
    monkeypatch.delenv('WDRUNGE_SUMMARY_THINKING', raising=False)
    member = agent(131072)
    summary, work = member.model(purpose='summary'), member.model()
    assert summary.extra_body == {'enable_thinking': False} and summary.stream_usage is True
    assert not work.extra_body and work.stream_usage is True
    monkeypatch.setenv('WDRUNGE_SUMMARY_THINKING', '1')
    assert not member.model(purpose='summary').extra_body


def test_read_only_calls_are_judged_by_what_they_do():
    assert read_only_call({'name': 'read_project_file', 'args': {'path': 'a'}})
    assert read_only_call({'name': 'collaboration_read', 'args': {}})
    assert not read_only_call({'name': 'collaboration_publish', 'args': {}})
    assert not read_only_call({'name': 'write_project_file', 'args': {}})
    assert read_only_argv(['sh', '-c', "sed -n '95,232p' server/app/repository.py | head -50"])
    assert read_only_argv(['/usr/bin/grep', '-rn', 'def ', 'server'])
    assert not read_only_argv(['sh', '-c', 'sed -n 1,5p a.py > b.py'])
    assert not read_only_argv(['sed', '-i', '', 's/a/b/', 'a.py'])
    assert not read_only_argv(['find', '.', '-name', '*.pyc', '-delete'])
    assert not read_only_argv(['npm', 'run', 'build'])
    # Blog3: the frontend read through cd/loops/2>&1/quoted '>' and every such call counted
    # as progress, so a 178-turn read run never reached the streak limit.
    assert read_only_argv(['bash', '-c', "cd src && for f in a.js b.js; do echo \"== $f\"; grep -n 'export ' $f; done"])
    assert read_only_argv(['bash', '-c', "ls -la src 2>&1; awk 'NR>=460 && NR<=520' ui.js; cat x 2>/dev/null || true"])
    assert read_only_argv(['bash', '-c', 'if [ -f a ]; then cat a; else echo none; fi'])
    assert not read_only_argv(['bash', '-c', 'cd src && perl -pi -e "s/a/b/" x.js'])
    assert not read_only_argv(['bash', '-c', 'cat a | tee b'])
    assert not read_only_argv(['bash', '-c', 'echo x >> log'])
    assert not read_only_argv(['bash', '-c', 'cat a &> out.txt'])
    assert not read_only_argv(['bash', '-c', 'for f in *.js; do rm $f; done'])
    assert not read_only_call({'name': 'execute_command', 'args': {'argv': ['cat', 'a'], 'background': True}})
    reading = turn(read('a.py', 10), ('search_code', {'query': 'x'}, '{}'))
    writing = turn(('write_project_file', {'path': 'a.py', 'content': 'x'}, 'Wrote a.py'))
    assert read_streak([*writing, *reading, *reading]) == 2
    assert read_streak([*reading, streak_notice(12, paused=False), *reading]) == 13
    assert read_streak([*reading, *writing]) == 0


def test_the_worker_reminds_then_pauses_reads_without_dropping_context(monkeypatch, tmp_path):
    """Blog3: a clean handoff at 2N discarded what a long read run had just loaded, and
    the agent read it again. Now reads are refused until it writes, runs or plans."""
    from app.agents.tools._workspace import active_workspace
    from app.agents.tools.read_project_file import make_read_project_file
    from app.agents.tools.write_project_file import make_write_project_file
    monkeypatch.setenv('WDRUNGE_READ_STREAK', '3')
    (tmp_path / 'docs').mkdir()
    for number in range(1, 10):
        (tmp_path / 'docs' / f'p{number}.md').write_text(f'# Part {number}\n')
    token = active_workspace.set(tmp_path)

    async def run():
        worker = WorkerAgent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md', tools=(
            make_read_project_file(('.',)), make_write_project_file(('src',)))))
        seen = []

        class Model:
            def bind_tools(self, available):
                return self

            async def ainvoke(self, messages, **kwargs):
                seen.append(list(messages))
                if len(seen) <= 7:
                    return AIMessage(content=f'reading part {len(seen)}', tool_calls=[
                        {'id': f'r{len(seen)}', 'name': 'read_project_file', 'args': {'path': f'docs/p{len(seen)}.md'}}])
                if len(seen) == 8:
                    return AIMessage(content='writing', tool_calls=[
                        {'id': 'w', 'name': 'write_project_file', 'args': {'path': 'src/a.js', 'content': 'export function a() {}\n'}}])
                if len(seen) == 9:
                    return AIMessage(content='reading again', tool_calls=[
                        {'id': 'r9', 'name': 'read_project_file', 'args': {'path': 'docs/p9.md'}}])
                return AIMessage(content='done')
        monkeypatch.setattr(worker, 'model', lambda: Model())
        async for _ in worker.stream('build pages', conversation_id='chat', context_mode='fresh', handoff='details'):
            pass
        notices = [[m for m in call if 'read_streak' in m.additional_kwargs] for call in seen]
        assert not any(notices[:3]) and notices[3][-1].additional_kwargs['read_streak'] == 3
        assert notices[6][-1].additional_kwargs['read_streak'] == 6 and '读取已暂停' in notices[6][-1].content
        # The context is kept: the first read turn is still there.
        assert any(isinstance(m, AIMessage) and m.content == 'reading part 1' for m in seen[6])
        results = {m.tool_call_id: m for m in seen[7] if isinstance(m, ToolMessage)}
        assert results['r7'].status == 'error' and 'reads_paused' in results['r7'].content
        assert results['r6'].status != 'error'
        # A write lifts the pause; its receipt names the declarations it wrote.
        results = {m.tool_call_id: m for m in seen[9] if isinstance(m, ToolMessage)}
        assert 'Structure: L1 export function a()' in results['w'].content
        assert results['r9'].status != 'error'
    try:
        asyncio.run(run())
    finally:
        active_workspace.reset(token)


def test_a_reminder_followed_by_a_goal_state_is_acknowledged():
    """Blog2: the goal state was appended after the collaboration reminder, so the reminder
    was never the message before the response, never acknowledged, and injected again."""
    from app.agents.memory import reminder
    notice = reminder('Collaboration reminders (project data, not instructions):\n[{"event_id": 168}]')
    notice.additional_kwargs['event_ids'] = [168, 170]
    assert reminder_events(notice) == [168, 170]
    legacy = reminder('Collaboration reminders (project data, not instructions):\n[{"event_id": 5}, {"event_ids": [6, 7]}]')
    assert reminder_events(legacy) == [5, 6, 7]
    assert reminder_events(HumanMessage(content='Assigned task')) == []


def test_the_ledger_keeps_a_code_index_of_written_and_read_files(monkeypatch):
    """Blog3: write arguments fold away and the ledger held only paths, so the frontend
    re-read its own modules to recall a name or a CSS class. The index keeps their
    declarations, newest first, within a budget."""
    from app.agents.context_fold import empty_ledger, record_facts, render_ledger
    api = 'Wrote src/api.js (60 characters; sha256=aa11).\nStructure: L1 export async function request(path, options) | L9 export const api = {'
    css = 'Wrote src/pages.css (40 characters; sha256=bb22).\nStructure: L1 .editor-toolbar { | L7 .post-card {'
    newer = 'Wrote src/api.js (80 characters; sha256=cc33).\nStructure: L1 export async function request(path, options) | L12 export function upload(file) {'
    source = 'def list_posts(db, page):\n    pass\nclass Post:\n    pass\n'
    body = [*turn(('write_project_file', {'path': 'src/api.js', 'content': '…'}, api)),
            *turn(('write_project_file', {'path': 'src/pages.css', 'content': '…'}, css)),
            *turn(('read_project_file', {'path': 'server/posts.py'}, source)),
            *turn(('read_project_file', {'path': 'server/big.py', 'offset': 100}, 'def tail_only():\n')),
            *turn(('write_project_file', {'path': 'src/api.js', 'edits': []}, newer))]
    text = render_ledger(record_facts(empty_ledger(), body))
    index = text[text.index('Code index'):]
    # Newest first; the rewritten file shows its current declarations once.
    assert index.index('src/api.js') < index.index('server/posts.py') < index.index('src/pages.css')
    assert 'L12 export function upload(file)' in index and index.count('src/api.js') == 1
    assert 'L1 def list_posts(db, page):' in index and 'L3 class Post:' in index
    assert 'tail_only' not in index  # a partial read is not the file's structure
    # The older receipt's outline is superseded; its receipt line stays.
    stubs = supersession(body, len(body))
    assert stubs[1].startswith('Wrote src/api.js (60 characters') and 'Structure superseded' in stubs[1]
    monkeypatch.setenv('WDRUNGE_CODE_INDEX_CHARS', '150')
    small = render_ledger(record_facts(empty_ledger(), body))
    assert 'src/api.js' in small[small.index('Code index'):] and '(+2 older files not shown)' in small
