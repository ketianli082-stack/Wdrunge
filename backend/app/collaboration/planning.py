"""Task planning rules enforced by the platform, not only by prompts.

- Typed deliverables: files/directories (artifacts), shared documents published with
  collaboration_publish (documents) and services that must be running and verified
  in the environment registry (services). Contradictions are rejected up front.
- Deliverable-level dependencies (needs): a task can start as soon as a document is
  published or a service is verified, instead of waiting for a whole task.
- Goal scale bounds the number of criteria and is passed to every assignee.
- Verification ownership: at most one end-to-end verification task per goal; other
  tasks verify only their own deliverable; duplicated criteria are flagged.
"""
from __future__ import annotations

import re

SCALES = ('small', 'medium', 'large')
CRITERIA_LIMITS = {'small': {'goal': 3, 'task': 3}, 'medium': {'goal': 6, 'task': 5}, 'large': {'goal': 8, 'task': 8}}
SCALE_GUIDANCE = {
    'small': 'Goal scale small: deliver the minimal implementation and documents the criteria require; no extra endpoints, '
             'configuration options, styling or documentation sections beyond them.',
    'medium': 'Goal scale medium: cover the criteria with proportionate structure; avoid speculative features.',
    'large': 'Goal scale large: plan for maintainability, but still deliver only what the criteria and documents require.',
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
    clash = sorted({str(p).strip('./') for p in artifacts} & {str(p).strip('./') for p in documents})
    if clash:
        raise ValueError(f'{clash} declared both as files (artifacts) and as shared documents (documents). Shared documents '
                         'are published with collaboration_publish and checked in project storage; declare each path once.')
    from app.collaboration.environment import SERVICE_NAME
    for name in services:
        if not SERVICE_NAME.match(str(name)):
            raise ValueError(f'Service name {name!r} must be a registry name such as api or web')


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


async def need_satisfied(service, c, project, need) -> bool:
    if 'service' in need:
        record = await service._get(c, 'service', project, need['service'], False)
        return bool(record and record.get('status') == 'verified')
    target = need['document']
    for document in await service._list(c, 'document', project):
        if target in {document['id'], document.get('path')}:
            return document['version'] >= need.get('min_version', 1) and not await still_draft(service, c, project, document)
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


def draft_content(path: str, author: str, chinese: bool) -> str:
    from pathlib import PurePosixPath
    kind = next((kind for kind, pattern in DRAFT_KINDS if re.search(pattern, path, re.I)), 'generic')
    sections = DRAFT_SECTIONS[kind][0 if chinese else 1]
    title = PurePosixPath(path).stem.replace('-', ' ').replace('_', ' ')
    note = f'> 草稿：{author} 正在编写，完成的章节会逐步更新。' if chinese else f'> Draft: {author} is writing this; sections are filled in as work completes.'
    pending = '（待填写）' if chinese else '(to be written)'
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


class PlanningMixin:
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
                'collaboration_publish (replace_section_body) as each part is done; tasks waiting on them start after your '
                'first update. Delivery requires the drafts to be filled.')

    async def task_brief(self, project, request) -> str:
        """Deliverable, dependency, scale and verification facts for the assignee."""
        lines = []
        if request.get('documents'):
            lines.append(f"Documents to publish with collaboration_publish: {request['documents']}.")
        if request.get('services'):
            lines.append(f"Services to deliver running and verified (start with execute_command ready_port): {request['services']}.")
        if request.get('needs'):
            lines.append(f"This task started when these deliverables became available: {request['needs']}.")
        if request.get('goal_id'):
            async with self.reading() as c:
                goal = await self._get(c, 'goal', project, request['goal_id'], False)
                tasks = [r for r in await self._list(c, 'request', project) if r.get('goal_id') == request['goal_id']]
            if goal_scale(goal):
                lines.append(SCALE_GUIDANCE[goal_scale(goal)])
            owner = next((t for t in tasks if t.get('verification') == 'end_to_end' and t['state'] not in {'cancelled', 'rejected'}), None)
            if request.get('verification') == 'end_to_end':
                lines.append('You own the end-to-end verification of this goal: verify the integrated result against the goal '
                             'criteria and record evidence. Other tasks only checked their own deliverables.')
            elif owner:
                lines.append(f"Verify only your own deliverable. End-to-end verification belongs to task {owner['id']} "
                             f"({owner['recipient']}); do not repeat it.")
        return '\n'.join(lines)
