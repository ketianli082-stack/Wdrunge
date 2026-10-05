"""The context pipeline: entry budget (L0), pruning (L1), policy-based micro-compaction
(L2), work-segment summaries (L3) and when history is worth rewriting."""
import asyncio
import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents import context_fold, memory as memory_module
from app.agents.base import Agent, AgentSpec
from app.agents.context_fold import STUB, estimate, facts_of, fold, ingest, is_ledger, segments, unchanged_of
from app.agents.context_policy import DURABLE, RECOMPUTABLE, policy_of
from app.agents.memory import ContextMemory, message_tokens

counter = iter(range(10**6))


def turn(*calls, text=''):
    ids = [f'c{next(counter)}' for _ in calls]
    if not calls:
        return [AIMessage(content=text or 'thinking', id=f'ai-{next(counter)}')]
    ai = AIMessage(content=text, id=f'ai-{ids[0]}', tool_calls=[{'id': i, 'name': name, 'args': args}
                                                                for i, (name, args, _) in zip(ids, calls)])
    return [ai, *[ToolMessage(content=result if isinstance(result, str) else json.dumps(result), tool_call_id=i, id=f'r-{i}',
                              status='error' if isinstance(result, dict) and result.get('error') else 'success')
                  for i, (_, _, result) in zip(ids, calls)]]


def read(path, body):
    return 'read_project_file', {'path': path}, body


def agent(window=131072):
    return Agent(AgentSpec(id='backend', name='Backend', prompt_file='backend.md', context_window=window))


def by_id(messages):
    return {m.id: m for m in messages}


class Summarizer:
    def __init__(self, text='Did X; wrote src/a.py.'):
        self.inputs, self.text = [], text

    async def ainvoke(self, messages, **kwargs):
        self.inputs.append(messages)
        return AIMessage(content=self.text, usage_metadata={'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40})


def test_tools_declare_their_context_policy():
    assert policy_of('read_project_file') == policy_of('search_code') == policy_of('execute_command') == RECOMPUTABLE
    assert policy_of('collaboration_read') == policy_of('collaborate', {'action': 'read'}) == RECOMPUTABLE
    assert policy_of('collaboration_publish') == policy_of('collaborate', {'action': 'transition'}) == DURABLE
    assert policy_of('write_project_file') == policy_of('a_tool_nobody_declared') == DURABLE


def test_a_large_result_is_capped_on_entry_and_recoverable(monkeypatch):
    monkeypatch.setenv('WDRUNGE_RESULT_TOKENS', '1000')
    text = '中文内容' * 3000  # 12,000 characters, ~18K estimated tokens
    body = [HumanMessage(content='task', id='task'), *turn(read('docs/a.md', text))]
    result, source, replacement = ingest(body)
    capped = result[-1]
    assert source == [body[-1]] and replacement == [capped]
    assert estimate(capped.content) <= 1000 and capped.content.startswith('中文内容')
    # A paged read keeps its head and says where to continue.
    assert f'recall_context(message_id="{capped.id}", offset=' in capped.content
    assert facts_of(capped)['capped'] == len(text) and facts_of(capped)['complete'] is False
    # Already processed results are never cut again.
    assert ingest(result)[1] == []


def test_capped_command_output_keeps_head_and_tail_and_json_keeps_its_shape(monkeypatch):
    monkeypatch.setenv('WDRUNGE_RESULT_TOKENS', '800')
    output = 'start\n' + 'noise line\n' * 2000 + 'ERROR: tests failed\n'
    body = [*turn(('execute_command', {'command': 'npm', 'arguments': ['test']},
                   {'id': 'p1', 'exit_code': 1, 'output': output}))]
    capped = ingest(body)[0][-1]
    data = json.loads(capped.content)
    assert data['exit_code'] == 1 and data['output'].startswith('start') and 'ERROR: tests failed' in data['output']
    assert 'context_budget' in data and estimate(capped.content) <= 800


def test_results_of_one_response_share_a_budget(monkeypatch):
    monkeypatch.setenv('WDRUNGE_RESULT_TOKENS', '8000')
    monkeypatch.setenv('WDRUNGE_RESPONSE_RESULT_TOKENS', '6000')
    body = turn(read('a.md', 'a' * 2000), read('b.md', 'b' * 12000), read('c.md', 'c' * 12000))
    result = ingest(body)[0]
    sizes = [facts_of(m)['tokens'] for m in result[1:]]
    assert sum(sizes) <= 6000 and result[1].content == 'a' * 2000  # the small one stays whole


def test_an_unchanged_result_still_in_context_is_not_repeated():
    first = turn(read('src/app.py', 'print(1)\n' * 200))
    again = turn(read('src/app.py', 'print(1)\n' * 200))
    body = [HumanMessage(content='task', id='task'), *first, *again]
    result = ingest(body)[0]
    notice = result[-1]
    assert json.loads(notice.content)['unchanged'] is True and unchanged_of(notice) == first[1].id
    # Asking again right after the notice returns the result itself.
    third = turn(read('src/app.py', 'print(1)\n' * 200))
    assert ingest([*result, *third])[0][-1].content == third[1].content
    # A changed file, a folded earlier copy or force=true return the full text.
    changed = turn(read('src/app.py', 'print(2)\n' * 200))
    assert ingest([*body[:3], *changed])[0][-1].content == changed[1].content
    forced = turn(('collaboration_read', {'document_id': 'd', 'section_id': 's', 'force': True}, '{"content": "x"}'))
    earlier = turn(('collaboration_read', {'document_id': 'd', 'section_id': 's', 'force': True}, '{"content": "x"}'))
    assert ingest([*earlier, *forced])[0][-1].content == '{"content": "x"}'
    durable = turn(('collaboration_publish', {'changes': []}, '{"revisions": []}'))
    repeat = turn(('collaboration_publish', {'changes': []}, '{"revisions": []}'))
    assert ingest([*durable, *repeat])[0][-1].content == '{"revisions": []}'


def test_a_notice_keeps_the_result_it_stands_for(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '1000')
    first = turn(read('src/app.py', 'x = 1\n' * 400))
    later = [m for i in range(3) for m in turn(read(f'notes{i}.txt', 'n' * 1500))]
    body = ingest([HumanMessage(content='task', id='task'), *first, *later, *turn(read('src/app.py', 'x = 1\n' * 400))])[0]
    folded = by_id(fold(body))
    assert folded[first[1].id].content == first[1].content  # pinned while the notice is verbatim
    assert folded[later[1].id].content != later[1].content


def test_durable_results_are_never_cut_mechanically(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '1000')
    monkeypatch.setenv('WDRUNGE_FOLD_EXCERPT_TOKENS', '0')
    publish = turn(('collaboration_publish', {'changes': []}, {'revisions': [{'document_id': 'd', 'version': 2}], 'note': 'p' * 3000}))
    reading = turn(read('src/a.py', 'a' * 3000))
    body = [HumanMessage(content='task', id='task'), *publish, *reading, *turn(read('src/b.py', 'b' * 3000))]
    folded = by_id(fold(body))
    assert folded[publish[1].id].content == publish[1].content
    assert folded[reading[1].id].content.startswith('[Folded read_project_file result')


def test_redundant_turns_are_pruned_outside_the_verbatim_tier(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '1000')
    failing = turn(('execute_command', {'command': 'curl', 'arguments': ['http://x']}, {'error': 'refused', 'exit_code': 7}))
    same = turn(('collaboration_check_delivery', {}, {'findings': []}))
    repeated = turn(('collaboration_check_delivery', {}, {'findings': []}))
    thinking = turn(read('a.py', 'a' * 100), text='The service is down; retrying.')
    ok = turn(('execute_command', {'command': 'curl', 'arguments': ['http://x']}, {'exit_code': 0, 'output': 'hi'}))
    goal = HumanMessage(content='goal v1', id='goal-1', additional_kwargs={'goal_state': True})
    body = [HumanMessage(content='task', id='task'), goal, *failing, *same, *thinking, *repeated, *ok,
            HumanMessage(content='goal v2', id='goal-2', additional_kwargs={'goal_state': True}),
            *turn(read('n.txt', 'n' * 3000)), *turn(read('m.txt', 'm' * 3000))]
    folded = fold(body)
    ids = {m.id for m in folded}
    # A failure the same call later fixed, and an identical repeated check, leave with their calls.
    assert failing[0].id not in ids and failing[1].id not in ids
    assert same[0].id not in ids and repeated[1].id in ids and 'goal-1' not in ids and 'goal-2' in ids
    # A turn with the model's own words stays (its reasoning is not redundant).
    assert thinking[0].id in ids
    assert all(m.tool_call_id in {c['id'] for a in folded if isinstance(a, AIMessage) for c in a.tool_calls}
               for m in folded if isinstance(m, ToolMessage))


def test_tiers_count_per_result_so_one_large_turn_does_not_flatten_history(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '10000')
    monkeypatch.setenv('WDRUNGE_FOLD_EXCERPT_TOKENS', '15000')
    monkeypatch.setenv('WDRUNGE_RESULT_TOKENS', '4000')
    older = [m for i in range(3) for m in turn(read(f'old{i}.py', 'o' * 2000))]
    large = turn(*[read(f'big{i}.md', 'b' * 7000) for i in range(4)])  # ~14K counted, more than the verbatim tier
    body = [HumanMessage(content='task', id='task'), *older, *large, *turn(read('now.py', 'x' * 2000))]
    folded = by_id(fold(body))
    # The newest results of the large turn stay verbatim; older ones fall to excerpts, not pointers.
    assert folded[large[-1].id].content == large[-1].content
    assert all(facts_of(folded[m.id])['level'] == context_fold.EXCERPT for m in older if isinstance(m, ToolMessage))


def test_history_is_rewritten_once_waiting_has_cost_as_much_as_the_rewrite():
    old = [m for i in range(6) for m in turn(read(f'f{i}.py', 'x' * 1000))]
    cached = [m for i in range(10) for m in turn(('collaboration_publish', {'changes': []}, 'p' * 8000))]
    before = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'), *old, *cached, *turn()]
    after = [*before[:2], *[m.model_copy(update={'content': 'short'}) if isinstance(m, ToolMessage) else m for m in old],
             *cached, before[-1]]
    memory = ContextMemory(agent(), 'rent-or-buy')
    saved = message_tokens(before) - message_tokens(after)
    # Each call that keeps the foldable ~3K pays for it at the cached price; the rewrite would
    # make the next call resend ~40K at full price. It happens once waiting has cost that much.
    waited = 0
    while not memory.worth_rewriting(before, after, cold=False):
        waited += 1
    assert waited == int(.8 * (message_tokens(after) - message_tokens(before[-1:]) - message_tokens(before[:2])) // (.2 * saved))
    assert memory.worth_rewriting(before, after, cold=False)  # stays due until prepare rewrites the history
    # A cold cache has nothing to lose; changes to unsent messages are free.
    assert ContextMemory(agent(), 'cold').worth_rewriting(before, after, cold=True)
    tail = [*before[:-1], before[-1].model_copy(update={'content': 'x'})]
    assert ContextMemory(agent(), 'tail').worth_rewriting(
        [*before[:-1], before[-1].model_copy(update={'content': 'x' * 4000})], tail, cold=False)


def test_a_cold_cache_is_told_by_the_time_of_the_last_request(monkeypatch):
    monkeypatch.setenv('WDRUNGE_CACHE_COLD_SECONDS', '600')
    recent = AIMessage(content='a', response_metadata={'wdrunge_requested_at': time.time() - 30})
    old = AIMessage(content='a', response_metadata={'wdrunge_requested_at': time.time() - 900})
    assert not ContextMemory.cache_cold([recent]) and ContextMemory.cache_cold([old])
    assert not ContextMemory.cache_cold([AIMessage(content='no timestamp')])


def test_work_segments_end_at_durable_changes(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '2000')
    reads = [m for i in range(3) for m in turn(read(f'docs/s{i}.md', 's' * 1500))]
    write = turn(('write_project_file', {'path': 'src/a.py', 'content': 'a' * 3000}, 'Wrote src/a.py (3000 characters; sha256=ab).'))
    broken = turn(('execute_command', {'command': 'npm', 'arguments': ['build']}, {'error': 'build failed', 'exit_code': 1, 'output': 'e' * 3000}))
    publish = turn(('collaboration_publish', {'changes': []}, {'revisions': [{'document_id': 'd', 'version': 1}]}))
    body = [HumanMessage(content='task', id='task'), *reads, *write, *broken, *publish,
            *[m for i in range(3) for m in turn(read(f'now{i}.py', 'n' * 1500))]]
    folded = fold(body)
    spans = segments(folded)
    # Reads up to the write, then the failed build up to the publish (the ledger keeps the failure).
    assert [(folded[a].id, folded[b - 1].id) for a, b in spans] == [(reads[0].id, write[1].id), (broken[0].id, publish[1].id)]
    assert 'Unresolved tool failures' in next(m for m in folded if is_ledger(m)).content
    # The newest outline of a document keeps its segment.
    outline = turn(('collaboration_publish', {'changes': []}, {'revisions': [{'document_id': 'd', 'version': 2, 'outline': [{'id': 's1', 'title': 'A'}]}]}))
    body = [*body[:-6], *outline, *body[-6:]]
    folded = fold(body)
    assert all(outline[1].id not in {m.id for m in folded[a:b]} for a, b in segments(folded))


def test_completed_work_collapses_to_a_summary_near_the_budget(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '3000')
    async def run():
        member = agent(24000)
        model = Summarizer('Read the specs; wrote src/m*.py.')
        member.model = lambda: model
        memory = ContextMemory(member, 'segment-scope')
        work = [m for i in range(6) for m in turn(
            ('collaboration_publish', {'changes': [{'document_id': f'd{i}'}]}, {'revisions': [], 'note': 'p' * 5000}))]
        history = [SystemMessage(content='sys'), HumanMessage(content='Assigned task: build', id='task'), *work,
                   *turn(read('now.py', 'x' * 1000))]
        assert message_tokens(history) > memory.budget * memory_module.SEGMENT_COMMIT
        prepared = await memory.prepare(history)
        assert prepared.stage == 'collapse' and model.inputs
        summaries = [m for m in prepared.messages if m.additional_kwargs.get('context_segment')]
        assert summaries and 'Read the specs' in summaries[0].content and 'recall_context' in summaries[0].content
        assert message_tokens(prepared.messages) < message_tokens(history)
        # The same segments give the same summary messages, so the next call keeps its cache prefix.
        again = await memory.prepare(prepared.messages)
        assert [m.id for m in again.messages] == [m.id for m in prepared.messages]
    asyncio.run(run())


def test_segment_summaries_are_prepared_in_the_background(monkeypatch):
    monkeypatch.setenv('WDRUNGE_FOLD_VERBATIM_TOKENS', '3000')
    monkeypatch.setattr(memory_module, 'SEGMENT_STAGE', .01)
    async def run():
        member = agent(131072)
        model = Summarizer('Background summary.')
        member.model = lambda: model
        memory = ContextMemory(member, 'background-scope')
        work = [m for i in range(4) for m in turn(
            ('collaboration_publish', {'changes': [{'document_id': f'd{i}'}]}, {'revisions': [], 'note': 'p' * 3000}))]
        history = [SystemMessage(content='sys'), HumanMessage(content='task', id='task'), *work,
                   *turn(read('now.py', 'x' * 1000))]
        first = await memory.prepare(history)
        assert not any(m.additional_kwargs.get('context_segment') for m in first.messages)
        for _ in range(100):
            if any(entry['text'] for key, entry in memory_module._SEGMENTS.items() if key[0] == 'background-scope'):
                break
            await asyncio.sleep(.01)
        assert any(entry['text'] == 'Background summary.' for key, entry in memory_module._SEGMENTS.items()
                   if key[0] == 'background-scope')
    asyncio.run(run())


def test_the_whole_history_summary_never_cuts_the_verbatim_tier(monkeypatch):
    monkeypatch.setattr(memory_module, 'SEGMENT_STAGE', 10)
    monkeypatch.setattr(memory_module, 'SEGMENT_COMMIT', 10)
    async def run():
        member = agent(12000)
        model = Summarizer()
        member.model = lambda: model
        work = [m for i in range(10) for m in turn(('future_tool', {'i': i}, 'w' * 5000))]
        recent = turn(read('src/now.py', 'n' * 3500))
        history = [SystemMessage(content='sys'), HumanMessage(content='Assigned task', id='task'), *work, *recent]
        prepared = await ContextMemory(member, 'summary-scope').prepare(history)
        assert prepared.stage == 'summary'
        assert by_id(prepared.messages)[recent[1].id].content == 'n' * 3500
        submitted = '\n'.join(m.content for call in model.inputs for m in call)
        assert 'w' * 4000 not in submitted  # what is summarized is cut for the summarizer
    asyncio.run(run())
