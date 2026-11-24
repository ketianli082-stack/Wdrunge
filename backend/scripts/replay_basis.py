"""Replay deliveries that were refused for a changed document version under today's rule.

    python scripts/replay_basis.py <records.json> <calls.json>

records.json: collaboration_records of the project (kinds revision and request);
calls.json: its tool_calls rows (see replay_tool_calls.py). For every submit_task that
failed with "Dependency changed", the project's documents are rebuilt as they were at
that moment, the member's document reads up to then are recorded the way the platform
records them now, and the delivery check runs (app.collaboration.basis). Reads are
taken per member, not per task, which can only add reads and so cannot hide a conflict.
"""
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


async def replay(records, calls):
    from app.collaboration.basis import BasisChanged
    from app.collaboration.service import Collaboration
    from app.storage import ChatStore
    revisions = sorted((json.loads(r['data']) for r in records if r['kind'] == 'revision'), key=lambda r: (r['id'], r['version']))
    requests = {json.loads(r['data'])['id']: json.loads(r['data']) for r in records if r['kind'] == 'request'}
    refused = [c for c in calls if c['tool'] == 'submit_task' and c['status'] == 'error' and 'Dependency changed' in c['result']]
    for number, call in enumerate(refused, 1):
        moment, identifier = epoch(call['created_at']), json.loads(call['arguments'])['identifier']
        with tempfile.TemporaryDirectory() as folder:
            store = ChatStore(f'sqlite+aiosqlite:///{folder}/replay.db')
            project = (await store.create_project('Replay', folder))['id']
            service = Collaboration(store)
            await service.initialize()
            request = {**requests[identifier], 'state': 'running', 'based_on': []}
            async with service.transaction() as c:
                latest = {}
                for revision in revisions:
                    if revision['created'] <= moment:
                        await service._put(c, 'revision', project, f"{revision['id']}:{revision['version']}", revision)
                        latest[revision['id']] = revision
                for revision in latest.values():
                    await service._put(c, 'document', project, revision['id'],
                                       {k: revision[k] for k in ('id', 'path', 'title', 'version', 'hash', 'author', 'created')})
                await service._put(c, 'request', project, identifier, request)
            reads = 0
            for earlier in calls:
                if (earlier['tool'] != 'collaboration_read' or earlier['agent_id'] != call['agent_id'] or earlier['status'] != 'success'
                        or epoch(earlier['created_at']) >= moment):
                    continue
                arguments = json.loads(earlier['arguments'])
                try:
                    result = json.loads(earlier['result'])
                except ValueError:  # cut at the log limit: the version is near the start
                    version = re.search(r'"version": (\d+)', earlier['result'])
                    result = {'document_id': arguments.get('document_id'), 'version': int(version.group(1)) if version else None,
                              'content': 'x' * arguments.get('limit', 6000)}
                if not isinstance(result, dict) or 'document_id' not in result:
                    continue  # a notice that the text was already shown
                await service.record_read(project, identifier, result, arguments.get('section_id'), arguments.get('subtree', False),
                                          arguments.get('whole', False), arguments.get('offset', 0))
                reads += 1
            async with service.transaction() as c:
                basis = [(d['path'], d['version']) for d, _ in await service.basis_documents(c, project, request)]
                record = await service._get(c, 'basis', project, identifier, False) or {'documents': {}}
                try:
                    notes, outcome = await service.basis_check(c, project, request), 'accepted'
                except BasisChanged as error:
                    notes, outcome = [str(error), json.dumps(error.details, ensure_ascii=False)[:600]], 'REFUSED'
            was = re.search(r'Dependency changed: (\w+) is v(\d+)', call['result'])
            path = next((r['path'] for r in latest.values() if r['id'] == was.group(1)), was.group(1))
            print(f"{number}. {call['created_at'][11:19]} {call['agent_id']} {identifier}")
            print(f"   then: refused, {path} is v{was.group(2)} (the member had typed another version into based_on)")
            print(f"   task declares: needs={request.get('needs') or []}, documents={request.get('documents') or []}")
            print(f"   documents it is built on now: {basis or 'none'}; {reads} document reads recorded "
                  f"({sum(len(v) for v in record['documents'].values())} sections)")
            print(f"   now: {outcome}" + (f" — {'; '.join(notes)}" if notes else ''))
            await store.close()


async def section_comparison(records, back=2):
    """The run's own revisions as a case for the comparison by section: a task that needs
    the most revised document and read all of it `back` versions ago, and one that read
    only a section that did not change since."""
    from app.collaboration.basis import BasisChanged, section_texts
    from app.collaboration.service import Collaboration
    from app.storage import ChatStore
    revisions = sorted((json.loads(r['data']) for r in records if r['kind'] == 'revision'), key=lambda r: (r['id'], r['version']))
    document = max({r['id'] for r in revisions}, key=lambda d: sum(r['id'] == d for r in revisions))
    history = [r for r in revisions if r['id'] == document]
    current, old = history[-1], history[max(0, len(history) - 1 - back)]
    with tempfile.TemporaryDirectory() as folder:
        store = ChatStore(f'sqlite+aiosqlite:///{folder}/replay.db')
        project = (await store.create_project('Replay', folder))['id']
        service = Collaboration(store)
        await service.initialize()
        before, now = section_texts(old), section_texts(current)
        same = next(s for s in old['outline'] if s['id'] != 'root' and before[s['id']] == now.get(s['id']))
        tasks = {'reader:all': None, 'reader:one': same['id']}
        async with service.transaction() as c:
            for revision in history:
                await service._put(c, 'revision', project, f"{revision['id']}:{revision['version']}", revision)
            await service._put(c, 'document', project, document, {k: current[k] for k in ('id', 'path', 'title', 'version', 'hash', 'author', 'created')})
            for identifier in tasks:
                await service._put(c, 'request', project, identifier, {'id': identifier, 'state': 'running', 'needs': [{'document': current['path']}]})
        print(f"\nComparison by section on {current['path']}: read at v{old['version']}, delivered at v{current['version']}")
        for identifier, section in tasks.items():
            result = {'document_id': document, 'version': old['version'], 'content': old['content']}
            await service.record_read(project, identifier, result, section_id=section, whole=section is None)
            async with service.transaction() as c:
                try:
                    notes = await service.basis_check(c, project, await service._get(c, 'request', project, identifier))
                    print(f"   read only 「{same['title']}」: accepted — {'; '.join(notes)}")
                except BasisChanged as error:
                    print(f'   read the whole document: {error}')
                    for item in error.details.get('changed_sections', []):
                        print(f"     changed 「{item['title']}」 in v{item['changed_in']}: {item['summary'][:110]}")
                    for item in error.details.get('removed_sections', []):
                        print(f"     removed or renamed 「{item['title']}」")
        await store.close()


if __name__ == '__main__':
    loaded = json.loads(Path(sys.argv[1]).read_text())
    asyncio.run(replay(loaded, json.loads(Path(sys.argv[2]).read_text())))
    asyncio.run(section_comparison(loaded))
