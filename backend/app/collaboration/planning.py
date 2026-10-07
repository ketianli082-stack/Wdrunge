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
# The theme has a hard limit instead (THEME_LIMIT), whatever the scale.
DOCUMENT_BUDGETS = {
    'small': {'requirements': 4000, 'contract': 4000, 'report': 4000, 'generic': 3000},
    'medium': {'requirements': 10000, 'contract': 15000, 'report': 8000, 'generic': 8000},
    'large': {'requirements': 20000, 'contract': 30000, 'report': 15000, 'generic': 15000},
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
    'generic': (['概要', '内容', '待办'], ['Summary', 'Details', 'Open items']),
}
# A document's kind is read from whole words of its path: matched as substrings,
# "requirements" contains "ui" and was taken for a design document.
DRAFT_KINDS = [(kind, re.compile(rf'(?<![a-z0-9])(?:{words})(?![a-z0-9])|{cjk}', re.I)) for kind, words, cjk in (
    ('report', r'reports?|verification|tests?|testing|qa', r'验证|测试'),
    ('contract', r'contracts?|apis?|interfaces?|openapi|schemas?', r'契约|接口'),
    ('requirements', r'prd|requirements?|specs?', r'需求'),
    ('design', r'designs?|styles?|themes?|ui|ux|visual', r'视觉|设计|主题'))]


def document_kind(path: str) -> str:
    return next((kind for kind, pattern in DRAFT_KINDS if pattern.search(str(path))), 'generic')


def document_budget(scale, path) -> int | None:
    return DOCUMENT_BUDGETS[scale].get(document_kind(path)) if scale in DOCUMENT_BUDGETS else None


def budget_line(scale, paths) -> str:
    budgets = [(path, document_budget(scale, path)) for path in paths]
    listed = '; '.join(f'{path} about {budget:,}' for path, budget in budgets if budget)
    if not listed:
        return ''
    return (f'Length budget in characters for this {scale} goal: {listed}. Cover the criteria within it: tables, one '
            'representative example per pattern and references to other documents instead of enumerating every case or '
            'repeating them. Every colleague who reads or edits the document pays for its length.')


PENDING = ('（待填写）', '(to be written)')

# Drafts that are themselves the example of a document's structure: placeholders only,
# never sample content (a weak model copies sample content). They reach the author
# once, in its task, instead of being resent in every system prompt.
DRAFT_TEMPLATES = {
    'requirements': ("""<概述：目标、假设、不包含，不超过 400 字>

# <模块名>

## 需求1：<一句话标题>

<谁、在哪个页面、能做什么；页面上的区域和操作；数据和规则；为空或出错时显示什么>

## 需求2：<一句话标题>

<同上>

## 验收标准

- <能在浏览器里操作并看到的一条结果>

# <下一个模块名>

## 需求1：<一句话标题>

<同上>

## 验收标准

- <能在浏览器里操作并看到的一条结果>
""", """<Overview: goal, assumptions, out of scope, at most 400 characters>

# <module name>

## Requirement 1: <one-line title>

<who, on which page, can do what; areas and actions on the page; data and rules; what shows when empty or on error>

## Requirement 2: <one-line title>

<as above>

## Acceptance criteria

- <one result that can be operated and seen in the browser>

# <next module name>

## Requirement 1: <one-line title>

<as above>

## Acceptance criteria

- <one result that can be operated and seen in the browser>
"""),
    'design': ("""## 气质

<3–5 个关键词>

## 色调倾向

<明亮或深色；色调用文字描述，不写色值；疏朗或紧凑；圆润或硬朗；字体气质>

## 素材清单

| 文件路径 | 用途 | 尺寸 |
|---|---|---|
| <素材文件路径> | <用途> | <尺寸> |
""", """## Character

<3-5 keywords>

## Tone

<light or dark; tone in words, no color values; airy or compact; rounded or sharp; type character>

## Assets

| File path | Purpose | Size |
|---|---|---|
| <asset file path> | <purpose> | <size> |
"""),
    'report': ("""## <模块名>

| 检查项 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|
| <验收标准原文> | <预期结果> | <实际结果> | <通过或未通过> | <截图路径> |

## 缺陷与阻塞

<没有就写“无”>
""", """## <module name>

| Check | Expected | Actual | Verdict | Evidence |
|---|---|---|---|---|
| <acceptance criterion> | <expected result> | <actual result> | <pass or fail> | <screenshot path> |

## Defects and blockers

<write "none" when there are none>
"""),
}
PLACEHOLDERS = frozenset(token for pair in DRAFT_TEMPLATES.values() for text in pair for token in re.findall(r'<[^<>\n]+>', text))


def placeholders_left(content: str) -> list[str]:
    return sorted(token for token in PLACEHOLDERS if token in content) + [mark for mark in PENDING if f'\n{mark}\n' in content]


def unfilled(content: str) -> bool:
    """Whether a document still has platform draft placeholders."""
    return bool(placeholders_left(content))


DRAFT_NOTE = re.compile(r'^> (?:草稿：|Draft: ).*\n?', re.M)
OVERVIEW_LIMIT = 400


def authored(text: str) -> str:
    """The author's own text, without the platform's draft note; what length limits count."""
    return DRAFT_NOTE.sub('', text).strip()


def draft_content(path: str, author: str, chinese: bool) -> str:
    from pathlib import PurePosixPath
    kind = document_kind(path)
    note = f'> 草稿：{author} 正在编写，完成的章节会逐步更新。' if chinese else f'> Draft: {author} is writing this; sections are filled in as work completes.'
    if kind in DRAFT_TEMPLATES:
        note += '尖括号里是要替换掉的占位说明。' if chinese else ' Text in angle brackets is a placeholder to replace.'
    if kind == 'requirements':
        # Every level-1 heading is a module, so this draft has no title heading.
        return f'{note}\n\n' + DRAFT_TEMPLATES[kind][0 if chinese else 1]
    title = PurePosixPath(path).stem.replace('-', ' ').replace('_', ' ')
    if kind in DRAFT_TEMPLATES:
        return f'# {title}\n\n{note}\n\n' + DRAFT_TEMPLATES[kind][0 if chinese else 1]
    pending = PENDING[0] if chinese else PENDING[1]
    return f'# {title}\n\n{note}\n\n' + ''.join(f'## {heading}\n\n{pending}\n\n' for heading in DRAFT_SECTIONS[kind][0 if chinese else 1])


# --- requirements documents ----------------------------------------------------
# A requirements document is organised for development by module: every level-1
# heading is a module; its level-2 headings are requirements plus one acceptance
# criteria section. Developers read one module instead of the whole document.
REQUIREMENT_HEADING = re.compile(r'\s*(需求|requirement)', re.I)
CRITERIA_HEADING = re.compile(r'\s*(验收标准|acceptance criteria)\s*$', re.I)
LIST_ITEM = re.compile(r'^(?:[-*+]|\d+[.)])\s+\S', re.M)
MODULE_LIMITS = {'requirements': 5, 'criteria': 5}
INIT_MODULE = 'init'


def requirement_modules(content: str) -> list[dict]:
    """The modules of a requirements document, in order: section ID, title, requirement
    headings, acceptance criteria (None when the section is missing) and the IDs of
    every section inside the module."""
    from app.collaboration.documents import outline
    modules = []
    for entry in outline(content):
        if entry['level'] == 1:
            modules.append({'id': entry['id'], 'title': entry['title'].strip(), 'requirements': [], 'criteria': None,
                            'sections': [entry['id']]})
        elif modules and entry['level'] > 1:
            module = modules[-1]
            module['sections'].append(entry['id'])
            if entry['level'] == 2 and CRITERIA_HEADING.match(entry['title']):
                module['criteria'] = LIST_ITEM.findall(content[entry['body_start']:entry['end']])
            elif entry['level'] == 2 and REQUIREMENT_HEADING.match(entry['title']):
                module['requirements'].append(entry['title'].strip())
    return modules


def requirements_problems(content: str) -> list[str]:
    """Why a requirements document cannot be built from yet, naming the module and the
    section at fault; empty when it is well formed."""
    modules = requirement_modules(content)
    if not modules:
        return ['文档没有一级标题。每个模块用一个一级标题（# 模块名），模块下用二级标题写“## 需求N：标题”和“## 验收标准”。']
    problems = []
    for module in modules:
        name = module['title']
        if not module['requirements'] and module['criteria'] is None:
            # Seen with the real model: the overview written under a level-1 heading of its own.
            problems.append(f'“{name}”是一级标题，但下面没有需求和验收标准。一级标题只用于模块；概述等其他内容写在第一个标题之前，不加标题。')
            continue
        if not module['requirements']:
            problems.append(f'模块“{name}”没有需求：至少加一个二级标题“## 需求1：一句话标题”。')
        if module['criteria'] is None:
            problems.append(f'模块“{name}”缺少二级标题“## 验收标准”。')
        elif not module['criteria']:
            problems.append(f'模块“{name}”的“验收标准”是空的：每条写成一个列表项（- …）。')
        for key, label in (('requirements', '需求'), ('criteria', '验收标准')):
            if len(module[key] or ()) > MODULE_LIMITS[key]:
                problems.append(f'模块“{name}”有 {len(module[key])} 条{label}，上限是 {MODULE_LIMITS[key]} 条：拆成两个模块。')
    overview = authored(content[:next((m.start() for m in re.finditer(r'^#', content, re.M)), len(content))])
    if len(overview) > OVERVIEW_LIMIT:
        # Blog8: 7 assumptions, a trade-off list and the module list made it 960 characters.
        problems.append(f'第一个标题之前的概述有 {len(overview)} 字，上限是 {OVERVIEW_LIMIT} 字：只写目标、影响范围的假设和不包含；'
                        '属于某个模块的规则写进那个模块的需求，不写取舍理由。')
    left = placeholders_left(content)
    if left:
        problems.append(f'文档里还有草稿占位符没有替换：{"、".join(left[:5])}。')
    return problems


# --- theme documents -----------------------------------------------------------
# The designer describes the theme in words; colors, sizes and component styles are
# the frontend's, through its component library. Blog8: the task asked for colors,
# type scale and states, and the designer wrote a 19K-character token sheet.
THEME_LIMIT = 400
STYLE_VALUES = (
    ('色值', re.compile(r'#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z])', re.I)),
    ('颜色函数', re.compile(r'(?<![a-z])(?:rgba?|hsla?|hwb|oklch|oklab|lch|lab)\s*\(', re.I)),
    ('尺寸', re.compile(r'(?<![a-z0-9.])(?:\d+(?:\.\d+)?\s*)?px(?![a-z])|\d+(?:\.\d+)?\s*(?:rem|em|pt|vh|vw|ms)(?![a-z])', re.I)),
    ('样式属性', re.compile(r'(?<![a-z-])(?:font-(?:size|weight|family)|line-height|letter-spacing|border-radius|box-shadow|'
                         r'padding|margin)(?![a-z-])', re.I)),
)


def theme_problems(content: str) -> list[str]:
    """Why a theme document cannot be delivered: too long, or style values in it."""
    text, problems = authored(content), []
    if len(text) > THEME_LIMIT:
        problems.append(f'主题说明有 {len(text)} 字，上限是 {THEME_LIMIT} 字：只用文字写气质、色调倾向和素材清单。')
    found = [f'{label}（{"、".join(sorted({m.group(0).strip() for m in pattern.finditer(text)})[:5])}）'
             for label, pattern in STYLE_VALUES if pattern.search(text)]
    if found:
        problems.append('主题说明不写具体样式，发现：' + '；'.join(found) + '。颜色、字号、间距和组件样式由前端用组件库决定，'
                        '任务里要求写也不写；颜色用文字描述（例如“沉稳的蓝灰”），素材尺寸写成 24×24。')
    left = placeholders_left(content)
    if left:
        problems.append(f'文档里还有草稿占位符没有替换：{"、".join(left[:5])}。')
    return problems


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


# Roles that build the modules of a requirements document, and the manifest that shows
# a role's part of the project has been initialized.
MODULE_ROLES = ('backend', 'frontend')
INIT_MARKERS = {'backend': ('backend/pyproject.toml', 'backend/requirements.txt'), 'frontend': ('frontend/package.json',)}
FINISHED = {'cancelled', 'rejected', 'completed'}


def project_initialized(root) -> dict:
    from pathlib import Path
    return {role: any((Path(root) / marker).is_file() for marker in markers) for role, markers in INIT_MARKERS.items()}


def check_requirements(requirements):
    """A goal says where its requirements are, or why it needs none: {document: path} or {none: reason}."""
    if requirements is None:
        return None
    if not isinstance(requirements, dict) or len([k for k in ('document', 'none') if requirements.get(k)]) != 1:
        raise ValueError('requirements 只能是 {"document": "<需求文档路径>"} 或 {"none": "<这个目标不需要需求文档的原因>"} 之一')
    if requirements.get('none'):
        return {'none': str(requirements['none']).strip()[:500]}
    path = deliverable_path(str(requirements['document']).strip())
    if not path.lower().endswith('.md') or path.startswith(('/', '..')):
        raise ValueError('requirements.document 必须是项目内的 Markdown 路径，例如 docs/product/requirements.md')
    return {'document': path}


class PlanningMixin:
    async def requirements_state(self, c, project, goal) -> dict | None:
        """Whether the goal's requirements document can be built from, with its modules.
        None when the goal registered no document."""
        path = ((goal or {}).get('requirements') or {}).get('document')
        if not path:
            return None
        state = {'path': path, 'ready': False, 'modules': []}
        document = next((d for d in await self._list(c, 'document', project) if same_document(path, d)), None)
        if document is None:
            return {**state, 'problem': '还没有发布'}
        revision = await self._get(c, 'revision', project, f"{document['id']}:{document['version']}")
        state.update(document_id=document['id'], version=document['version'],
                     modules=[{'title': m['title'], 'section_id': m['id']} for m in requirement_modules(revision['content'])])
        if await still_draft(self, c, project, document):
            return {**state, 'problem': '还是平台生成的草稿'}
        if not await document_released(self, c, project, document):
            return {**state, 'problem': '还没有以 ready=true 发布'}
        problems = requirements_problems(revision['content'])
        if problems:
            return {**state, 'problem': '结构不符合按模块开发的格式：' + ' '.join(problems[:4])}
        return {**state, 'ready': True}

    async def plan_task(self, c, project, goal, identifier, recipient, module, spec, planned) -> tuple[dict, list[str]]:
        """Hold a goal task to the requirements-first flow. Returns the fields the
        platform adds to the task and the dependencies it adds.
        - with a registered requirements document, only product gets tasks until it is ready;
        - a planned backend or frontend task builds exactly one module of it, or only
          initializes (module="init"); one unfinished task per role and module;
        - a module task starts after its role's initialization;
        - a module's contract document belongs to that module."""
        state = await self.requirements_state(c, project, goal)
        if state is None:
            if module and module != INIT_MODULE:
                raise ValueError(f'这个目标没有登记需求文档，module 只能不填或填 "{INIT_MODULE}"。')
            return ({'module': module} if module else {}), []
        if recipient != 'product' and not state['ready']:
            raise ValueError(f"需求文档 {state['path']} {state['problem']}，现在不能给 {recipient} 建任务。下一步：只给产品建一个任务"
                             f"（documents=[\"{state['path']}\"]），然后用一句话结束本轮；产品交付后你会被唤醒，再建其余任务。")
        titles = [m['title'] for m in state['modules']]
        if not module:
            if planned and recipient in MODULE_ROLES:
                raise ValueError(f'给 {recipient} 建任务必须写 module：需求文档里的一个模块名 {titles}，'
                                 f'或 "{INIT_MODULE}"（初始化任务）。一个任务只做一个模块。')
            return {}, []
        others = [r for r in await self._list(c, 'request', project)
                  if r.get('goal_id') == goal['id'] and r['id'] != identifier and r['state'] not in FINISHED]
        same = next((r for r in others if r['recipient'] == recipient and r.get('module') == module), None)
        if same:
            raise ValueError(f"{recipient} 已经有{'初始化' if module == INIT_MODULE else f'模块“{module}”的'}任务 {same['id']}"
                             f"（{same['state']}）。在原任务上返工或重试，不新建。")
        if module == INIT_MODULE:
            return {'module': module}, []
        if module not in titles:
            raise ValueError(f'需求文档里没有模块“{module}”。module 填文档里的模块名之一：{titles}，或 "{INIT_MODULE}"。')
        added = []
        if recipient in MODULE_ROLES:
            init = next((r for r in others if r['recipient'] == recipient and r.get('module') == INIT_MODULE), None)
            root = (await self._project(c, project))['workspace_path']
            if init is None and not project_initialized(root)[recipient]:
                raise ValueError(f"项目里还没有 {recipient} 的工程（没有 {' 或 '.join(INIT_MARKERS[recipient])}）。"
                                 f'先给 {recipient} 建一个 module="{INIT_MODULE}" 的初始化任务，模块任务依赖它。')
            if init is not None and init['id'] not in spec['dependencies']:
                added.append(init['id'])  # a module is built on the initialized project
        for other in others:
            theirs = other.get('module')
            if not theirs or theirs == module:
                continue
            if theirs != INIT_MODULE:
                waited = [need['document'] for need in spec.get('needs') or () if need.get('document')
                          and any(deliverable_path(need['document']) == deliverable_path(d) for d in other.get('documents') or ())]
                if waited:
                    raise ValueError(f"{waited[0]} 是模块“{theirs}”的任务 {other['id']} 交付的文档。模块“{module}”的任务只能等待"
                                     '本模块的后端任务交付的契约文档，或初始化任务交付的通用约定。')
            shared = [d for d in spec.get('documents') or ()
                      if any(deliverable_path(d) == deliverable_path(o) for o in other.get('documents') or ())]
            if shared:
                raise ValueError(f"{shared[0]} 已经由任务 {other['id']}（module={theirs}）交付。契约文档一个模块一份："
                                 f'给模块“{module}”单独的文档路径；各模块通用的约定由后端初始化任务交付。')
        section = next(m['section_id'] for m in state['modules'] if m['title'] == module)
        return {'module': module, 'module_ref': {'document_id': state['document_id'], 'section_id': section, 'path': state['path']}}, added

    async def document_problems(self, c, project, path, content) -> list[str]:
        """Why a document cannot be marked ready or delivered in its role's format."""
        if await self.is_requirements_document(c, project, path):
            return requirements_problems(content)
        if document_kind(path) == 'design':
            return theme_problems(content)
        return []

    async def is_requirements_document(self, c, project, path) -> bool:
        """A document of the requirements kind, or the one a goal registered as its requirements."""
        if document_kind(path) == 'requirements':
            return True
        return any(deliverable_path((g.get('requirements') or {}).get('document') or '') == deliverable_path(path)
                   for g in await self._list(c, 'goal', project) if (g.get('requirements') or {}).get('document'))

    async def adopt_requirements(self, project, goal, conversation=''):
        """A requirements file the user already wrote becomes a managed document, so
        tasks can be planned from its modules and read one module at a time."""
        path = ((goal or {}).get('requirements') or {}).get('document')
        if not path:
            return
        async with self.reading() as c:
            root = (await self._project(c, project))['workspace_path']
            managed = any(same_document(path, d) for d in await self._list(c, 'document', project))
        if managed or not self._path(root, path).is_file():
            return
        await self.publish(project, 'leader', [{'path': path, 'base_version': 0,
                                                'summary': 'Imported the requirements document the user provided'}], conversation)

    async def goal_plan(self, project, goal) -> dict:
        """What the Leader plans from right after creating a goal: whether the project is
        initialized, and where the requirements stand."""
        async with self.reading() as c:
            root = (await self._project(c, project))['workspace_path']
            state = await self.requirements_state(c, project, goal)
        plan = {'project': {f'{role}_initialized': done for role, done in project_initialized(root).items()}}
        if state is not None:
            # With the document ID, so the Leader reads its outline without looking it up.
            plan['requirements'] = {'document': state['path'], 'ready': state['ready'],
                                    **({'document_id': state['document_id']} if state.get('document_id') else {}),
                                    **({'modules': [m['title'] for m in state['modules']]} if state['ready'] else {})}
            plan['next_action'] = ('需求文档已就绪：对照用户原话验收后，用一次 create_tasks 建完其余任务（每个模块一个后端任务和一个前端任务，module 填模块名）。'
                                   if state['ready'] else
                                   f"需求文档{state['problem']}：只给产品建一个任务（documents=[\"{state['path']}\"]），然后结束本轮。")
        return plan

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
                before = await self._get(c, 'revision', project, f"{item['document_id']}:{item['version'] - 1}", False)
                known = {h['id'] for h in before['outline']} if before else set()
                # Section IDs the author has not seen yet (new or renamed headings).
                entry['new_sections'] = [{'id': h['id'], 'title': h['title']} for h in revision['outline'][:OUTLINE_LIMIT]
                                         if h['id'] != 'root' and h['id'] not in known]
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
            themes = [path for path in request['documents'] if document_kind(path) == 'design']
            if themes:
                lines.append(f"{', '.join(themes)}: at most {THEME_LIMIT} characters of words, with no color values (#xxxxxx, "
                             'rgb()), sizes (px, rem) or style properties, even if the task asks for them; ready=true and '
                             'delivery are refused otherwise.')
            from app.collaboration.tools import documents_only
            if not (request.get('artifacts') or request.get('services') or request.get('module')) and documents_only(request['recipient']):
                lines.append('这个任务只交付文档：最后一份文档以 ready=true 发布成功后，任务自动交付并结束，不需要再调用 submit_task。'
                             '所以全部写完、自己通读过之后再标记 ready。')
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
                state = await self.requirements_state(c, project, goal)
            reference = request.get('module_ref')
            if reference:
                # The exact first call, so the member reads its module once instead of the
                # outline and then every heading.
                lines.append(f"本任务只做需求文档 {reference['path']} 里的模块“{request['module']}”。第一步读它（一次调用返回整个模块）："
                             f"collaboration_read(document_id=\"{reference['document_id']}\", section_id=\"{reference['section_id']}\", "
                             'subtree=true)。其他模块不是你的任务。')
            elif state and state.get('document_id') and request['recipient'] != 'product':
                titles = '、'.join(m['title'] for m in state['modules'])
                first = ('本任务是初始化（module="init"）：只交付框架或外壳，不做任何模块的业务功能。'
                         if request.get('module') == INIT_MODULE else '')
                lines.append(f"{first}需求文档 {state['path']}（document_id=\"{state['document_id']}\"）的模块：{titles}。"
                             '读某个模块用 collaboration_read(document_id, section_id=<模块的章节 ID>, subtree=true)，章节 ID 在大纲里。')
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
