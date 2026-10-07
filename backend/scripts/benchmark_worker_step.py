"""Measure what the backend itself costs per model turn of a worker.

One real task runs through the scheduler, the worker graph, SQLite checkpoints, the
collaboration records and the real file tools, against a model that answers at once
(read two files, write one, search, in turn). Everything measured is platform time:
context preparation, checkpoints, activity and usage records, the task journal.

Reported per model turn: wall and CPU milliseconds, database round trips (calls handed
to the SQLite thread), SQL statements and connection checkouts. The counts are exact
and repeatable; the times vary by a few percent between runs, so compare the minimum
of several. Blog4 measured 36 ms per turn in the real run (median, 1,933 turns).

Usage: python scripts/benchmark_worker_step.py [turns] [--runs N] [--timers]
--timers also prints the time spent in each store, service and checkpoint operation.
Run it on two revisions (a git worktree) to compare them.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DASHSCOPE_API_KEY', 'benchmark')

import aiosqlite  # noqa: E402
import aiosqlite.core  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.agents.team import Team  # noqa: E402
from app.agents.worker import WorkerAgent  # noqa: E402
from app.collaboration.runtime import Scheduler  # noqa: E402
from app.collaboration.service import Collaboration  # noqa: E402
from app.storage import ChatStore  # noqa: E402

FILES = 40
counts = {'round_trips': 0, 'statements': 0, 'checkouts': 0}
timers: dict[str, list] = {}


def count_round_trips():
    original = aiosqlite.core.Connection._execute

    async def counted(self, fn, *args, **kwargs):
        counts['round_trips'] += 1
        return await original(self, fn, *args, **kwargs)
    aiosqlite.core.Connection._execute = counted


def timed(owner, name):
    original = getattr(owner, name)
    label = f'{owner.__name__}.{name}'

    def record(start):
        entry = timers.setdefault(label, [0, 0.0])
        entry[0] += 1
        entry[1] += time.perf_counter() - start

    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                record(start)
    else:
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                record(start)
    setattr(owner, name, wrapper)


def instrument():
    from langgraph.prebuilt import ToolNode
    from app.agents.memory import ContextMemory
    for name in ('upsert_activity', 'record_tool_calls', 'record_agent_usage', 'save_agent_session', 'save_compaction'):
        timed(ChatStore, name)
    for name in ('get_request', 'inbox', 'journal_facts', 'acknowledge'):
        timed(Collaboration, name)
    for name in ('aput', 'aput_writes'):
        timed(AsyncSqliteSaver, name)
    for name in ('prepare', 'settle_desk'):
        timed(ContextMemory, name)
    timed(ToolNode, 'ainvoke')


class SummaryModel:
    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content='Confirmed: handlers are exported from the out files; values live in fileN.ts.',
                         usage_metadata={'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20})


class WorkModel:
    """Answers at once with the next tool calls; ends after `turns` responses."""
    def __init__(self, turns: int):
        self.turns, self.calls = turns, 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        n = self.calls
        self.calls += 1
        size = sum(len(m.content) for m in messages if isinstance(m.content, str))
        usage = {'input_tokens': size // 3, 'output_tokens': 200, 'total_tokens': size // 3 + 200,
                 'input_token_details': {'cache_read': size // 4}}
        if n >= self.turns:
            return AIMessage(content='Delivered.', usage_metadata=usage)
        if n % 4 in (0, 1):
            tool_calls = [{'name': 'read_project_file', 'id': f'c{n}-{i}',
                           'args': {'path': f'frontend/src/file{(n * 2 + i) % FILES}.ts'}} for i in range(2)]
        elif n % 4 == 2:
            body = f'// revision {n}\n' + ''.join(
                f'export function handler{n}_{i}(input: string): string {{ return input + "{i}"; }}\n' for i in range(60))
            tool_calls = [{'name': 'write_project_file', 'id': f'c{n}-0',
                           'args': {'path': f'frontend/src/out{n % 10}.ts', 'content': body}}]
        else:
            tool_calls = [{'name': 'search_code', 'id': f'c{n}-0', 'args': {'query': f'handler{n - 1}_', 'path': 'frontend/src'}}]
        return AIMessage(content='', tool_calls=tool_calls, usage_metadata=usage)


async def run(turns: int) -> dict:
    directory = Path(tempfile.mkdtemp(prefix='wdrunge-benchmark-'))
    try:
        workspace = directory / 'workspace'
        (workspace / 'frontend/src').mkdir(parents=True)
        for i in range(FILES):
            (workspace / f'frontend/src/file{i}.ts').write_text(
                f'// file {i}\n' + ''.join(f'export const value{i}_{j} = "{"x" * 60}";\n' for j in range(70)))
        database = directory / 'benchmark.db'
        store = ChatStore(f'sqlite+aiosqlite:///{database}')
        event.listen(store.engine.sync_engine, 'before_cursor_execute',
                     lambda *args, **kwargs: counts.__setitem__('statements', counts['statements'] + 1))
        event.listen(store.engine.sync_engine, 'checkout',
                     lambda *args, **kwargs: counts.__setitem__('checkouts', counts['checkouts'] + 1))
        project = (await store.create_project('Benchmark', str(workspace)))['id']
        conversation = (await store.create_conversation(project))['id']
        service = Collaboration(store)
        await service.initialize()
        connection = await aiosqlite.connect(str(database))  # as app.main opens the checkpointer
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        model = WorkModel(turns)
        WorkerAgent.model = lambda self, purpose='work', *args, **kwargs: SummaryModel() if purpose == 'summary' else model
        team = Team()
        await team.configure_persistence(store, saver)
        team._checkpointer, team.collaboration = saver, service
        scheduler = Scheduler(service, team)
        await service.begin_conversation_run(project, conversation, message_id='m1', content='go')
        await service.create_request(project, 'leader', 'frontend', 'Implement the reading module. ' + 'Details. ' * 200,
                                     'benchmark', conversation=conversation, artifacts=['frontend/src/out2.ts'])
        job = await service.claim(scheduler.owner)
        before, wall, cpu = dict(counts), time.perf_counter(), time.process_time()
        await scheduler.execute(*job)
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        state = (await service.snapshot(project))['request'][0]['state']
        activities = len((await store.get_conversation_detail(conversation))['activities'])
        result = {'turns': model.calls, 'state': state, 'wall_ms_per_turn': round(wall * 1000 / model.calls, 1),
                  'cpu_ms_per_turn': round(cpu * 1000 / model.calls, 1),
                  **{f'{key}_per_turn': round((counts[key] - before[key]) / model.calls, 1) for key in counts},
                  'activities': activities, 'database_mb': round(database.stat().st_size / 1048576, 1)}
        await connection.close()
        await store.close()
        return result
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('turns', type=int, nargs='?', default=200)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--timers', action='store_true')
    args = parser.parse_args()
    count_round_trips()
    if args.timers:
        instrument()
    results = []
    for _ in range(args.runs):
        results.append(asyncio.run(run(args.turns)))
        print(json.dumps(results[-1]), flush=True)
    if args.runs > 1:
        print('MIN', json.dumps({key: min(r[key] for r in results) for key in ('wall_ms_per_turn', 'cpu_ms_per_turn')}))
    if timers:
        turns = sum(r['turns'] for r in results)
        for label, (calls, seconds) in sorted(timers.items(), key=lambda item: -item[1][1]):
            print(f'{label:34} {calls / turns:5.1f} calls/turn {seconds * 1000 / turns:7.2f} ms/turn {seconds * 1e6 / calls:8.0f} us/call')


if __name__ == '__main__':
    main()
