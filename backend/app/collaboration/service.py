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

from app.collaboration.documents import digest, outline, section, edit_document, page
from app.collaboration.schema import DDL
from app.collaboration.workspaces import merge_text, IsolatedWorkspace
from app.storage import ChatStore, StorageConflictError, StorageNotFoundError

ROLES = {'leader', 'product', 'backend', 'frontend', 'designer', 'testing'}
TERMINAL = {'completed', 'cancelled', 'rejected', 'failed'}


class Collaboration:
    def __init__(self, store: ChatStore):
        self.store = store
        self._lock = asyncio.Lock()
        self._ready = False
        self.max_running = max(1, min(32, int(os.getenv('WDRUNGE_MAX_CONCURRENCY', '4'))))
        self.role_concurrency = max(1, min(8, int(os.getenv('WDRUNGE_ROLE_CONCURRENCY', '2'))))

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
        await self.rebuild_search()

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

    @asynccontextmanager
    async def reading(self):
        await self.initialize()
        async with self.store.engine.connect() as c:
            await c.exec_driver_sql('BEGIN')
            try:
                yield c
            finally:
                await c.rollback()

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
        if rel.is_absolute() or '..' in rel.parts or not result.is_relative_to(root) or any(part in {'.git', '.wdrunge-runs', '.aws', '.codex', '.agents'} for part in rel.parts):
            raise ValueError('Path must stay inside the project workspace')
        return result

    async def _dependencies(self, c, project, based_on):
        for dependency in based_on:
            doc = await self._get(c, 'document', project, dependency['document_id'])
            if doc['version'] != dependency['version']:
                raise StorageConflictError(f"Dependency changed: {doc['id']} is v{doc['version']}")

    async def publish(self, project, author, changes, conversation=''):
        """Validate all documents/dependencies, journal a batch, then publish together."""
        if author not in ROLES or not isinstance(changes, list) or not changes or len(changes) > 20:
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
                base_version = change.get('base_version', 0)
                merging = base_version != version
                if merging and (not change.get('allow_merge') or not current or change.get('external')):
                    raise StorageConflictError(f'{identifier}: expected version {version}')
                summary = change.get('summary', '').strip()
                if not summary or len(summary) > 2000:
                    raise ValueError('A nonempty change summary of at most 2000 characters is required')
                await self._dependencies(c, project, change.get('based_on', []))
                old = (await self._get(c, 'revision', project, f'{identifier}:{version}'))['content'] if current else ''
                path = current['path'] if current else change['path']
                target = self._path(p['workspace_path'], path)
                path = str(target.relative_to(Path(p['workspace_path']).resolve()))
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
                    if merging:
                        base_revision = await self._get(c, 'revision', project, f'{identifier}:{base_version}')
                        base_content = base_revision['content']
                        # Automatic reconciliation is allowed only for unchanged
                        # direct bodies of explicitly selected existing sections.
                        for edit in change.get('edits', []):
                            if edit['operation'] not in {'replace_text', 'replace_section_body'}:
                                raise StorageConflictError('Structural edits require current document version')
                            before = section(base_content, edit['section_id'])
                            after = section(old, edit['section_id'])
                            if base_content[before['start']:before['body_end']] != old[after['start']:after['body_end']]:
                                raise StorageConflictError('The edited section changed concurrently')
                        content = merge_text(base_content, old, edit_document(base_content, change.get('edits', [])))
                    else:
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
                    created=time.time(), external=external, merged_from_version=base_version if merging else None,
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
                            await self._index_sections(c, pid, r)
                            await self._put(c, 'document', pid, r['id'], {k: r[k] for k in ('id', 'path', 'title', 'version', 'hash', 'author', 'created')})
                        for r in pending['revisions']:
                            previous = await self._get(c, 'revision', pid, f"{r['id']}:{r['version']-1}", False)
                            old_sections = {s['id']: previous['content'][s['start']:s['body_end']] for s in previous['outline']} if previous else {}
                            new_sections = {s['id']: r['content'][s['start']:s['body_end']] for s in r['outline']}
                            changed = [key for key in old_sections.keys() | new_sections.keys() if old_sections.get(key) != new_sections.get(key)]
                            notified = set()
                            for sub in await self._list(c, 'subscription', pid):
                                if sub['document_id'] == r['id'] and sub.get('section_id'):
                                    selected = next((h for h in r['outline'] if h['id'] == sub['section_id']), None)
                                    ambiguous = selected is None or sum(h['title'] == selected['title'] and h['parent'] == selected['parent'] for h in r['outline']) != 1
                                    if ambiguous:
                                        sub['needs_review'] = True
                                        await self._put(c, 'subscription', pid, f"{sub['member']}:{r['id']}:{sub['section_id']}", sub)
                                        await self._event(c, pid, sub['member'], {'type': 'subscription_structure_changed', 'document_id': r['id'], 'section_id': sub['section_id'], 'hint': 'Read the new outline and resubscribe; whole-document reminders remain enabled.'})
                                if sub['document_id'] == r['id'] and (not sub.get('section_id') or sub.get('needs_review') or sub['section_id'] in changed) and sub['member'] not in notified:
                                    notified.add(sub['member'])
                                    await self._event(c, pid, sub['member'], {'type': 'document_changed', 'document_id': r['id'], 'version': r['version'], 'summary': r['summary'], 'batch_id': r['batch_id'], 'changed_sections': changed})
                        changed_versions = {r['id']: r['version'] for r in pending['revisions']}
                        for request in await self._list(c, 'request', pid):
                            affected = [ref for ref in request['based_on'] if ref['document_id'] in changed_versions and ref['version'] != changed_versions[ref['document_id']]]
                            if affected and request['state'] not in TERMINAL:
                                request['needs_review'] = True
                                await self._put(c, 'request', pid, request['id'], request)
                                for member in {request['sender'], request['recipient']}:
                                    await self._event(c, pid, member, {'type': 'request_basis_changed', 'request_id': request['id'], 'dependencies': affected}, request['conversation'])
                        await c.execute(text("DELETE FROM collaboration_records WHERE kind='pending' AND project=:p AND id=:i"), {'p': pid, 'i': pending['id']})

    async def catalog(self, project):
        async with self.reading() as c:
            p = await self._project(c, project)
            docs = await self._list(c, 'document', project)
            for d in docs:
                path = self._path(p['workspace_path'], d['path'])
                d['external_change'] = not path.exists() or digest(path.read_text()) != d['hash']
            return docs

    async def read(self, project, document_id, version=None, section_id=None, offset=0, limit=6000):
        if offset < 0 or not 1 <= limit <= 12000:
            raise ValueError('Invalid read window')
        async with self.reading() as c:
            doc = await self._get(c, 'document', project, document_id)
            r = await self._get(c, 'revision', project, f"{document_id}:{version or doc['version']}")
            if section_id is None:
                return {'document_id': document_id, 'version': r['version'], 'outline': r['outline'][offset:offset+min(limit,100)], 'next_offset': offset+min(limit,100) if len(r['outline']) > offset+min(limit,100) else None}
            s = section(r['content'], section_id)
            body = r['content'][s['body_start']:s['body_end']]
            content, next_offset, fragment = page(body, offset, limit)
            return {'document_id': document_id, 'version': r['version'], 'section': s, 'content': content, 'next_offset': next_offset, 'fragment': fragment}


    async def subscribe(self, project, member, document_id, enabled=True, section_id=None):
        if member not in ROLES:
            raise ValueError('Unknown member')
        async with self.transaction() as c:
            await self._get(c, 'document', project, document_id)
            key = f'{member}:{document_id}:{section_id or "*"}'
            if section_id:
                doc = await self._get(c, 'document', project, document_id)
                revision = await self._get(c, 'revision', project, f"{document_id}:{doc['version']}")
                selected = section(revision['content'], section_id)
                if sum(h['title'] == selected['title'] and h['parent'] == selected['parent'] for h in revision['outline']) != 1:
                    raise ValueError('Section subscription requires a unique sibling heading')
            if enabled:
                await self._put(c, 'subscription', project, key, dict(member=member, document_id=document_id, section_id=section_id))
            else:
                await c.execute(text("DELETE FROM collaboration_records WHERE kind='subscription' AND project=:p AND id=:i"), dict(p=project, i=key))
        return {'subscribed': enabled}

    @staticmethod
    def _summary(summary, fallback):
        if summary is None:
            return fallback
        if not isinstance(summary, str):
            raise ValueError('Public summary must be text')
        value = ' '.join(summary.split())
        if not value or len(value) > 160:
            raise ValueError('Public summary must be 1..160 characters; keep full details in content/result')
        return value

    async def _group_message(self, c, project, conversation, sender, recipient, summary, key):
        if not conversation:
            return None
        from app.agents.identity import member_name
        identifier = digest(f'{project}:{key}')
        await self.store.add_chat_message_in_transaction(c, conversation, {
            'id': identifier, 'sender': sender,
            'content': f'@{member_name(recipient)} {summary}',
            'mentions': [{'worker': recipient, 'name': member_name(recipient), 'summary': summary}],
        })
        return identifier

    async def send(self, project, sender, recipient, content, key, conversation='', reply_to=None, request_id=None, summary=None):
        if not key or len(key) > 160 or sender not in ROLES or recipient not in ROLES or not content.strip() or len(content) > 4000:
            raise ValueError('Invalid message')
        summary = self._summary(summary, '有一条协作消息，请查看详细记录。')
        async with self.transaction() as c:
            await self._project(c, project)
            await self._conversation(c, project, conversation)
            if reply_to:
                await self._get(c, 'message', project, reply_to)
            if request_id:
                request = await self._get(c, 'request', project, request_id)
                if request['recipient'] != recipient or request['conversation'] != conversation:
                    raise ValueError('Message request target must match recipient and conversation')
            identifier = f'{sender}:{key}'
            message = dict(id=identifier, sender=sender, recipient=recipient, content=content, conversation=conversation, reply_to=reply_to, target_request=request_id, summary=summary)
            old = await self._get(c, 'message', project, identifier, False)
            if old:
                old.setdefault('summary', '有一条协作消息，请查看详细记录。')
            if old and {k: v for k, v in old.items() if k != 'chat_message_id'} != message:
                raise StorageConflictError('Idempotency key was used for different message')
            if not old:
                message['chat_message_id'] = await self._group_message(c, project, conversation, sender, recipient, summary, 'message:'+identifier)
                await self._put(c, 'message', project, identifier, message)
                await self._event(c, project, recipient, {'type': 'message', **message}, conversation)
            return old or message

    async def inbox(self, project, member, context, conversation='', limit=6000, request=None):
        async with self.reading() as c:
            rows = (await c.execute(text('''SELECT e.seq,e.data FROM collaboration_events e
              LEFT JOIN collaboration_receipts r ON r.project=e.project AND r.context=:ctx AND r.seq=e.seq
              WHERE e.project=:p AND e.recipient=:m AND (e.conversation='' OR e.conversation=:v)
              AND (json_extract(e.data,'$.target_request') IS NULL OR json_extract(e.data,'$.target_request')=:request)
              AND r.seq IS NULL ORDER BY e.seq LIMIT 200'''), dict(ctx=f'{member}:{context}', p=project, m=member, v=conversation, request=request))).mappings().all()
            items = []
            size = 0
            for row in rows:
                item = {'event_id': row['seq'], **json.loads(row['data'])}
                original_length = len(json.dumps(item, ensure_ascii=False))
                if original_length > limit//2:
                    for field in ('content','summary','goal','result','detail'):
                        if isinstance(item.get(field), str) and len(item[field]) > 600:
                            item[field] = item[field][:600]
                            item['preview_only'] = True
                    if len(item.get('changed_sections', [])) > 20:
                        item['changed_section_count'] = len(item['changed_sections'])
                        item['changed_sections'] = item['changed_sections'][:20]
                        item['preview_only'] = True
                length = len(json.dumps(item, ensure_ascii=False)) + 180
                if size + length > limit:
                    break
                items.append(item)
                size += length
            groups = {}
            result = []
            for item in items:
                key = item.get('document_id') if item.get('type') == 'document_changed' else None
                if key and key in groups:
                    group = groups[key]
                    group['event_ids'].append(item['event_id'])
                    group['versions'].append(item['version'])
                    group['changes'].append({'version': item['version'], 'summary': item['summary'], 'changed_sections': item.get('changed_sections', [])})
                elif key:
                    group = dict(type='document_changes', document_id=key, event_ids=[item['event_id']], versions=[item['version']], changes=[{'version': item['version'], 'summary': item['summary'], 'changed_sections': item.get('changed_sections', [])}])
                    groups[key] = group
                    result.append(group)
                else:
                    result.append(item)
            return result

    async def acknowledge(self, project, member, context, event_ids):
        async with self.transaction() as c:
            for identifier in event_ids:
                await c.execute(text('''INSERT OR IGNORE INTO collaboration_receipts(project,context,seq)
                  SELECT project,:ctx,seq FROM collaboration_events WHERE project=:p AND recipient=:m AND seq=:s'''), dict(ctx=f'{member}:{context}', p=project, m=member, s=identifier))
                event = (await c.execute(text('SELECT conversation,data FROM collaboration_events WHERE project=:p AND recipient=:m AND seq=:s'), {'p': project, 'm': member, 's': identifier})).mappings().first()
                if event:
                    payload = json.loads(event['data'])
                    if event['conversation'] and payload.get('chat_message_id'):
                        await self.store.add_receipt_in_transaction(c, event['conversation'], {'message_id': payload['chat_message_id'], 'reader': member})


    async def create_request(self, project, sender, recipient, goal, key, conversation='', based_on=None, dependencies=None, artifacts=None, parent=None, priority=1, deadline=None, summary=None, origin_message=None):
        summary = self._summary(summary, '请协作完成这项任务，详见任务记录。')
        if not key or len(key) > 160:
            raise ValueError('A bounded idempotency key is required')
        if priority not in range(4) or (deadline is not None and deadline <= 0):
            raise ValueError('Priority must be 0..3 and deadline a positive Unix timestamp')
        if sender not in ROLES or recipient not in ROLES - {'leader'} or not goal.strip() or len(goal) > 8000:
            raise ValueError('Invalid request')
        async with self.transaction() as c:
            await self._project(c, project)
            await self._conversation(c, project, conversation)
            identifier = f'{sender}:{key}'
            spec = dict(sender=sender, recipient=recipient, goal=goal, conversation=conversation, based_on=based_on or [], dependencies=dependencies or [], artifacts=artifacts or [], parent=parent, priority=priority, deadline=deadline, summary=summary)
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
            project_info = await self._project(c, project)
            for path in spec['artifacts']:
                self._path(project_info['workspace_path'], path)
            request = dict(id=identifier, **spec, spec=spec, state='queued', created=time.time(), attempts=0, result='', delivered=[], needs_review=False)
            if origin_message:
                if not await c.scalar(text('SELECT 1 FROM messages WHERE id=:i AND conversation_id=:c'), {'i': origin_message, 'c': conversation}):
                    raise ValueError('Origin message must belong to the request conversation')
                request['origin_message'] = origin_message
            else:
                request['origin_message'] = await self._group_message(c, project, conversation, sender, recipient, summary, 'request:'+identifier)
            await self._put(c, 'request', project, identifier, request)
            await self._event(c, project, recipient, {'type': 'request', 'request_id': identifier, 'goal': goal[:1000], 'target_request': identifier}, conversation)
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

    async def transition(self, project, identifier, actor, state, result='', artifacts=None, based_on=None, summary=None):
        public_summary = self._summary(summary, {'review': '已提交交付，请验收。', 'completed': '验收通过，可以继续后续工作。', 'queued': '请继续处理原任务并重新交付。', 'failed': '任务执行失败，需要处理后重试。', 'blocked': '当前任务等待依赖或问题处理。', 'cancelled': '这项任务已取消。', 'rejected': '暂不执行这项任务。'}.get(state, '任务状态已更新。'))
        async with self.transaction() as c:
            p = await self._project(c, project)
            r = await self._get(c, 'request', project, identifier)
            allowed = {'running': {'review', 'blocked', 'failed', 'cancelled'}, 'queued': {'cancelled', 'rejected'}, 'blocked': {'queued', 'cancelled'}, 'review': {'completed', 'queued', 'cancelled'}, 'failed': {'queued', 'cancelled'}}
            if state not in allowed.get(r['state'], set()):
                raise StorageConflictError(f"Cannot move {r['state']} to {state}")
            if actor not in {r['sender'], r['recipient'], 'leader'} or (state == 'completed' and actor not in {r['sender'], 'leader'}):
                raise ValueError('Actor cannot make this transition')
            if r['state'] == 'failed' and actor not in {r['sender'], 'leader'}:
                raise ValueError('Only sender or Leader may retry a failed request')
            if r['state'] == 'failed' and state == 'queued':
                r['attempts'] = 0
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
            if state == 'queued':
                r.pop('blocked_reason', None)
            if state == 'cancelled':
                await self._cancel_dependents(c, project, identifier)
            await self._put(c, 'request', project, identifier, r)
            await self._event(c, project, r['sender'], {'type': 'request_status', 'request_id': identifier, 'state': state, 'result': r['result'][:1200], 'target_request': r.get('parent')}, r['conversation'])
            recipient = r['recipient'] if actor != r['recipient'] else r['sender']
            await self._group_message(c, project, r['conversation'], actor, recipient, public_summary, f'transition:{identifier}:{uuid4().hex}')
            if state == 'completed' and r.get('origin_message') and r['conversation']:
                await self.store.add_reaction_in_transaction(c, r['conversation'], {'message_id': r['origin_message'], 'worker': r['recipient'], 'reaction': 'done'})
            return r

    async def get_request(self, project, identifier):
        async with self.reading() as c:
            return await self._get(c, 'request', project, identifier)

    async def snapshot(self, project):
        async with self.reading() as c:
            await self._project(c, project)
            return {kind: await self._list(c, kind, project) for kind in ('document', 'subscription', 'message', 'request', 'pending', 'decision', 'handoff', 'settings')}

    async def claim(self, owner="local"):
        await self.recover_requests()
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,data FROM collaboration_records WHERE kind='request' AND state IN ('queued','blocked','running') ORDER BY updated"))).mappings().all()
            from collections import Counter
            busy = Counter((row['project'], json.loads(row['data'])['recipient']) for row in rows if json.loads(row['data'])['state'] == 'running')
            if sum(busy.values()) >= self.max_running:
                return None
            rows = sorted(rows, key=lambda row: (-json.loads(row['data']).get('priority', 1), json.loads(row['data'])['created']))
            for row in rows:
                project, r = row['project'], json.loads(row['data'])
                paused = await self._get(c, 'settings', project, 'project', False)
                if paused and paused.get('paused'):
                    continue
                if r.get('blocked_reason') in {'merge_conflict', 'basis_changed'}:
                    continue
                if r['state'] == 'running' or busy[(project, r['recipient'])] >= self.role_concurrency:
                    continue
                deps = [await self._get(c, 'request', project, d) for d in r['dependencies']]
                if any(d['state'] in {'cancelled', 'rejected', 'failed'} for d in deps):
                    if r.get('blocked_reason') != 'dependency_failed':
                        r.update(state='blocked', blocked_reason='dependency_failed')
                        await self._put(c, 'request', project, r['id'], r)
                        await self._event(c, project, 'leader', {'type': 'dependency_failed', 'request_id': r['id']}, r['conversation'])
                    continue
                if any(d['state'] not in {'completed', 'review'} for d in deps):
                    continue
                if r['attempts'] >= 12:
                    r.update(state='failed', result='Request execution budget exhausted')
                    await self._put(c, 'request', project, r['id'], r)
                    continue
                r.pop('blocked_reason', None)
                r.update(state='running', attempts=r['attempts']+1, lease_owner=owner, lease_until=time.time()+60)
                if not r.get('workspace_id') or r.get('workspace_published'):
                    r.update(workspace_id=uuid4().hex, workspace_published=False)
                r['instance_id'] = uuid4().hex
                await self._put(c, 'request', project, r['id'], r)
                p = await self._project(c, project)
                return project, p['workspace_path'], r
        return None

    async def recover_requests(self):
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,id,data FROM collaboration_records WHERE kind='request' AND state='running'"))).mappings().all()
            for row in rows:
                r = json.loads(row['data'])
                if r.get('lease_until', 0) > time.time():
                    continue
                r.update(state='queued')
                await self._put(c, 'request', row['project'], row['id'], r)

    async def set_paused(self, project, paused):
        async with self.transaction() as c:
            await self._project(c, project)
            await self._put(c, 'settings', project, 'project', {'paused': paused})
        return {'paused': paused}


    async def section_diff(self, project, document_id, from_version, to_version, section_id, offset=0, limit=6000):
        if offset < 0 or not 1 <= limit <= 12000:
            raise ValueError('Invalid diff window')
        async with self.reading() as c:
            contents = []
            for version in (from_version, to_version):
                r = await self._get(c, 'revision', project, f'{document_id}:{version}')
                found = next((s for s in r['outline'] if s['id'] == section_id), None)
                contents.append(r['content'][found['start']:found['body_end']] if found else '')
            diff = ''.join(difflib.unified_diff(contents[0].splitlines(True), contents[1].splitlines(True), fromfile=f'v{from_version}', tofile=f'v{to_version}'))
            return {'diff': diff[offset:offset+limit], 'next_offset': offset+limit if len(diff) > offset+limit else None}

    async def search(self, project, query, offset=0, limit=20):
        if not query.strip() or len(query) > 200 or offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Invalid search query or window')
        async with self.reading() as c:
            await self._project(c, project)
            rows = (await c.execute(text("""SELECT document,version,section,title,body,
              instr(lower(body),lower(:q)) AS position FROM collaboration_sections
              WHERE project=:p AND instr(lower(body),lower(:q))>0
              ORDER BY document,section LIMIT :n OFFSET :offset"""),
              {'p': project, 'q': query, 'n': limit+1, 'offset': offset})).mappings().all()
            matches = [{'document_id': row['document'], 'version': row['version'], 'section_id': row['section'], 'title': row['title'],
                        'excerpt': row['body'][max(0,row['position']-81):row['position']+239]} for row in rows[:limit]]
            return {'matches': matches, 'next_offset': offset+limit if len(rows)>limit else None}


    async def read_cached(self, project, member, context, force=False, **payload):
        result = await self.read(project, **payload)
        key = digest(json.dumps([member, context, payload, result.get('version')], sort_keys=True))
        async with self.reading() as c:
            cached = await self._get(c, 'read', project, key, False)
        if cached and not force:
            return {'already_in_context': True, 'document_id': payload['document_id'], 'version': result['version'], 'section_id': payload.get('section_id'), 'hint': 'Use force=true to read again.'}, None
        return result, key

    async def confirm_reads(self, project, keys):
        async with self.transaction() as c:
            for key in keys:
                await self._put(c, 'read', project, key, {'confirmed': True})


    async def _cancel_dependents(self, c, project, identifier):
        requests = await self._list(c, 'request', project)
        cancelled = {identifier}
        while True:
            changed = False
            for r in requests:
                if r['state'] not in TERMINAL and r['id'] not in cancelled and (set(r['dependencies']) & cancelled or r.get('parent') in cancelled):
                    r.update(state='cancelled', result=f"Cancelled because prerequisite or parent {identifier} was cancelled")
                    cancelled.add(r['id'])
                    await self._put(c, 'request', project, r['id'], r)
                    await self._event(c, project, r['sender'], {'type': 'request_status', 'request_id': r['id'], 'state': 'cancelled', 'result': r['result']}, r['conversation'])
                    changed = True
            if not changed:
                break

    async def scheduling_tick(self, now=None):
        now = now or time.time()
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,data FROM collaboration_records WHERE kind='request' AND state NOT IN ('completed','cancelled','rejected','failed')"))).mappings().all()
            for row in rows:
                r = json.loads(row['data'])
                if r.get('deadline') and r['deadline'] <= now and not r.get('escalated_at'):
                    r.update(escalated_at=now, priority=min(3, r.get('priority', 1)+1))
                    await self._put(c, 'request', row['project'], r['id'], r)
                    await self._event(c, row['project'], 'leader', {'type': 'request_overdue', 'request_id': r['id'], 'state': r['state'], 'deadline': r['deadline']}, r['conversation'])

    async def schedule_request(self, project, identifier, actor, priority=None, deadline=None):
        async with self.transaction() as c:
            r = await self._get(c, 'request', project, identifier)
            if actor not in {r['sender'], 'leader'} or r['state'] in TERMINAL:
                raise ValueError('Only sender or leader may schedule an active request')
            if priority is not None:
                if priority not in range(4):
                    raise ValueError('Priority must be 0..3')
                r['priority'] = priority
            if deadline is not None:
                if deadline <= 0:
                    raise ValueError('Deadline must be a positive Unix timestamp')
                r['deadline'] = deadline
                r.pop('escalated_at', None)
            await self._put(c, 'request', project, identifier, r)
            return r

    async def dependency_graph(self, project):
        async with self.reading() as c:
            await self._project(c, project)
            requests = await self._list(c, 'request', project)
            return {'nodes': [{key: r.get(key) for key in ('id','goal','state','recipient','priority','deadline','blocked_reason')} for r in requests],
                    'edges': [{'from': r['id'], 'to': dependency, 'type': 'depends_on'} for r in requests for dependency in r['dependencies']]}

    async def record_decision(self, project, actor, title, rationale, links, facts=None, identifier=None, base_version=0, state='proposed', supersedes=None):
        if state not in {'proposed', 'accepted', 'superseded', 'rejected'} or not title.strip() or not rationale.strip():
            raise ValueError('A decision needs a title, rationale and valid state')
        if len(title) > 240 or len(rationale) > 4000 or len(links) > 30 or len(facts or {}) > 30:
            raise ValueError('Decision exceeds size limits')
        if actor != 'leader' and state != 'proposed':
            raise ValueError('Only Leader can accept, reject or supersede a decision')
        async with self.transaction() as c:
            await self._project(c, project)
            identifier = identifier or uuid4().hex
            previous = await self._get(c, 'decision', project, identifier, False)
            if base_version != (previous['version'] if previous else 0):
                raise StorageConflictError('Decision version changed')
            if previous and actor not in {previous['author'], 'leader'}:
                raise ValueError('Only the author or Leader may edit a decision')
            for link in links:
                await self._get(c, 'revision', project, f"{link['document_id']}:{link['version']}")
            if state == 'accepted':
                if not links:
                    raise ValueError('Accepted decisions must cite a published document revision')
                await self._dependencies(c, project, links)
            if supersedes:
                old = await self._get(c, 'decision', project, supersedes)
                if old['state'] != 'accepted' or state != 'accepted':
                    raise ValueError('Only an accepted decision may supersede another accepted decision')
                old.update(state='superseded', version=old['version']+1, superseded_by=identifier)
                await self._put(c, 'decision', project, old['id'], old)
                await self._put(c, 'decision_revision', project, f"{old['id']}:{old['version']}", old)
            decision = dict(id=identifier, version=base_version+1, author=previous['author'] if previous else actor,
                            title=title, rationale=rationale, links=links, facts=facts or {}, state=state,
                            supersedes=supersedes, created=time.time())
            await self._put(c, 'decision', project, identifier, decision)
            await self._put(c, 'decision_revision', project, f'{identifier}:{base_version+1}', decision)
            await self._event(c, project, 'leader', {'type': 'decision_changed', 'decision_id': identifier, 'state': state, 'title': title})
            return decision

    async def maintenance(self, project, stale_days=90):
        if stale_days < 1:
            raise ValueError('stale_days must be positive')
        async with self.reading() as c:
            await self._project(c, project)
            docs = await self._list(c, 'document', project)
            versions = {d['id']: d['version'] for d in docs}
            decisions = await self._list(c, 'decision', project)
            findings, bodies = [], []
            for d in docs:
                r = await self._get(c, 'revision', project, f"{d['id']}:{d['version']}")
                stale_dependencies = [ref for ref in r.get('based_on', []) if versions.get(ref['document_id']) != ref['version']]
                if stale_dependencies:
                    findings.append({'kind': 'stale_dependency', 'document_id': d['id'], 'dependencies': stale_dependencies})
                if time.time() - r['created'] > stale_days * 86400:
                    findings.append({'kind': 'review_due', 'document_id': d['id'], 'last_updated': r['created']})
                for heading in r['outline']:
                    body = ' '.join(r['content'][heading['body_start']:heading['body_end']].split()).casefold()
                    if len(body) >= 80:
                        bodies.append((d['id'], heading['id'], body))
            hashes = {}
            for doc, sid, body in bodies:
                h = digest(body)
                if h in hashes:
                    findings.append({'kind': 'duplicate_section', 'left': hashes[h], 'right': {'document_id': doc, 'section_id': sid}, 'requires_review': True})
                else:
                    hashes[h] = {'document_id': doc, 'section_id': sid}
            facts = {}
            for d in decisions:
                if d['state'] != 'accepted':
                    continue
                if any(versions.get(ref['document_id']) != ref['version'] for ref in d['links']):
                    findings.append({'kind': 'decision_evidence_changed', 'decision_id': d['id'], 'requires_review': True})
                for key, value in d['facts'].items():
                    if key in facts and facts[key]['value'] != value:
                        findings.append({'kind': 'contradictory_decisions', 'fact': key, 'left': facts[key], 'right': {'decision_id': d['id'], 'value': value}, 'requires_review': True})
                    else:
                        facts[key] = {'decision_id': d['id'], 'value': value}
            return {'findings': findings, 'coverage': 'Exact normalized section duplicates, explicit decision facts and version/age checks. Free-text semantic contradictions require review.'}

    async def handoff(self, project, member='leader', save=False, summary=''):
        async with self.transaction() as c:
            await self._project(c, project)
            docs = await self._list(c, 'document', project)
            requests = [r for r in await self._list(c, 'request', project) if r['state'] not in TERMINAL and (member == 'leader' or member in {r['sender'], r['recipient']})]
            decisions = [d for d in await self._list(c, 'decision', project) if d['state'] == 'accepted']
            handoff = {'created': time.time(), 'member': member, 'summary': summary[:2000],
                       'documents': [{'id': d['id'], 'version': d['version'], 'title': d['title']} for d in docs[:30]],
                       'pending': [{key: r.get(key) for key in ('id','state','recipient','dependencies','needs_review')} for r in requests[:30]],
                       'decisions': [{'id': d['id'], 'version': d['version'], 'title': d['title'], 'links': d['links']} for d in decisions[:20]],
                       'remaining': {'documents': max(0,len(docs)-30), 'pending': max(0,len(requests)-30), 'decisions': max(0,len(decisions)-20)}}
            if save:
                handoff['id'] = uuid4().hex
                await self._put(c, 'handoff', project, handoff['id'], handoff)
            return handoff


    async def _index_sections(self, c, project, revision):
        await c.execute(text('DELETE FROM collaboration_sections WHERE project=:p AND document=:d'), {'p': project, 'd': revision['id']})
        for heading in revision['outline']:
            await c.execute(text('INSERT INTO collaboration_sections(project,document,section,version,title,body) VALUES(:p,:d,:s,:v,:t,:b)'),
                            {'p': project, 'd': revision['id'], 's': heading['id'], 'v': revision['version'], 't': heading['title'], 'b': revision['content'][heading['start']:heading['body_end']]})

    async def rebuild_search(self):
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,data FROM collaboration_records WHERE kind='document'"))).mappings().all()
            for row in rows:
                d = json.loads(row['data'])
                if not await c.scalar(text('SELECT 1 FROM collaboration_sections WHERE project=:p AND document=:d AND version=:v LIMIT 1'), {'p': row['project'], 'd': d['id'], 'v': d['version']}):
                    r = await self._get(c, 'revision', row['project'], f"{d['id']}:{d['version']}")
                    await self._index_sections(c, row['project'], r)

    async def renew_leases(self, owner, active_requests):
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,id,data FROM collaboration_records WHERE kind='request' AND state='running'"))).mappings().all()
            for row in rows:
                r = json.loads(row['data'])
                if r.get('lease_owner') == owner and (row['project'], row['id']) in active_requests:
                    r['lease_until'] = time.time()+60
                    await self._put(c, 'request', row['project'], row['id'], r)

    async def prepare_workspace(self, project, identifier):
        async with self.transaction() as c:
            r = await self._get(c, 'request', project, identifier)
            p = await self._project(c, project)
            workspace = IsolatedWorkspace(p['workspace_path'], r['workspace_id'])
            await asyncio.to_thread(workspace.prepare)
            await asyncio.to_thread(workspace.refresh)
            return workspace

    async def publish_artifacts(self, project, identifier, instance_id):
        async with self.transaction() as c:
            r = await self._get(c, 'request', project, identifier)
            if r.get('instance_id') != instance_id or r['state'] not in {'running', 'review'} or r.get('lease_until',0) <= time.time():
                raise StorageConflictError('Request execution lease is no longer valid')
            if r.get('workspace_published'):
                return
            await self._dependencies(c, project, r['based_on'])
            if any(dep['state'] != 'completed' for dep in [await self._get(c, 'request', project, identifier) for identifier in r['dependencies']]):
                raise StorageConflictError('Accept dependency deliveries before publishing artifacts')
            p = await self._project(c, project)
            workspace = IsolatedWorkspace(p['workspace_path'], r['workspace_id'])
            for path in r['artifacts']:
                target = self._path(str(workspace.work), path)
                if not target.is_file():
                    raise ValueError(f'Expected artifact is missing: {path}')
            await asyncio.to_thread(workspace.recover_merge)
            # Finish an interrupted merge before considering a new plan.
            managed = {d['path'] for d in await self._list(c, 'document', project)}
            changes = await asyncio.to_thread(workspace.plan, managed)
            await asyncio.to_thread(workspace.publish, changes)
            r['workspace_published'] = True
            r['merged_paths'] = [item['path'] for item in changes]
            await self._put(c, 'request', project, identifier, r)

    async def mark_conflict(self, project, identifier, detail, instance_id=None):
        async with self.transaction() as c:
            r = await self._get(c, 'request', project, identifier)
            if r['state'] not in {'running', 'review'} or (instance_id and r.get('instance_id') != instance_id):
                return
            r.update(state='blocked', blocked_reason='basis_changed' if 'Dependency changed:' in detail else 'merge_conflict', result=detail[:2000])
            await self._put(c, 'request', project, identifier, r)
            await self._event(c, project, 'leader', {'type': 'merge_conflict', 'request_id': identifier, 'detail': detail[:1000]}, r['conversation'])


    async def inspect_message(self, project, identifier, offset=0, limit=4000):
        if offset < 0 or not 1 <= limit <= 8000:
            raise ValueError('Invalid message window')
        async with self.reading() as c:
            message = await self._get(c, 'message', project, identifier)
            content = message['content']
            return {**message, 'content': content[offset:offset+limit], 'next_offset': offset+limit if len(content)>offset+limit else None}


    async def history(self, project, document_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Invalid revision window')
        async with self.reading() as c:
            await self._get(c, 'document', project, document_id)
            rows = await c.scalars(text("""SELECT data FROM collaboration_records WHERE project=:p AND kind='revision'
              AND json_extract(data,'$.id')=:d ORDER BY CAST(json_extract(data,'$.version') AS INTEGER) DESC
              LIMIT :n OFFSET :offset"""), {'p': project, 'd': document_id, 'n': limit+1, 'offset': offset})
            revisions = [json.loads(row) for row in rows]
            return {'revisions': [{key: r.get(key) for key in ('version','summary','author','created','conversation','based_on','batch_id','merged_from_version')} for r in revisions[:limit]],
                    'next_offset': offset+limit if len(revisions)>limit else None}
