"""Replay a recorded run's model calls through the context pipeline, without a model.

Inputs (a directory of JSON files exported from a run's database, see
docs/reports/2026-10-06-blog4-run-analysis.md, appendix):
- calls.json: tool_calls rows (agent_id, tool_call_id, tool, status, created_at, arguments, result);
- work.json: agent_usage rows of model calls (agent_id, created_at, request_id, total_tokens, output_tokens);
- rbytes.json: [{tool_call_id, rbytes}] the real byte size of each result (results are stored cut).

For each model call of an agent within an attempt window, the tool calls after it form a
turn; before each call, ContextMemory.prepare runs on the history exactly as in the
worker. Summaries come from a fixed fake text. A desk refresh returns the latest text seen
for the same resource. Reports per attempt: calls, rewrites by stage, desk hits, estimated
and actual context size, and the input priced with a prompt cache (first changed message
onward at full price, the rest at WDRUNGE_CACHE_HIT_PRICE).

Usage: python scripts/replay_context.py <data_dir> --agent frontend [--attempts attempts.json] [--ratio 0.643]
The same script runs on earlier revisions of the pipeline (it only calls prepare), so
branches can be compared on the same run.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import collections
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402

from app.agents.base import Agent, AgentSpec  # noqa: E402
from app.agents.memory import ContextMemory, message_tokens  # noqa: E402

BLOG4_ATTEMPTS = {
    'frontend': [('reading#1', '2026-10-05 13:32:18', '2026-10-05 16:07:30'), ('reading#2', '2026-10-05 16:08', '2026-10-05 18:23:20'),
                 ('reading#3', '2026-10-05 18:23:20', '2026-10-05 18:37:20'), ('admin#1', '2026-10-05 18:37:20', '2026-10-05 20:14'),
                 ('admin#2', '2026-10-05 20:14', '2026-10-05 21:45:20'), ('admin#3', '2026-10-05 21:45:20', '2026-10-05 22:15')],
    'testing': [('e2e#1', '2026-10-05 22:14', '2026-10-05 23:45:30'), ('e2e#2', '2026-10-05 23:45:30', '2026-10-06 01')],
    'backend': [('api-reading', '2026-10-05 12:23', '2026-10-05 13:32'), ('api-admin', '2026-10-05 13:32', '2026-10-05 16:18'),
                ('rollout', '2026-10-05 16:24', '2026-10-05 16:54'), ('defect', '2026-10-05 22:32', '2026-10-05 23:14')]}
SUMMARY = ('Confirmed: routes /api/v1/articles (list, detail), fields id/slug/title/summary/tags; client.ts request() '
           'wraps fetch with timeout; wrote the files named in the ledger. Decided: reuse Pagination.tsx. Next: wire the page.')


def stamp(text: str) -> float:
    return datetime.datetime.fromisoformat(text[:26]).replace(tzinfo=datetime.timezone.utc).timestamp()


class FakeSummaryModel:
    def __init__(self):
        self.calls = 0

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=SUMMARY, usage_metadata={'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20})


def same(a, b) -> bool:
    return (a.id == b.id and type(a) is type(b) and a.content == b.content
            and getattr(a, 'tool_calls', None) == getattr(b, 'tool_calls', None))


async def replay(data: Path, agent_id: str, attempts, ratio: float, window: int):
    calls = [c for c in json.load(open(data / 'calls.json')) if c['agent_id'] == agent_id]
    work = [w for w in json.load(open(data / 'work.json')) if w['agent_id'] == agent_id]
    rbytes = {r['tool_call_id']: r['rbytes'] for r in json.load(open(data / 'rbytes.json'))}
    price = float(os.getenv('WDRUNGE_CACHE_HIT_PRICE', '0.2'))
    try:
        from app.agents.context_meter import RATIO_KEY
    except ImportError:  # an earlier revision without the meter
        RATIO_KEY = None
    try:
        from app.agents.context_desk import desk_of, is_desk
    except ImportError:
        desk_of = is_desk = None
    try:
        from app.agents.read_result import format_read, parse_read
    except ImportError:  # an earlier revision: file entries are keyed by the requested offset and limit
        format_read = parse_read = None
    report = []
    for name, start, end in attempts:
        member = Agent(AgentSpec(id=agent_id, name=agent_id, prompt_file=f'{agent_id}.md', context_window=window))
        summary_model = FakeSummaryModel()
        member.model = lambda **kwargs: summary_model
        latest: dict[str, str] = {}
        # Ranges of each file as last read, newest last: the desk re-reads a file by the
        # range an entry holds, which need not be a range any recorded call asked for.
        ranges: dict[str, list] = collections.defaultdict(list)

        async def refresher(tool, args):
            if tool == 'read_project_file' and parse_read is not None:
                start = args.get('offset') or 0
                end = start + (args.get('limit') or 30000)
                for read in reversed(ranges.get(args.get('path'), [])):
                    if read.start <= start and (end <= read.end or read.next_offset is None):
                        total = read.total if read.total is not None else (read.end if read.next_offset is None else None)
                        return format_read(read.body[start - read.start:end - read.start], start, total,
                                           more=read.next_offset is not None or end < read.end)
                raise KeyError(args.get('path'))
            key = resource(tool, args)
            if key not in latest:
                raise KeyError(key)
            return latest[key]

        memory = ContextMemory(member, f'replay:{agent_id}:{name}', refresher=refresher) \
            if 'refresher' in ContextMemory.__init__.__code__.co_varnames else ContextMemory(member, f'replay:{agent_id}:{name}')
        works = [w for w in work if start <= w['created_at'] < end]
        times = [w['created_at'] for w in works]
        by_turn = collections.defaultdict(list)
        for call in calls:
            if start <= call['created_at'] < end:
                index = bisect.bisect_right(times, call['created_at']) - 1
                if index >= 0:
                    by_turn[index].append(call)
        messages = [SystemMessage(content='system prompt ' * 400, id='system'),
                    HumanMessage(content='Assigned task: ' + name + ' ' + 'handoff ' * 300, id='task')]
        stats = collections.Counter()
        sent_before = None
        sizes, desk_sizes = [], []
        for index, w in enumerate(works):
            prepared = await memory.prepare(messages)
            messages = prepared.messages
            # Hits: notices the entry budget produced for reads of what the desk showed (counted
            # before they fold into stubs).
            last_turn = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=len(messages))
            stats['desk_hits'] += sum(1 for m in messages[last_turn:] if isinstance(m, ToolMessage) and '"on_desk": true' in m.content)
            if prepared.changed and prepared.stage not in (None, 'ingest'):
                stats['rewrites'] += 1
                stats[f'stage:{prepared.stage}'] += 1
            # Cache-priced input: everything from the first message that differs from the last call's is full price.
            if sent_before is None:
                first = 0
            else:
                first = next((i for i, (a, b) in enumerate(zip(sent_before, messages)) if not same(a, b)), min(len(sent_before), len(messages)))
            cached = message_tokens(messages[:first]) * ratio
            full = message_tokens(messages[first:]) * ratio
            stats['input_actual'] += cached + full
            stats['input_priced'] += cached * price + full
            stats['cached_actual'] += cached
            sizes.append(message_tokens(messages) * ratio)
            if desk_of is not None:
                desk_sizes.append(message_tokens([m for m in messages if is_desk(m)]) * ratio)
            sent_before = list(messages)
            # The model's turn and its tool results.
            turn_calls = by_turn.get(index, [])
            metadata = {'wdrunge_requested_at': stamp(w['created_at'])}
            if RATIO_KEY:
                metadata[RATIO_KEY] = ratio
            ai = AIMessage(content='', id=f'ai-{index}', response_metadata=metadata, tool_calls=[
                {'id': c['tool_call_id'], 'name': c['tool'], 'args': arguments(c)} for c in turn_calls])
            results = []
            for c in turn_calls:
                text = padded(c['result'], rbytes.get(c['tool_call_id']))
                results.append(ToolMessage(content=text, tool_call_id=c['tool_call_id'], id=f'r-{c["tool_call_id"]}',
                                           status='error' if c['status'] != 'success' else 'success'))
                key = resource(c['tool'], arguments(c))
                if key and c['status'] == 'success':
                    latest[key] = text
                    if c['tool'] == 'read_project_file' and parse_read is not None:
                        ranges[arguments(c).get('path')].append(parse_read(text, arguments(c).get('offset') or 0))
                stats['reads'] += bool(key)
            messages = [*messages, ai, *results]
            stats['calls'] += 1
        final = await memory.prepare(messages)
        last_turn = max((i for i, m in enumerate(final.messages) if isinstance(m, AIMessage)), default=0)
        stats['desk_hits'] += sum(1 for m in final.messages[last_turn:] if isinstance(m, ToolMessage) and '"on_desk": true' in m.content)
        stats['summary_calls'] = summary_model.calls
        n = max(1, stats['calls'])
        report.append({'attempt': name, 'calls': stats['calls'], 'reads': stats['reads'], 'desk_hits': stats['desk_hits'],
                       'rewrites': stats['rewrites'], 'stages': {k[6:]: v for k, v in stats.items() if k.startswith('stage:')},
                       'summary_calls': stats['summary_calls'],
                       'avg_input_actual': round(stats['input_actual'] / n), 'peak_input_actual': round(max(sizes) if sizes else 0),
                       'avg_desk_actual': round(sum(desk_sizes) / n) if desk_sizes else 0,
                       'input_actual_total': round(stats['input_actual']), 'input_priced_total': round(stats['input_priced']),
                       'cache_hit_rate': round(stats['cached_actual'] / max(1, stats['input_actual']), 3)})
        print(json.dumps(report[-1], ensure_ascii=False), flush=True)
    return report


def arguments(call) -> dict:
    try:
        value = json.loads(call['arguments'])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def padded(text: str, size: int | None) -> str:
    """Stored results are cut at 8,000 characters; pad to the real size so volume is right
    (deterministically, so a repeated read still compares equal)."""
    if not size:
        return text
    missing = size - len(text.encode('utf-8'))
    return text + ('x' * missing if missing > 0 else '')


def resource(tool: str, args: dict) -> str | None:
    if tool == 'collaborate':
        tool = 'collaboration_' + str(args.get('action', ''))
        args = {k: v for k, v in args.items() if k != 'action'}
    if tool == 'read_project_file' and args.get('path'):
        return json.dumps(['file', args['path'], args.get('offset') or 0, args.get('limit') or 30000])
    if tool == 'collaboration_read' and args.get('document_id'):
        part = 'whole' if args.get('whole') else str(args.get('section_id') or 'outline')
        return json.dumps(['document', args['document_id'], part, args.get('offset') or 0])
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('data', type=Path)
    parser.add_argument('--agent', default='frontend')
    parser.add_argument('--attempts', type=Path, help='JSON [[name, start, end], ...]; default: the Blog4 attempts')
    parser.add_argument('--ratio', type=float, default=0.643, help='actual tokens per estimated token')
    parser.add_argument('--window', type=int, default=131072)
    args = parser.parse_args()
    attempts = json.load(open(args.attempts)) if args.attempts else BLOG4_ATTEMPTS[args.agent]
    report = asyncio.run(replay(args.data, args.agent, attempts, args.ratio, args.window))
    total = collections.Counter()
    for item in report:
        for key in ('calls', 'reads', 'desk_hits', 'rewrites', 'summary_calls', 'input_actual_total', 'input_priced_total'):
            total[key] += item[key]
    print('TOTAL', json.dumps(dict(total)))


if __name__ == '__main__':
    main()
