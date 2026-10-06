"""The desk (working set), the token meter and the bound on work-segment summaries
(Blog4: folded reads were read again every few minutes; the estimate overstated tokens
1.55x; segment summaries grew without bound)."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents import context_desk, context_fold, memory as memory_module
from app.agents.base import Agent, AgentSpec
from app.agents.context_desk import desk_of, is_desk
from app.agents.context_fold import STUB, desk_hit_of, facts_of, fold, ingest
from app.agents.context_meter import RATIO_KEY, calibrate, ratio_of
from app.agents.memory import ContextMemory, message_tokens

counter = iter(range(10**6))


def turn(*calls, text='', ratio=None):
    ids = [f'c{next(counter)}' for _ in calls]
    metadata = {RATIO_KEY: ratio} if ratio else {}
    if not calls:
        return [AIMessage(content=text or 'thinking', id=f'ai-{next(counter)}', response_metadata=metadata)]
    ai = AIMessage(content=text, id=f'ai-{ids[0]}', response_metadata=metadata,
                   tool_calls=[{'id': i, 'name': name, 'args': args} for i, (name, args, _) in zip(ids, calls)])
    return [ai, *[ToolMessage(content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                              tool_call_id=i, id=f'r-{i}',
                              status='error' if isinstance(result, dict) and result.get('error') else 'success')
                  for i, (_, _, result) in zip(ids, calls)]]


def read(path, body, **args):
    return 'read_project_file', {'path': path, **args}, body


def section(document, section_id, content, version=3):
    return ('collaboration_read', {'document_id': document, 'section_id': section_id, 'version': version},
            {'document_id': document, 'version': version, 'section': {'id': section_id, 'title': f'Part {section_id}'},
             'content': content, 'next_offset': None, 'fragment': False})


def filler(n=3):
    return [m for i in range(n) for m in turn(('search_code', {'query': f'pad{next(counter)}'}, 'p' * 3000))]


def agent(window=131072):
    return Agent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md', context_window=window))


def by_id(messages):
    return {m.id: m for m in messages}


@pytest.fixture(autouse=True)
def small_tiers(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '2000')
    monkeypatch.setenv('WDRUNGE_FOLD_EXCERPT_TOKENS', '0')


@pytest.fixture
def rewrite_now(monkeypatch):
    """Moving reads to the desk saves no tokens by itself, so rent-or-buy would wait for
    other savings; these tests look at the desk, not at the timing of rewrites."""
    monkeypatch.setattr(ContextMemory, 'worth_rewriting', lambda self, before, after, cold: True)


class Files:
    """A refresher over an in-memory project: reads return the current text."""
    def __init__(self, **files):
        self.files, self.calls = dict(files), []

    async def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 'read_project_file':
            text = self.files[args['path']]  # KeyError: the file is gone
            offset, limit = args.get('offset') or 0, args.get('limit') or 30000
            excerpt = text[offset:offset + limit]
            return excerpt + (f'\n[truncated; next_offset={offset + limit}]' if len(text) > offset + limit else '')
        if name == 'collaboration_read':
            document = self.files[args['document_id']]
            if args.get('section_id'):
                return {'document_id': args['document_id'], 'version': document['version'],
                        'section': {'id': args['section_id'], 'title': f"Part {args['section_id']}"},
                        'content': document['sections'][args['section_id']], 'next_offset': None, 'fragment': False}
            return {'document_id': args['document_id'], 'version': document['version'],
                    'outline': [{'id': s, 'title': f'Part {s}', 'level': 2} for s in document['sections']]}
        return None


def test_folded_reads_move_to_the_desk_and_reading_them_again_returns_a_notice():
    a, b = turn(read('src/a.ts', 'export const a = 1;\n' * 50)), turn(read('src/b.ts', 'export const b = 2;\n' * 50))
    body = [HumanMessage(content='task', id='task'), *a, *b, *filler()]
    folded = fold(body)
    desk = next(m for m in folded if is_desk(m))
    assert 'on the desk' in by_id(folded)[a[1].id].content and 'export const a = 1;' in desk.content
    assert [e['key'] for e in desk_of(folded)['entries']] == [['file', 'src/a.ts', 0, 30000], ['file', 'src/b.ts', 0, 30000]]
    # Reading a.ts again: the result is already shown, so the model gets a one-line notice.
    again = turn(read('src/a.ts', 'export const a = 1;\n' * 50))
    result = ingest([*folded, *again])[0]
    notice = json.loads(result[-1].content)
    assert notice['on_desk'] is True and notice['entry'] == 'file src/a.ts' and desk_hit_of(result[-1]) == ['file', 'src/a.ts', 0, 30000]
    # A range of a whole file on the desk is a hit too; changed content or force=true is not.
    part = turn(read('src/a.ts', ('export const a = 1;\n' * 50)[100:300], offset=100, limit=200))
    assert json.loads(ingest([*folded, *part])[0][-1].content)['on_desk'] is True
    changed = turn(read('src/a.ts', 'export const a = 2;\n' * 50))
    assert ingest([*folded, *changed])[0][-1].content == changed[1].content
    forced = turn(read('src/a.ts', 'export const a = 1;\n' * 50, force=True))
    assert ingest([*folded, *forced])[0][-1].content == forced[1].content
    # Once folded, the notice is a stub (its turn is redundant) and counts as a read of the entry.
    later = fold([*result, *filler()])
    assert facts_of(by_id(later).get(result[-1].id, result[-1]))['level'] == STUB or result[-1].id not in by_id(later)
    entry = next(e for e in desk_of(later)['entries'] if e['key'][1] == 'src/a.ts')
    assert len(entry['reads']) == 2


def test_whole_reads_cover_ranges_and_documents_carry_their_latest_outline(rewrite_now):
    head = turn(read('src/big.ts', 'h' * 3000 + '\n[truncated; next_offset=3000]', limit=3000))
    tail = turn(read('src/big.ts', 't' * 1000, offset=3000, limit=1000))
    whole = turn(read('src/big.ts', 'h' * 3000 + 't' * 1000))
    part = turn(section('design', 's2', 'Section two text'))
    body = [HumanMessage(content='task', id='task'), *head, *tail, *whole, *part, *filler()]
    async def run():
        files = Files(**{'src/big.ts': 'h' * 3000 + 't' * 1000,
                         'design': {'version': 4, 'sections': {'s1': 'one', 's2': 'Section two text'}}})
        memory = ContextMemory(agent(), 'desk-cover', refresher=files)
        prepared = await memory.prepare([SystemMessage(content='sys'), *body])
        desk = desk_of(prepared.messages)
        assert [e['key'] for e in desk['entries']] == [['file', 'src/big.ts', 0, 30000], ['document', 'design', 's2', 0]]
        text = next(m for m in prepared.messages if is_desk(m)).content
        assert text.count('### file src/big.ts') == 1 and 'h' * 3000 + 't' * 1000 in text
        # The document shows its latest outline and version (refreshed without the pinned version).
        assert '### document design v4 — outline (latest)' in text and '- s1 Part s1' in text and 'v4 — section s2' in text
        assert ('collaboration_read', {'document_id': 'design', 'section_id': 's2'}) in files.calls
    asyncio.run(run())


def test_the_desk_refreshes_after_writes_and_drops_what_is_gone(rewrite_now):
    async def run():
        files = Files(**{'src/a.ts': 'old a', 'src/gone.ts': 'gone'})
        memory = ContextMemory(agent(), 'desk-refresh', refresher=files)
        history = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'),
                   *turn(read('src/a.ts', 'old a')), *turn(read('src/gone.ts', 'gone')), *filler()]
        first = await memory.prepare(history)
        assert 'old a' in next(m for m in first.messages if is_desk(m)).content
        files.files['src/a.ts'] = 'new a after my edit'
        del files.files['src/gone.ts']
        write = turn(('write_project_file', {'path': 'src/a.ts', 'edits': [{'old_text': 'old', 'new_text': 'new'}]},
                      'Wrote src/a.ts (19 characters; sha256=ab).'))
        second = await memory.prepare([*first.messages, *write, *filler(4)])
        desk = next(m for m in second.messages if is_desk(m))
        assert 'new a after my edit' in desk.content and 'updated since you read it' in desk.content
        assert 'Not on the desk any more' in desk.content and 'src/gone.ts' in desk.content
        assert [e['key'][1] for e in desk_of(second.messages)['entries']] == ['src/a.ts']
        # The old read is not stubbed as stale; the desk is the current text.
        assert not any('Stale' in m.content for m in second.messages if isinstance(m, ToolMessage))
    asyncio.run(run())


def test_the_desk_is_bounded_and_evicts_the_coldest_entry_first(monkeypatch, rewrite_now):
    monkeypatch.setenv('WDRUNGE_DESK_TOKENS', '1500')
    monkeypatch.setenv('WDRUNGE_DESK_HALF_LIFE', '2')
    cold, warm = turn(read('src/cold.ts', 'c' * 2000)), turn(read('src/warm.ts', 'w' * 2000))
    body = [HumanMessage(content='task', id='task'), *cold, *warm, *filler()]
    async def run():
        memory = ContextMemory(agent(), 'desk-evict')
        prepared = await memory.prepare([SystemMessage(content='sys'), *body])
        # Equal heat and size: the entry placed first leaves; the desk says so.
        assert [e['key'][1] for e in desk_of(prepared.messages)['entries']] == ['src/warm.ts']
        assert 'Not on the desk any more' in next(m for m in prepared.messages if is_desk(m)).content
        # cold.ts is read back: its earlier read counts again, so now warm.ts (read once) leaves.
        again = [*prepared.messages, *turn(read('src/cold.ts', 'c' * 2000)), *filler(4)]
        second = await memory.prepare(again)
        assert [e['key'][1] for e in desk_of(second.messages)['entries']] == ['src/cold.ts']
        assert len(next(e for e in desk_of(second.messages)['entries'])['reads']) == 2
    asyncio.run(run())


def test_the_desk_never_changes_between_rewrites_and_keeps_its_message_when_unchanged(monkeypatch):
    import time
    monkeypatch.setenv('WDRUNGE_CACHE_COLD_SECONDS', '1')
    async def run():
        memory = ContextMemory(agent(), 'desk-stable', refresher=Files(**{'src/a.ts': 'a' * 500}))
        history = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'), *turn(read('src/a.ts', 'a' * 500)), *filler()]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ContextMemory, 'worth_rewriting', lambda self, before, after, cold: True)
            first = await memory.prepare(history)
        desk = next(m for m in first.messages if is_desk(m))
        # A few more turns with nothing worth a rewrite: the history (and the desk) is returned as it is.
        more = [*first.messages, *turn(('search_code', {'query': 'q'}, 'short'))]
        second = await memory.prepare(more)
        assert not second.changed and next(m for m in second.messages if is_desk(m)) is desk
        # A cold cache and folds worth making: the rewrite happens, and the unchanged desk keeps its ID and text.
        padding = filler(6)
        padding[0].response_metadata['wdrunge_requested_at'] = time.time() - 100
        padding[-2].response_metadata['wdrunge_requested_at'] = time.time() - 100
        third = await memory.prepare([*more, *padding])
        assert third.changed
        kept = next(m for m in third.messages if is_desk(m))
        assert kept.id == desk.id and kept.content == desk.content and memory_module.same_for_model(kept, desk)
    asyncio.run(run())


def test_thresholds_are_actual_tokens_calibrated_from_billed_prompt_tokens(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '3000')
    sent = [SystemMessage(content='s' * 4000), HumanMessage(content='task'), *turn(read('a.md', '中' * 3000))]
    response = AIMessage(content='ok', usage_metadata={'input_tokens': 5000, 'output_tokens': 5, 'total_tokens': 5005})
    estimated = memory_module.message_tokens(sent) + 500
    assert calibrate(sent, response, schema_tokens=500) == round(5000 / estimated, 3)
    assert ratio_of([*sent, response]) == response.response_metadata[RATIO_KEY] < 1
    # A later measurement is smoothed into the earlier one and stays within bounds.
    later = AIMessage(content='ok', usage_metadata={'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105})
    calibrate([*sent, response], later, 500)
    assert 0.4 <= later.response_metadata[RATIO_KEY] < response.response_metadata[RATIO_KEY]
    assert calibrate(sent, AIMessage(content='no usage'), 0) is None
    # With half as many actual tokens per estimated one, twice the estimated volume stays verbatim.
    reads = [m for i in range(6) for m in turn(read(f'f{i}.py', 'x' * 1800))]
    body = [HumanMessage(content='task', id='task'), *reads, *turn()]
    plain = sum(facts_of(m)['level'] == 0 for m in fold(body) if isinstance(m, ToolMessage))
    calibrated = sum(facts_of(m)['level'] == 0 for m in fold(body, ratio=0.5) if isinstance(m, ToolMessage))
    assert plain < calibrated
    memory = ContextMemory(agent(100000), 'meter')
    assert memory.budget == memory.budget_for(1.0)
    assert memory.budget_for(0.5) == 2 * memory.budget_for(1.0) + memory.schema_tokens


class Summarizer:
    def __init__(self, text):
        self.text, self.inputs = text, []

    async def ainvoke(self, messages, **kwargs):
        self.inputs.append(messages)
        return AIMessage(content=self.text, usage_metadata={'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40})


def test_segment_summaries_are_bounded_by_merging_the_oldest(monkeypatch):
    monkeypatch.setenv('WDRUNGE_SEGMENT_TOKENS', '600')
    async def run():
        member = agent()
        model = Summarizer('Merged: wrote a.py and b.py; API routes /x and /y.')
        member.model = lambda: model
        memory = ContextMemory(member, 'merge-scope')
        summaries = [HumanMessage(content=f'Earlier work segment {i}: ' + 'detail ' * 150, id=f'seg{i}',
                                  additional_kwargs={'context_summary': True, 'context_segment': {'first': f'f{i}', 'last': f'l{i}', 'turns': 4}})
                     for i in range(4)]
        body = [HumanMessage(content='task', id='task'), *summaries, *turn()]
        first = await memory.merge_summaries(body)
        assert first == body  # prepared in the background
        for _ in range(100):
            if any(entry['text'] for key, entry in memory_module._SEGMENTS.items() if key[0] == 'merge-scope'):
                break
            await asyncio.sleep(.01)
        merged = await memory.merge_summaries(body)
        kept = [m for m in merged if m.additional_kwargs.get('context_segment')]
        assert len(kept) < 4 and kept[0].additional_kwargs['context_segment']['merged'] >= 2
        assert 'Merged: wrote a.py' in kept[0].content and kept[0].additional_kwargs['context_segment']['first'] == 'f0'
        assert 'Earlier work segment 0' in '\n'.join(m.content for call in model.inputs for m in call)
    asyncio.run(run())


def test_handoffs_keep_the_desk_and_a_fresh_attempt_is_told_what_was_at_hand(rewrite_now):
    async def run():
        member = agent(20000)
        member.model = lambda: Summarizer('Goal: finish. Next: tests.')
        memory = ContextMemory(member, 'handoff-desk', refresher=Files(**{'src/a.ts': 'a' * 800}))
        history = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'), *turn(read('src/a.ts', 'a' * 800)), *filler()]
        prepared = await memory.prepare(history)
        assert any(is_desk(m) for m in prepared.messages)
        checkpoint = await memory.checkpoint(prepared.messages, 'Goal: finish')
        assert any(is_desk(m) for m in checkpoint.messages) and 'Goal: finish' in checkpoint.messages[-1].content
        handoff = await memory.platform_handoff(prepared.messages[:1], prepared.messages[1:], 'test')
        assert any(is_desk(m) for m in handoff)
        from app.agents.worker import WorkerAgent
        from types import SimpleNamespace
        worker = WorkerAgent(AgentSpec(id='frontend', name='Frontend', prompt_file='frontend.md'))
        class Saver:
            async def aget_tuple(self, config):
                return SimpleNamespace(checkpoint={'channel_values': {'messages': prepared.messages}})
        worker._checkpointer = Saver()
        worker._threads['request'] = 'old-thread'
        progress = await worker.previous_progress('request')
        assert 'kept these at hand' in progress and 'file src/a.ts' in progress
    asyncio.run(run())


def test_a_worker_refreshes_the_desk_through_its_own_tools(monkeypatch, tmp_path, rewrite_now):
    from app.agents.tools._workspace import active_workspace
    from app.agents.tools.read_project_file import make_read_project_file
    from app.agents.tools.write_project_file import make_write_project_file
    from app.agents.worker import WorkerAgent
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '1000')
    monkeypatch.setenv('WDRUNGE_CACHE_COLD_SECONDS', '0')
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'a.js').write_text('export const a = 1;\n' + 'export const pad = 0;\n' * 59)
    (tmp_path / 'src' / 'b.js').write_text('export const b = 2;\n' * 60)
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
                step = len(seen)
                if step == 1:
                    return AIMessage(content='read a', tool_calls=[{'id': 'r1', 'name': 'read_project_file', 'args': {'path': 'src/a.js'}}])
                if step == 2:
                    return AIMessage(content='read b', tool_calls=[{'id': 'r2', 'name': 'read_project_file', 'args': {'path': 'src/b.js'}}])
                if step == 3:
                    return AIMessage(content='edit a', tool_calls=[{'id': 'w', 'name': 'write_project_file', 'args': {
                        'path': 'src/a.js', 'edits': [{'old_text': 'export const a = 1;\n', 'new_text': 'export const a = 42;\n'}]}}])
                if step == 4:
                    return AIMessage(content='read a again', tool_calls=[{'id': 'r3', 'name': 'read_project_file', 'args': {'path': 'src/a.js'}}])
                return AIMessage(content='done')
        monkeypatch.setattr(worker, 'model', lambda: Model())
        async for _ in worker.stream('build', conversation_id='chat', context_mode='fresh', handoff='details'):
            pass
        desks = [next((m for m in call if is_desk(m)), None) for call in seen]
        assert desks[-1] is not None
        # The desk shows a.js after the edit, read from disk, with b.js beside it.
        assert 'export const a = 42;' in desks[-1].content and 'export const b = 2;' in desks[-1].content
        # Reading a.js again after the refresh was a hit: the model got a notice, not the text.
        results = {m.tool_call_id: m for m in seen[-1] if isinstance(m, ToolMessage)}
        assert json.loads(results['r3'].content)['on_desk'] is True
    try:
        asyncio.run(run())
    finally:
        active_workspace.reset(token)
