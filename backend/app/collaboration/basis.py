"""What a task is built on, recorded by the platform instead of declared by the member.

A task's basis is the documents its `needs` name plus its module of the goal's
requirements document; documents it merely looked at, and the ones it delivers
itself, are not. The platform records which version of which section each task
actually read. A delivery is compared section by section: only a section the task
read and that changed since blocks it, and the rejection names those sections, so
the member reads a few sections instead of the whole document again.
"""
from __future__ import annotations

from app.storage import StorageConflictError

BASIS_NEXT_ACTION = ('只读上面列出的这几节（collaboration_read 带 document_id 和 section_id）。和你的交付无关就直接再提交一次；'
                     '有关就只改受影响的部分。超出本任务验收标准的内容不要自己做，用 collaboration_send 报给 Leader。')
REMOVED_NEXT_ACTION = '你读过的章节被删除或改名了：先读一次这份文档的大纲（collaboration_read 只带 document_id），再读需要的章节。'


class BasisChanged(StorageConflictError):
    """A delivery rests on document sections that changed after the task read them."""

    def __init__(self, message, details=None, next_action=BASIS_NEXT_ACTION):
        super().__init__(message)
        self.details = details or {}
        self.next_action = next_action

    def result(self) -> dict:
        return {'error': str(self), **self.details, 'next_action': self.next_action,
                'error_details': {'category': 'operation_rejected', 'code': 'basis_changed'}}


def section_texts(revision: dict) -> dict:
    """Heading and direct body of every section: the unit a publication's changes are
    counted in. Trailing blank lines are not content: inserting a section after one
    moves its last newline without changing what it says."""
    return {s['id']: revision['content'][s['start']:s['body_end']].rstrip() for s in revision['outline']}


def within(revision: dict, section_id: str, ancestor: str) -> bool:
    parents = {s['id']: s['parent'] for s in revision['outline']}
    while section_id is not None:
        if section_id == ancestor:
            return True
        section_id = parents.get(section_id)
    return False


def read_sections(revision: dict, section_id=None, subtree=False, whole=False, offset=0, length=0) -> list[dict]:
    """The sections whose text a read returned. An outline read returns none."""
    outline = revision['outline']
    if whole:
        start, end = offset, offset + length
        return [s for s in outline if s['start'] < end and (s['body_end'] > start or s['body_end'] == s['start'] == start)]
    if section_id is None:
        return []
    target = next((s for s in outline if s['id'] == section_id), None)
    if target is None:
        return []
    if not subtree:
        return [target]
    start, end = target['body_start'] + offset, target['body_start'] + offset + length
    return [target, *[s for s in outline if s is not target and target['start'] <= s['start'] < target['end']
                      and s['start'] < end and s['body_end'] > start]]


class BasisMixin:
    """Methods of Collaboration; records are kind='basis', id=<request id>."""

    async def _revision(self, c, project, document_id, version, cache=None):
        key = (document_id, version)
        if cache is not None and key in cache:
            return cache[key]
        revision = await self._get(c, 'revision', project, f'{document_id}:{version}', False)
        if cache is not None:
            cache[key] = revision
        return revision

    async def record_read(self, project, request_id, result, section_id=None, subtree=False, whole=False, offset=0):
        """Remember which sections of which version the task has read. An outline read
        adds nothing, but forgets sections that no longer exist in that version."""
        document_id, version = result.get('document_id'), result.get('version')
        if not request_id or not document_id or not version:
            return
        async with self.reading() as c:
            revision = await self._revision(c, project, document_id, version)
            record = await self._get(c, 'basis', project, request_id, False) or {'request': request_id, 'documents': {}}
        if revision is None:
            return
        known = dict(record['documents'].get(document_id) or {})
        updated = dict(known)
        outline_only = not whole and section_id is None
        if outline_only:
            present = {s['id'] for s in revision['outline']}
            updated = {sid: info for sid, info in known.items() if sid in present or info['version'] > version}
        else:
            for section in read_sections(revision, section_id, subtree, whole, offset, len(result.get('content') or '')):
                if updated.get(section['id'], {}).get('version', 0) < version:
                    updated[section['id']] = {'version': version, 'title': section['title']}
        if updated == known:
            return
        async with self.transaction() as c:
            record = await self._get(c, 'basis', project, request_id, False) or {'request': request_id, 'documents': {}}
            record['documents'][document_id] = updated
            await self._put(c, 'basis', project, request_id, record)

    async def basis_documents(self, c, project, request) -> list[tuple[dict, str | None]]:
        """(document, module section or None) for every document the task is built on."""
        from app.collaboration.planning import same_document
        documents = await self._list(c, 'document', project)
        own = [d for d in documents if any(same_document(path, d) for path in request.get('documents') or ())]
        found: dict[str, tuple[dict, str | None]] = {}
        for need in request.get('needs') or ():
            if need.get('document'):
                for document in documents:
                    if same_document(need['document'], document):
                        found[document['id']] = (document, None)
        reference = request.get('module_ref') or {}
        if reference.get('document_id') and reference.get('section_id'):
            document = next((d for d in documents if d['id'] == reference['document_id']), None)
            if document and document['id'] not in found:
                # Only its own module: other modules of the same document are other tasks' work.
                found[document['id']] = (document, reference['section_id'])
        return [item for key, item in found.items() if not any(d['id'] == key for d in own)]

    async def _section_history(self, c, project, document_id, section_id, since, current, cache):
        """Versions after `since` in which the section's text changed, with their summaries."""
        versions, summaries, previous = [], [], None
        for version in range(since, current + 1):
            revision = await self._revision(c, project, document_id, version, cache)
            text = section_texts(revision).get(section_id) if revision else None
            if version > since and text != previous:
                versions.append(version)
                if revision and revision.get('summary'):
                    summaries.append(f"v{version}: {revision['summary'][:200]}")
            previous = text
        return versions, '; '.join(summaries)

    async def basis_changes(self, c, project, request, only=None) -> tuple[dict, list[tuple[str, dict]]]:
        """Sections the task read that changed or disappeared since, per basis document:
        ({documents, changed_sections, removed_sections}, and for documents that moved on
        without touching a read section, [(document_id, its reads at the current version)]).
        `only` restricts the comparison to given document IDs."""
        record = await self._get(c, 'basis', project, request['id'], False) or {'documents': {}}
        cache: dict = {}
        documents, changed, removed, current_reads = [], [], [], []
        for document, module in await self.basis_documents(c, project, request):
            reads = record['documents'].get(document['id']) or {}
            if not reads or (only is not None and document['id'] not in only):
                continue
            current = await self._revision(c, project, document['id'], document['version'], cache)
            now = section_texts(current)
            stale, bumped = False, {}
            for section_id, info in reads.items():
                bumped[section_id] = info
                if info['version'] >= document['version']:
                    continue
                old = await self._revision(c, project, document['id'], info['version'], cache)
                if old is None or (module and not within(old, section_id, module)):
                    continue
                stale = True
                if section_id not in now:
                    removed.append({'document_id': document['id'], 'section_id': section_id, 'title': info.get('title') or section_id})
                elif now[section_id] != section_texts(old).get(section_id):
                    versions, summary = await self._section_history(c, project, document['id'], section_id,
                                                                    info['version'], document['version'], cache)
                    changed.append({'document_id': document['id'], 'section_id': section_id,
                                    'title': next(s['title'] for s in current['outline'] if s['id'] == section_id),
                                    'changed_in': versions, 'summary': summary})
                else:
                    bumped[section_id] = {**info, 'version': document['version']}
            if stale:
                documents.append({'document_id': document['id'], 'path': document.get('path'), 'title': document.get('title'),
                                  'read_version': min(i['version'] for i in reads.values()), 'current_version': document['version']})
                current_reads.append((document['id'], bumped))
        affected = {item['document_id'] for item in [*changed, *removed]}
        details = {'documents': [d for d in documents if d['document_id'] in affected],
                   'changed_sections': changed, 'removed_sections': removed}
        return ({key: value for key, value in details.items() if value},
                [(document_id, reads) for document_id, reads in current_reads if document_id not in affected])

    async def basis_check(self, c, project, request, bump=True) -> list[str]:
        """Reject a delivery whose read sections changed; otherwise move the references
        to the current versions and say which documents moved on without touching it."""
        details, unaffected = await self.basis_changes(c, project, request)
        if details:
            changed, removed = details.get('changed_sections', []), details.get('removed_sections', [])
            parts = ([f'你读过的 {len(changed)} 个章节在读之后改了'] if changed else []) + (
                [f'你读过的 {len(removed)} 个章节被删除或改名了'] if removed else [])
            raise BasisChanged('，'.join(parts) + '，提交没有执行。', details,
                               ' '.join(([BASIS_NEXT_ACTION] if changed else []) + ([REMOVED_NEXT_ACTION] if removed else [])))
        notes = []
        record = await self._get(c, 'basis', project, request['id'], False)
        documents = {d['id']: d for d in await self._list(c, 'document', project)}
        for document_id, reads in unaffected:
            if document_id not in documents:
                continue
            document = documents[document_id]
            notes.append(f"{document.get('path') or document_id} 已到 v{document['version']}，改动不在你读过的章节里。")
            if bump and record:
                record['documents'][document_id] = reads
        if bump and record and notes:
            await self._put(c, 'basis', project, request['id'], record)
        return notes

    async def _dependencies(self, c, project, based_on):
        """Explicit document references (a document citing another, a decision, an API
        caller's based_on) are compared by whole version; the error names the document
        and the sections that changed."""
        for dependency in based_on or ():
            document = await self._get(c, 'document', project, dependency['document_id'])
            if document['version'] == dependency['version']:
                continue
            cache: dict = {}
            old = await self._revision(c, project, document['id'], dependency['version'], cache)
            current = await self._revision(c, project, document['id'], document['version'], cache)
            before, now = (section_texts(old) if old else {}), (section_texts(current) if current else {})
            titles = {s['id']: s['title'] for r in (old, current) if r for s in r['outline']}
            sections = [{'document_id': document['id'], 'section_id': key, 'title': titles.get(key)}
                        for key in sorted(before.keys() | now.keys(), key=lambda k: str(titles.get(k)))
                        if before.get(key) != now.get(key)][:20]
            raise BasisChanged(
                f"Dependency changed: {document['id']} is v{document['version']} ({document.get('path')}, "
                f"referenced at v{dependency['version']})",
                {'documents': [{'document_id': document['id'], 'path': document.get('path'), 'title': document.get('title'),
                                'read_version': dependency['version'], 'current_version': document['version']}],
                 'changed_sections': sections},
                '只读上面列出的章节，确认引用仍然成立后改用当前版本号再试。')
