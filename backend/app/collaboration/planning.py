"""Task planning rules enforced by the platform, not only by prompts.

- Typed deliverables: files/directories (artifacts), shared documents published with
  collaboration_publish (documents) and services that must be running and verified
  in the environment registry (services). Contradictions are rejected up front.
- Deliverable-level dependencies (needs): a task starts when a document is delivered
  (its producing task submitted it, or its author marked it ready) or a service is
  verified, instead of waiting for a whole task. Progressive publication alone only
  saves work in progress; it does not release dependents.
- Declared documents have a length budget by goal scale, stated to the author and
  reported (never enforced) when a publication crosses it.
- Goal scale bounds the number of criteria and is passed to every assignee.
- Verification ownership: at most one end-to-end verification task per goal; other
  tasks verify only their own deliverable; duplicated criteria are flagged.
"""
from __future__ import annotations

import posixpath
import re

SCALES = ('small', 'medium', 'large')
CRITERIA_LIMITS = {'small': {'goal': 3, 'task': 3}, 'medium': {'goal': 6, 'task': 5}, 'large': {'goal': 8, 'task': 8}}
SCALE_GUIDANCE = {
    'small': 'Goal scale small: deliver the minimal implementation and documents the criteria require; no extra endpoints, '
             'configuration options, styling or documentation sections beyond them.',
    'medium': 'Goal scale medium: cover the criteria with proportionate structure; avoid speculative features.',
    'large': 'Goal scale large: plan for maintainability, but still deliver only what the criteria and documents require.',
}
# Characters per declared document. Past runs wrote a 68K-character design and a 26K
# contract for a personal blog; every later reader and editor paid for that length.
DOCUMENT_BUDGETS = {
    'small': {'requirements': 4000, 'design': 4000, 'contract': 4000, 'report': 4000, 'generic': 3000},
    'medium': {'requirements': 10000, 'design': 15000, 'contract': 15000, 'report': 8000, 'generic': 8000},
    'large': {'requirements': 20000, 'design': 30000, 'contract': 30000, 'report': 15000, 'generic': 15000},
}


def goal_scale(goal: dict | None) -> str | None:
    """None for goals created without a scale (user-created or older goals): no budget."""
    return (goal or {}).get('scale')


def check_scale(scale):
    if scale is not None and scale not in SCALES:
        raise ValueError(f'scale must be one of {SCALES}')


def check_criteria_budget(criteria, scale, level):
    if scale is None:
        return
    limit = CRITERIA_LIMITS[scale][level]
    if len(criteria) > limit:
        raise ValueError(f'A {scale} goal allows at most {limit} criteria per {level}; merge related checks, move '
                         'verification details into the task text, or raise the goal scale if the request is genuinely larger.')


def normalize_needs(needs) -> list[dict]:
    result = []
    for need in needs or ():
        kinds = [key for key in ('document', 'service') if need.get(key)]
        if len(kinds) != 1:
            raise ValueError('Each need names exactly one document (path or ID) or one service')
        item = {kinds[0]: str(need[kinds[0]])}
        if need.get('min_version'):
            if kinds[0] != 'document' or int(need['min_version']) < 1:
                raise ValueError('min_version applies to documents and must be >= 1')
            item['min_version'] = int(need['min_version'])
        result.append(item)
    return result


def check_deliverables(artifacts, documents, services):
    """Validate typed deliverables. A path declared as a shared document is that one
    deliverable (published, not merged as a file), so it is dropped from artifacts
    rather than rejected; returns the artifacts to store."""
    from app.collaboration.environment import SERVICE_NAME
    for name in services:
        if not SERVICE_NAME.match(str(name)):
            raise ValueError(f'Service name {name!r} must be a registry name such as api or web')
    shared = {deliverable_path(path) for path in documents}
    return [path for path in artifacts if deliverable_path(path) not in shared]


def project_relative(text, root) -> str:
    """Rewrite absolute paths inside the project as project-relative ones."""
    if not isinstance(text, str) or not root:
        return text
    root = str(root).rstrip('/')
    return re.sub(re.escape(root) + r'(/|(?![\w-]))', lambda m: '' if m.group(1) else 'the project root', text)


def deliverable_path(path) -> str:
    return posixpath.normpath(str(path).replace('\\', '/'))


def implementation_paths(artifacts) -> list[str]:
    return [path for path in artifacts or () if not str(path).replace('\\', '/').lstrip('./').startswith('docs/')]


def serial_dependency(recipient, dependencies, serial_reason):
    """Reject a frontend task waiting for a backend implementation without a reason:
    it needs the published contract, which `needs` expresses directly."""
    if recipient != 'frontend' or (serial_reason or '').strip():
        return
    blocking = [d['id'] for d in dependencies if d.get('recipient') == 'backend' and implementation_paths(d.get('artifacts'))]
    if blocking:
        raise ValueError(
            f'This frontend task would wait for backend implementation task(s) {blocking}. Frontend needs the API contract, '
            'not the implementation: let the backend task declare the contract in documents and give this task '
            "needs=[{document: '<contract path>'}] so it starts as soon as the contract is published. If it truly needs the "
            'running backend, give serial_reason.')


async def still_draft(service, c, project, document) -> bool:
    """A platform-created placeholder nobody has filled in yet."""
    draft = await service._get(c, 'draft', project, document['id'], False)
    return bool(draft and document['version'] <= draft['version'])


ABANDONED = {'cancelled', 'rejected', 'failed'}


def same_document(declared, document) -> bool:
    return declared == document['id'] or deliverable_path(declared) == deliverable_path(document.get('path') or '')


async def document_released(service, c, project, document) -> bool:
    """Whether dependents may build on a document: its author marked it ready, or a task
    declaring it has delivered (review or completed). A document no live task declares
    (written by the user or an earlier goal) is released once it exists."""
    if await service._get(c, 'ready', project, document['id'], False):
        return True
    producers = [r for r in await service._list(c, 'request', project)
                 if r['state'] not in ABANDONED and any(same_document(d, document) for d in r.get('documents') or ())]
    return not producers or any(r['state'] in {'review', 'completed'} for r in producers)


async def need_satisfied(service, c, project, need) -> bool:
    if 'service' in need:
        # A service the user connected (a database) is provided, never delivered.
        if await service.connected_service(project, need['service']):
            return True
        record = await service._get(c, 'service', project, need['service'], False)
        return bool(record and record.get('status') == 'verified')
    target = need['document']
    for document in await service._list(c, 'document', project):
        if target in {document['id'], document.get('path')} or same_document(target, document):
            return (document['version'] >= need.get('min_version', 1) and not await still_draft(service, c, project, document)
                    and await document_released(service, c, project, document))
    return False


DRAFT_SECTIONS = {
    'contract': (['接口概览', '接口列表', '错误约定', '兼容与变更'], ['Overview', 'Endpoints', 'Errors', 'Compatibility']),
    'report': (['验证范围', '检查结果', '缺陷与阻塞', '证据'], ['Scope', 'Results', 'Defects and blockers', 'Evidence']),
    'design': (['目标', '布局与组件', '样式规范', '素材'], ['Goals', 'Layout and components', 'Style', 'Assets']),
    'requirements': (['背景与目标', '范围', '验收标准', '待确认问题'], ['Background and goals', 'Scope', 'Acceptance criteria', 'Open questions']),
    'generic': (['概要', '内容', '待办'], ['Summary', 'Details', 'Open items']),
}
DRAFT_KINDS = [('report', r'report|verification|test|qa|验证|测试'), ('contract', r'contract|api|interface|openapi|schema|契约|接口'),
               ('design', r'design|style|ui|视觉|设计'), ('requirements', r'prd|requirement|spec|需求')]


def document_kind(path: str) -> str:
    return next((kind for kind, pattern in DRAFT_KINDS if re.search(pattern, str(path), re.I)), 'generic')


def document_budget(scale, path) -> int | None:
    return DOCUMENT_BUDGETS[scale][document_kind(path)] if scale in DOCUMENT_BUDGETS else None


def budget_line(scale, paths) -> str:
    budgets = [(path, document_budget(scale, path)) for path in paths]
    listed = '; '.join(f'{path} about {budget:,}' for path, budget in budgets if budget)
    if not listed:
        return ''
    return (f'Length budget in characters for this {scale} goal: {listed}. Cover the criteria within it: tables, one '
            'representative example per pattern and references to other documents instead of enumerating every case or '
            'repeating them. Every colleague who reads or edits the document pays for its length.')


PENDING = ('（待填写）', '(to be written)')


def unfilled(content: str) -> bool:
    """Whether a document still has platform draft placeholders."""
    return any(f'\n{mark}\n' in content for mark in PENDING)


def draft_content(path: str, author: str, chinese: bool) -> str:
    from pathlib import PurePosixPath
    sections = DRAFT_SECTIONS[document_kind(path)][0 if chinese else 1]
    title = PurePosixPath(path).stem.replace('-', ' ').replace('_', ' ')
    note = f'> 草稿：{author} 正在编写，完成的章节会逐步更新。' if chinese else f'> Draft: {author} is writing this; sections are filled in as work completes.'
    pending = PENDING[0] if chinese else PENDING[1]
    return f'# {title}\n\n{note}\n\n' + ''.join(f'## {heading}\n\n{pending}\n\n' for heading in sections)


def _tokens(text):
    words = re.findall(r'[a-z0-9]+', text.lower())
    cjk = re.findall(r'[一-鿿]', text)
    return set(words) | {a + b for a, b in zip(cjk, cjk[1:])}


def duplicate_criteria(criteria, others, threshold=.6):
    """Pairs of (criterion, other task, other criterion) that check the same thing."""
    found = []
    for criterion in criteria:
        mine = _tokens(criterion)
        for task_id, theirs in others:
            for other in theirs:
                tokens = _tokens(other)
                if mine and tokens and len(mine & tokens) / len(mine | tokens) >= threshold:
                    found.append((criterion, task_id, other))
    return found


OUTLINE_LIMIT = 150


def outline_view(outline) -> list[dict]:
    """Section IDs and sizes for the next edits, without offsets."""
    return [{'id': h['id'], 'title': h['title'], 'level': h['level'], 'characters': h.get('characters', 0)}
            for h in outline[:OUTLINE_LIMIT] if h['id'] != 'root']


class PlanningMixin:
    async def publication_receipt(self, project, revisions, request_id=None) -> dict:
        """What the author needs for the next edit: the new outline (section IDs change with
        headings), the length and, for documents its task declares, the length budget."""
        async with self.reading() as c:
            request = await self._get(c, 'request', project, request_id, False) if request_id else None
            goal = await self._get(c, 'goal', project, request['goal_id'], False) if request and request.get('goal_id') else None
            scale = goal_scale(goal)
            declared = (request or {}).get('documents') or ()
            receipt, notices, hints = [], [], []
            for item in revisions:
                revision = await self._get(c, 'revision', project, f"{item['document_id']}:{item['version']}")
                entry = {**item, 'path': revision['path'], 'characters': len(revision['content']),
                         'outline': outline_view(revision['outline'])}
                if len(revision['outline']) > OUTLINE_LIMIT + 1:
                    entry['outline_truncated'] = True
                budget = document_budget(scale, revision['path']) if any(same_document(d, revision) for d in declared) else None
                if budget:
                    entry['budget'] = budget
                    previous = await self._get(c, 'revision', project, f"{item['document_id']}:{item['version'] - 1}", False)
                    # Only the publication that crosses the budget says so; repeating it invites shrinking loops.
                    if len(revision['content']) > budget >= (len(previous['content']) if previous else 0):
                        notices.append(f"{revision['path']} is now {len(revision['content']):,} characters, over its budget of about "
                                       f"{budget:,} for this {scale} goal. Do not rewrite or delete finished sections to shrink "
                                       'it; keep any remaining sections brief and add no detail beyond the criteria.')
                if await self._get(c, 'ready', project, item['document_id'], False):
                    entry['ready'] = True
                elif any(same_document(d, revision) for d in declared) and await self.became_complete(c, project, revision):
                    # Once, when the last placeholder is filled: tasks waiting on the document
                    # start on ready=true, not when the author's whole task is delivered.
                    hints.append(f"{revision['path']} has no unwritten sections left. If it is complete, publish your next "
                                 'change to it with ready=true (a change with no edits only marks it) so tasks waiting on it '
                                 'start now; then make at most one read-through pass of fixes.')
                receipt.append(entry)
        return {'revisions': receipt, **({'budget_notice': ' '.join(notices)} if notices else {}),
                **({'ready_hint': ' '.join(hints)} if hints else {})}

    async def became_complete(self, c, project, revision) -> bool:
        """This revision is the first without draft placeholders: the previous one had some,
        was the unfilled platform draft, or did not exist."""
        if unfilled(revision['content']):
            return False
        draft = await self._get(c, 'draft', project, revision['id'], False)
        if draft and revision['version'] <= draft['version']:
            return False
        previous = await self._get(c, 'revision', project, f"{revision['id']}:{revision['version'] - 1}", False)
        return (previous is None or unfilled(previous['content'])
                or bool(draft and previous['version'] <= draft['version']))

    async def create_document_drafts(self, project, request) -> list[dict]:
        """Publish a sectioned placeholder for each declared document that does not exist
        yet, so the assignee fills it progressively instead of writing it all at the end."""
        async with self.reading() as c:
            published = {d.get('path') for d in await self._list(c, 'document', project)}
        missing = [path for path in request.get('documents') or () if path not in published]
        if not missing:
            return []
        from app.agents.identity import member_name
        chinese = bool(re.search(r'[\u4e00-\u9fff]', request.get('goal', '')))
        try:
            author = member_name(request['recipient'])
        except KeyError:
            author = request['recipient']
        revisions = await self.publish(project, request['recipient'], [
            {'path': path, 'base_version': 0, 'summary': 'Draft created when the task started', 'content': draft_content(path, author, chinese)}
            for path in missing], request.get('conversation', ''))
        async with self.transaction() as c:
            for revision in revisions:
                await self._put(c, 'draft', project, revision['document_id'], {'request': request['id'], 'version': revision['version']})
        return revisions

    async def draft_brief(self, project, request) -> str:
        async with self.reading() as c:
            documents = {d.get('path'): d for d in await self._list(c, 'document', project)}
            drafts = [documents[path] for path in request.get('documents') or ()
                      if path in documents and await still_draft(self, c, project, documents[path])]
        if not drafts:
            return ''
        listed = ', '.join(f"{d['path']} (document_id={d['id']}, version={d['version']})" for d in drafts)
        return (f'Draft documents were created for your declared documents: {listed}. Read the outline and fill sections with '
                'collaboration_publish (replace_section_body) as each part is done, or, if the outline does not fit, replace '
                'the whole draft once with changes=[{document_id, base_version, summary, content}]. Publishing saves your '
                'progress; tasks waiting on these documents start when you submit your task, or earlier when you publish a '
                'change with ready=true once the document is complete enough for them to build on. Delivery requires the '
                'drafts to be filled.')

    async def task_brief(self, project, request) -> str:
        """Deliverable, dependency, scale and verification facts for the assignee."""
        lines = []
        if request.get('documents'):
            lines.append(f"Documents to publish with collaboration_publish: {request['documents']}. When every section is "
                         'written, publish with ready=true so tasks that need a document start without waiting for your '
                         'whole task; after that, make at most one read-through pass of fixes.')
            async with self.reading() as c:
                waiting = [(r, need['document']) for r in await self._list(c, 'request', project)
                           if r['id'] != request['id'] and r['state'] not in ABANDONED | {'completed', 'review'}
                           for need in r.get('needs') or () if need.get('document')
                           and any(deliverable_path(need['document']) == deliverable_path(d) for d in request['documents'])]
            if waiting:
                listed = ', '.join(sorted({f"{r['recipient']} ({path})" for r, path in waiting}))
                lines.append(f'Waiting on your documents: {listed}. They start as soon as you publish a change with '
                             'ready=true, or when you submit; never mark a document with placeholder sections ready.')
        if request.get('services'):
            lines.append(f"Services to deliver running: {request['services']}. Start each with execute_command(background=true, "
                         "ready_port=<its port>) and deliver while it serves; it is kept running after your task for "
                         "colleagues and the user (a running copy is replaced only with keep_running=true).")
        if request.get('needs'):
            lines.append(f"This task started when these deliverables became available: {request['needs']}.")
        if request.get('goal_id'):
            async with self.reading() as c:
                goal = await self._get(c, 'goal', project, request['goal_id'], False)
                tasks = [r for r in await self._list(c, 'request', project) if r.get('goal_id') == request['goal_id']]
            if goal_scale(goal):
                lines.append(SCALE_GUIDANCE[goal_scale(goal)])
                if request.get('documents'):
                    lines.append(budget_line(goal_scale(goal), request['documents']))
            owner = next((t for t in tasks if t.get('verification') == 'end_to_end' and t['state'] not in {'cancelled', 'rejected'}), None)
            if request.get('verification') == 'end_to_end':
                lines.append('You own the end-to-end verification of this goal: verify the integrated result against the goal '
                             'criteria and record evidence. Other tasks only checked their own deliverables. Services marked '
                             'running in the environment are the delivered ones: verify against them; restart one only if it '
                             'is stopped or stale.')
            elif owner:
                lines.append(f"Verify only your own deliverable. End-to-end verification belongs to task {owner['id']} "
                             f"({owner['recipient']}); do not repeat it.")
        return '\n'.join(lines)
