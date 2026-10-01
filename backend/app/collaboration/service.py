"""Durable project collaboration with short SQLite write transactions.

Revision snapshots are the recovery journal. Files are materialized before events
are published. A pending revision is never visible as a published document.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from app.collaboration.documents import digest, outline, section, edit_document
from app.collaboration.schema import DDL
from app.storage import ChatStore, StorageConflictError, StorageNotFoundError

ROLES = {'leader', 'product', 'backend', 'frontend', 'designer', 'testing'}
TERMINAL = {'completed', 'cancelled', 'rejected', 'failed'}


class Collaboration:
    def __init__(self, store: ChatStore):
        self.store = store
        self._lock = asyncio.Lock()
        self._ready = False

    async def initialize(self):
        if self._ready:
            return
        await self.store.initialize()
        async with self._lock:
            async with self.store.engine.begin() as c:
                for sql in DDL:
                    await c.execute(text(sql))
            self._ready = True
        await self.recover()

    @asynccontextmanager
    async def transaction(self):
        await self.initialize()
        async with self._lock:
            async with self.store.engine.connect() as c:
                await c.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    yield c
                    await c.commit()
                except BaseException:
                    await c.rollback()
                    raise

    async def _project(self, c, project):
        row = (await c.execute(text('SELECT * FROM projects WHERE id=:id'), {'id': project})).mappings().first()
        if not row:
            raise StorageNotFoundError('Project not found')
        return dict(row)

    async def project_for_conversation(self, conversation):
        await self.initialize()
        async with self.store.engine.connect() as c:
            return await c.scalar(text('SELECT project_id FROM conversations WHERE id=:id'), {'id': conversation})

    async def _conversation(self, c, project, conversation):
        if conversation and not await c.scalar(text('SELECT id FROM conversations WHERE id=:id AND project_id=:p'), {'id': conversation, 'p': project}):
            raise ValueError('Conversation does not belong to project')

    async def _get(self, c, kind, project, identifier, required=True):
        value = await c.scalar(text('SELECT data FROM collaboration_records WHERE kind=:k AND project=:p AND id=:i'), {'k': kind, 'p': project, 'i': identifier})
        if value is None and required:
            raise StorageNotFoundError(f'{kind} not found')
        return json.loads(value) if value else None

    async def _put(self, c, kind, project, identifier, data):
        await c.execute(text('''INSERT INTO collaboration_records(kind,project,id,data,state,owner,updated)
          VALUES(:k,:p,:i,:d,:s,:o,:t) ON CONFLICT(kind,project,id) DO UPDATE SET
          data=excluded.data,state=excluded.state,owner=excluded.owner,updated=excluded.updated'''),
          dict(k=kind, p=project, i=identifier, d=json.dumps(data, ensure_ascii=False), s=data.get('state', ''), o=data.get('recipient', data.get('author', '')), t=time.time()))

    async def _list(self, c, kind, project):
        rows = await c.scalars(text('SELECT data FROM collaboration_records WHERE kind=:k AND project=:p ORDER BY updated,id'), {'k': kind, 'p': project})
        return [json.loads(r) for r in rows]

    async def _event(self, c, project, recipient, data, conversation=''):
        await c.execute(text('INSERT INTO collaboration_events(project,recipient,conversation,data,created) VALUES(:p,:r,:v,:d,:t)'), dict(p=project, r=recipient, v=conversation, d=json.dumps(data, ensure_ascii=False), t=time.time()))

    @staticmethod
    def _path(workspace, path):
        root = Path(workspace).resolve()
        rel = Path(path)
        result = (root / rel).resolve()
        if rel.is_absolute() or '..' in rel.parts or not result.is_relative_to(root) or '.git' in rel.parts or '.wdrunge-runs' in rel.parts:
            raise ValueError('Path must stay inside the project workspace')
        return result

    async def _dependencies(self, c, project, based_on):
        for dependency in based_on:
            doc = await self._get(c, 'document', project, dependency['document_id'])
            if doc['version'] != dependency['version']:
                raise StorageConflictError(f"Dependency changed: {doc['id']} is v{doc['version']}")

    async def publish(self, project, author, changes, conversation=''):
        """Validate all documents/dependencies, journal a batch, then publish together."""
        if author not in ROLES or not changes or len(changes) > 20:
            raise ValueError('Invalid author or batch size')
        batch_id = uuid4().hex
        async with self.transaction() as c:
            p = await self._project(c, project)
            await self._conversation(c, project, conversation)
            if await self._list(c, 'pending', project):
                raise StorageConflictError('An unfinished publication requires recovery')
            revisions = []
            seen = set()
            paths = {d['path']: d['id'] for d in await self._list(c, 'document', project)}
            for change in changes:
                identifier = change.get('document_id') or uuid4().hex
                if identifier in seen:
                    raise ValueError('Only one change per document in a batch')
                seen.add(identifier)
                current = await self._get(c, 'document', project, identifier, False)
                version = current['version'] if current else 0
                if change.get('base_version', 0) != version:
                    raise StorageConflictError(f'{identifier}: expected version {version}')
                summary = change.get('summary', '').strip()
                if not summary or len(summary) > 2000:
                    raise ValueError('A nonempty change summary of at most 2000 characters is required')
                await self._dependencies(c, project, change.get('based_on', []))
                old = (await self._get(c, 'revision', project, f'{identifier}:{version}'))['content'] if current else ''
                path = current['path'] if current else change['path']
                target = self._path(p['workspace_path'], path)
                if target.suffix.lower() != '.md':
                    raise ValueError('Managed documents must be Markdown files')
                if path in paths and paths[path] != identifier:
                    raise StorageConflictError('Path is already managed')
                paths[path] = identifier
                actual = target.read_text() if target.exists() else None
                external = change.get('external', False)
                if current and actual != old and not external:
                    raise StorageConflictError('External file change detected; import it before editing')
                if external:
                    if actual is None:
                        raise StorageConflictError('External file is missing')
                    content = actual
                elif current:
                    content = edit_document(old, change.get('edits', []))
                else:
                    content = change.get('content', actual or '')
                    if actual is not None and actual != content:
                        raise StorageConflictError('Existing file differs; import its current content first')
                if len(content) > 500_000:
                    raise ValueError('Document too large')
                revision = dict(id=identifier, path=path, title=change.get('title', current['title'] if current else target.stem), version=version+1,
                    content=content, hash=digest(content), outline=outline(content), summary=summary,
                    author=author, conversation=conversation, based_on=change.get('based_on', []), batch_id=batch_id,
                    created=time.time(), external=external,
                    diff=''.join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=f'{path}@{version}', tofile=f'{path}@{version+1}')),
                    previous_hash=digest(actual) if actual is not None else None)
                revisions.append(revision)
            await self._put(c, 'pending', project, batch_id, {'id': batch_id, 'revisions': revisions})
        await self.recover(project)
        async with self.transaction() as c:
            if await self._get(c, 'pending', project, batch_id, False):
                raise StorageConflictError('Publication blocked by an external file change; inspect pending batch')
        return [{'document_id': r['id'], 'version': r['version'], 'batch_id': batch_id} for r in revisions]

    async def recover(self, project=None):
        async with self.transaction() as c:
            projects = [project] if project else list(await c.scalars(text("SELECT DISTINCT project FROM collaboration_records WHERE kind='pending'")))
            for pid in projects:
                p = await self._project(c, pid)
                for pending in await self._list(c, 'pending', pid):
                    targets = []
                    for r in pending['revisions']:
                        target = self._path(p['workspace_path'], r['path'])
                        actual = digest(target.read_text()) if target.exists() else None
                        if actual not in {r['previous_hash'], r['hash']}:
                            break
                        targets.append((target, r))
                    else:
                        for target, r in targets:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            fd, temp = tempfile.mkstemp(dir=target.parent, prefix='.collab-')
                            try:
                                with os.fdopen(fd, 'w') as f:
                                    f.write(r['content'])
                                    f.flush()
                                    os.fsync(f.fileno())
                                os.replace(temp, target)
                            finally:
                                if os.path.exists(temp):
                                    os.unlink(temp)
                            await self._put(c, 'revision', pid, f"{r['id']}:{r['version']}", r)
                            await self._put(c, 'document', pid, r['id'], {k: r[k] for k in ('id', 'path', 'title', 'version', 'hash', 'author', 'created')})
                        for r in pending['revisions']:
                            for sub in await self._list(c, 'subscription', pid):
                                if sub['document_id'] == r['id']:
                                    await self._event(c, pid, sub['member'], {'type': 'document_changed', 'document_id': r['id'], 'version': r['version'], 'summary': r['summary'], 'batch_id': r['batch_id']})
                        await c.execute(text("DELETE FROM collaboration_records WHERE kind='pending' AND project=:p AND id=:i"), {'p': pid, 'i': pending['id']})

    async def catalog(self, project):
        async with self.transaction() as c:
            p = await self._project(c, project)
            docs = await self._list(c, 'document', project)
            for d in docs:
                path = self._path(p['workspace_path'], d['path'])
                d['external_change'] = not path.exists() or digest(path.read_text()) != d['hash']
            return docs

    async def read(self, project, document_id, version=None, section_id=None, offset=0, limit=6000):
        if offset < 0 or not 1 <= limit <= 12000:
            raise ValueError('Invalid read window')
        async with self.transaction() as c:
            doc = await self._get(c, 'document', project, document_id)
            r = await self._get(c, 'revision', project, f"{document_id}:{version or doc['version']}")
            if section_id is None:
                return {'document_id': document_id, 'version': r['version'], 'outline': r['outline'][offset:offset+min(limit,100)], 'next_offset': offset+100 if len(r['outline']) > offset+100 else None}
            s = section(r['content'], section_id)
            body = r['content'][s['body_start']:s['body_end']]
            end = min(len(body), offset+limit)
            return {'document_id': document_id, 'version': r['version'], 'section': s, 'content': body[offset:end], 'next_offset': end if end < len(body) else None}

    async def subscribe(self, project, member, document_id, enabled=True):
        if member not in ROLES:
            raise ValueError('Unknown member')
        async with self.transaction() as c:
            await self._get(c, 'document', project, document_id)
            key = f'{member}:{document_id}'
            if enabled:
                await self._put(c, 'subscription', project, key, dict(member=member, document_id=document_id))
            else:
                await c.execute(text("DELETE FROM collaboration_records WHERE kind='subscription' AND project=:p AND id=:i"), dict(p=project, i=key))
        return {'subscribed': enabled}

    async def send(self, project, sender, recipient, content, key, conversation='', reply_to=None):
        if sender not in ROLES or recipient not in ROLES or not content.strip() or len(content) > 4000:
            raise ValueError('Invalid message')
        async with self.transaction() as c:
            await self._project(c, project)
            await self._conversation(c, project, conversation)
            if reply_to:
                await self._get(c, 'message', project, reply_to)
            identifier = f'{sender}:{key}'
            message = dict(id=identifier, sender=sender, recipient=recipient, content=content, conversation=conversation, reply_to=reply_to)
            old = await self._get(c, 'message', project, identifier, False)
            if old and old != message:
                raise StorageConflictError('Idempotency key was used for different message')
            if not old:
                await self._put(c, 'message', project, identifier, message)
                await self._event(c, project, recipient, {'type': 'message', **message}, conversation)
            return message

    async def inbox(self, project, member, context, conversation='', limit=6000):
        async with self.transaction() as c:
            rows = (await c.execute(text('''SELECT e.seq,e.data FROM collaboration_events e
              LEFT JOIN collaboration_receipts r ON r.project=e.project AND r.context=:ctx AND r.seq=e.seq
              WHERE e.project=:p AND e.recipient=:m AND (e.conversation='' OR e.conversation=:v)
              AND r.seq IS NULL ORDER BY e.seq LIMIT 100'''), dict(ctx=f'{member}:{context}', p=project, m=member, v=conversation))).mappings().all()
            items = []
            size = 0
            for row in rows:
                item = {'event_id': row['seq'], **json.loads(row['data'])}
                length = len(json.dumps(item, ensure_ascii=False))
                if size + length > limit:
                    break
                items.append(item)
                size += length
            return items

    async def acknowledge(self, project, member, context, event_ids):
        async with self.transaction() as c:
            for identifier in event_ids:
                await c.execute(text('''INSERT OR IGNORE INTO collaboration_receipts(project,context,seq)
                  SELECT project,:ctx,seq FROM collaboration_events WHERE project=:p AND recipient=:m AND seq=:s'''), dict(ctx=f'{member}:{context}', p=project, m=member, s=identifier))

    async def create_request(self, project, sender, recipient, goal, key, conversation='', based_on=None, dependencies=None, artifacts=None, parent=None):
        if sender not in ROLES or recipient not in ROLES - {'leader'} or not goal.strip() or len(goal) > 8000:
            raise ValueError('Invalid request')
        async with self.transaction() as c:
            await self._project(c, project)
            await self._conversation(c, project, conversation)
            identifier = f'{sender}:{key}'
            spec = dict(sender=sender, recipient=recipient, goal=goal, conversation=conversation, based_on=based_on or [], dependencies=dependencies or [], artifacts=artifacts or [], parent=parent)
            old = await self._get(c, 'request', project, identifier, False)
            if old:
                if old['spec'] != spec:
                    raise StorageConflictError('Idempotency key was used for another request')
                return old
            await self._dependencies(c, project, spec['based_on'])
            for dependency in spec['dependencies']:
                await self._get(c, 'request', project, dependency)
            if parent:
                await self._get(c, 'request', project, parent)
            requests = await self._list(c, 'request', project)
            if sum(r['state'] not in TERMINAL for r in requests) >= 50 or (parent and sum(r['spec']['parent'] == parent for r in requests) >= 12):
                raise ValueError('Outstanding request budget reached')
            request = dict(id=identifier, **spec, spec=spec, state='queued', created=time.time(), attempts=0, result='', delivered=[], needs_review=False)
            await self._put(c, 'request', project, identifier, request)
            await self._event(c, project, recipient, {'type': 'request', 'request_id': identifier, 'goal': goal[:1000]}, conversation)
            return request

    async def wait_for(self, project, identifier, dependencies, actor):
        async with self.transaction() as c:
            request = await self._get(c, 'request', project, identifier)
            if actor != request['recipient'] or request['state'] != 'running':
                raise ValueError('Only the running recipient may suspend a request')
            all_requests = {r['id']: r for r in await self._list(c, 'request', project)}
            for dep in dependencies:
                if dep not in all_requests:
                    raise ValueError('Unknown dependency')
                todo, seen = [dep], set()
                while todo:
                    node = todo.pop()
                    if node == identifier:
                        raise StorageConflictError('Dependency cycle detected')
                    if node not in seen:
                        seen.add(node)
                        todo.extend(all_requests[node]['dependencies'])
            request.update(dependencies=sorted(set(request['dependencies'] + dependencies)), state='blocked')
            await self._put(c, 'request', project, identifier, request)
            return request

    async def transition(self, project, identifier, actor, state, result='', artifacts=None, based_on=None):
        async with self.transaction() as c:
            p = await self._project(c, project)
            r = await self._get(c, 'request', project, identifier)
            allowed = {'running': {'review', 'blocked', 'failed', 'cancelled'}, 'queued': {'cancelled', 'rejected'}, 'blocked': {'queued', 'cancelled'}, 'review': {'completed', 'queued', 'cancelled'}}
            if state not in allowed.get(r['state'], set()):
                raise StorageConflictError(f"Cannot move {r['state']} to {state}")
            if actor not in {r['sender'], r['recipient'], 'leader'} or (state == 'completed' and actor not in {r['sender'], 'leader'}):
                raise ValueError('Actor cannot make this transition')
            if based_on is not None:
                await self._dependencies(c, project, based_on)
                r['based_on'] = based_on
            if state in {'review', 'completed'}:
                await self._dependencies(c, project, r['based_on'])
                if not (result or r['result']).strip():
                    raise ValueError('A delivery summary is required')
                delivered = artifacts if artifacts is not None else r['delivered']
                for path in set(delivered + r['artifacts']):
                    if not self._path(p['workspace_path'], path).is_file():
                        raise ValueError(f'Artifact does not exist: {path}')
                if any(d['state'] != 'completed' for d in [await self._get(c, 'request', project, dep) for dep in r['dependencies']]):
                    raise StorageConflictError('Dependencies are not completed')
                r['delivered'] = delivered
            r.update(state=state, result=result or r['result'], needs_review=False)
            await self._put(c, 'request', project, identifier, r)
            await self._event(c, project, r['sender'], {'type': 'request_status', 'request_id': identifier, 'state': state, 'result': r['result'][:1200]}, r['conversation'])
            return r

    async def get_request(self, project, identifier):
        async with self.transaction() as c:
            return await self._get(c, 'request', project, identifier)

    async def snapshot(self, project):
        async with self.transaction() as c:
            await self._project(c, project)
            return {kind: await self._list(c, kind, project) for kind in ('document', 'subscription', 'message', 'request', 'pending')}

    async def claim(self):
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,data FROM collaboration_records WHERE kind='request' AND state IN ('queued','blocked','running') ORDER BY updated"))).mappings().all()
            busy = {(row['project'], json.loads(row['data'])['recipient']) for row in rows if json.loads(row['data'])['state'] == 'running'}
            for row in rows:
                project, r = row['project'], json.loads(row['data'])
                paused = await self._get(c, 'settings', project, 'project', False)
                if paused and paused.get('paused'):
                    continue
                if r['state'] == 'running' or (project, r['recipient']) in busy:
                    continue
                deps = [await self._get(c, 'request', project, d) for d in r['dependencies']]
                if any(d['state'] not in {'completed', 'review'} for d in deps):
                    continue
                if r['attempts'] >= 12:
                    r.update(state='failed', result='Request execution budget exhausted')
                    await self._put(c, 'request', project, r['id'], r)
                    continue
                r.update(state='running', attempts=r['attempts']+1)
                await self._put(c, 'request', project, r['id'], r)
                p = await self._project(c, project)
                return project, p['workspace_path'], r
        return None

    async def recover_requests(self):
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,id,data FROM collaboration_records WHERE kind='request' AND state='running'"))).mappings().all()
            for row in rows:
                r = json.loads(row['data'])
                r.update(state='queued')
                await self._put(c, 'request', row['project'], row['id'], r)

    async def set_paused(self, project, paused):
        async with self.transaction() as c:
            await self._project(c, project)
            await self._put(c, 'settings', project, 'project', {'paused': paused})
        return {'paused': paused}
