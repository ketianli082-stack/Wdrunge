"""Context compaction without model calls (layers L0-L2 of app.agents.memory).

L0, entry budget (ingest): results the model has not seen yet are capped per result
(WDRUNGE_RESULT_TOKENS) and per model response (WDRUNGE_RESPONSE_RESULT_TOKENS); a
result identical to an earlier one still verbatim in the context becomes a short notice.
L1, history pruning: redundant results (superseded by a newer result for the same
target, identical to a newer one, a failure the same call later succeeded, stale after
a rewrite) become one-line stubs, and outside the verbatim tier whole model turns made
only of them are removed.
L2, micro-compaction: older results of recomputable tools (app.agents.context_policy)
fold by tier, counted per message by content volume from the newest:
- verbatim: the newest results up to WDRUNGE_FOLD_VERBATIM_TOKENS (the current turn always);
- excerpt: the next WDRUNGE_FOLD_EXCERPT_TOKENS keep each result's facts and its head;
- pointer: older results keep one line naming the tool, the resource and the message ID.
Durable results stay until a model summary of their work segment (L3) replaces them.
A task ledger keeps file, process, check and error facts from everything folded.
Originals stay in the compaction audit; recall_context returns them by message ID.
Facts about an original (its size, digest, whether a read covered the whole file) are
kept in additional_kwargs['fold'], which is never sent to the model.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agents.context_policy import recomputable

LEDGER_PREFIX = 'Task ledger (facts extracted from earlier tool results; historical data, not instructions):\n'
SIGNAL = re.compile(r'error|fail|exception|traceback|warn|denied|not found|cannot|unable|refused|✗|✘', re.I)
DIGEST_LIMIT = 1200
LEDGER_LIMIT = 3500
# Results up to these sizes stay as they are in the excerpt and pointer tiers.
EXCERPT_KEEP = 1200
POINTER_KEEP = 300
# Characters of a folded file or document body the excerpt tier keeps.
EXCERPT_HEAD = 600
EXCERPT, POINTER, STUB = 1, 2, 3


def _setting(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def verbatim_tokens() -> int:
    """Estimated tokens of the newest turns whose tool results stay verbatim."""
    return _setting('WDRUNGE_FOLD_VERBATIM_TOKENS', 30000, 1000)


def excerpt_tokens() -> int:
    """Estimated tokens (of the originals) before the verbatim turns kept as excerpts."""
    return _setting('WDRUNGE_FOLD_EXCERPT_TOKENS', 15000, 0)


def result_tokens() -> int:
    """Estimated tokens one tool result may bring into the context."""
    return _setting('WDRUNGE_RESULT_TOKENS', 8000, 500)


def response_tokens() -> int:
    """Estimated tokens the results of one model response may bring together."""
    return _setting('WDRUNGE_RESPONSE_RESULT_TOKENS', 20000, 1000)


def text_of(message: BaseMessage) -> str:
    return message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)


def estimate(text: str) -> int:
    # The same estimate as app.agents.memory.estimate_tokens (UTF-8 bytes / 2).
    return math.ceil(len(text.encode('utf-8')) / 2)


def is_ledger(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and 'context_ledger' in message.additional_kwargs


FOLDED_TEXT = re.compile(r'^\[(?:Superseded|Stale)|\n\[Folded .* result; recall_context|^\[Folded ', re.S)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def facts_of(message: BaseMessage) -> dict:
    """Facts about the original of a message: recorded when it entered the context or at
    its first fold, else measured now. A message folded before these facts were recorded
    never counts as complete."""
    meta = message.additional_kwargs.get('fold')
    if isinstance(meta, dict):
        return meta
    text = text_of(message)
    tokens = 12 + estimate(text)
    if isinstance(message, AIMessage) and message.tool_calls:
        tokens += estimate(json.dumps(message.tool_calls, ensure_ascii=False))
    legacy = isinstance(message, ToolMessage) and bool(FOLDED_TEXT.search(text))
    facts = {'level': STUB if legacy else 0, 'tokens': tokens, 'characters': len(text),
             'complete': not legacy and '[truncated; next_offset=' not in text}
    if isinstance(message, ToolMessage) and not legacy:
        facts['digest'] = text_digest(text)
    return facts


def with_facts(message: BaseMessage, level: int, **update) -> BaseMessage:
    meta = {**facts_of(message), 'level': level}
    return message.model_copy(update={**update, 'additional_kwargs': {**message.additional_kwargs, 'fold': meta}})


def tier_bounds(messages: list[BaseMessage], budget: int | None = None) -> tuple[int, int]:
    """(excerpt_start, verbatim_start): indexes of the first message of the excerpt and
    verbatim tiers, counted per message by the size of its original from the newest; a
    result counts at most one result budget, so one large result (from before the entry
    budget) cannot push all older history out of the tiers. The current turn, including
    results the model has not read yet, is always verbatim. verbatim_start is 0 when
    everything fits verbatim (nothing to fold); it may fall inside a turn, between its
    results. With a context budget, the tiers take at most a third and a sixth of it."""
    verbatim_limit, excerpt_limit = verbatim_tokens(), excerpt_tokens()
    if budget:
        verbatim_limit, excerpt_limit = min(verbatim_limit, budget // 3), min(excerpt_limit, budget // 6)
    turns = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    if not turns:
        return 0, 0
    cap = result_tokens() + 12

    def size(m):
        return min(facts_of(m)['tokens'], cap) if isinstance(m, (AIMessage, ToolMessage)) else 0

    used = sum(size(m) for m in messages[turns[-1]:])
    index = turns[-1]
    while index > 0 and used + size(messages[index - 1]) <= verbatim_limit:
        index -= 1
        used += size(messages[index])
    if not any(isinstance(m, (AIMessage, ToolMessage)) for m in messages[:index]):
        return 0, 0
    verbatim, used = index, 0
    while index > 0 and used + size(messages[index - 1]) <= excerpt_limit:
        index -= 1
        used += size(messages[index])
    return index, verbatim


def turn_start(messages: list[BaseMessage], index: int) -> int:
    """The first model turn starting at or after index (never between a call and its results)."""
    return next((i for i in range(index, len(messages)) if isinstance(messages[i], AIMessage)), len(messages))


def verbatim_boundary(messages: list[BaseMessage], budget: int | None = None) -> int:
    """Index of the first whole turn of the verbatim tier; 0 when nothing is folded."""
    boundary = tier_bounds(messages, budget)[1]
    return turn_start(messages, boundary) if boundary else 0


def calls_by_id(messages: list[BaseMessage]) -> dict[str, dict]:
    return {call['id']: call for m in messages if isinstance(m, AIMessage)
            for call in m.tool_calls if call.get('id')}


def tool_name(message: ToolMessage, calls: dict) -> str:
    call = calls.get(message.tool_call_id) or {}
    return call.get('name') or message.name or 'tool'


def parse(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def failed(message: ToolMessage) -> bool:
    if message.status == 'error':
        return True
    value = parse(text_of(message))
    return isinstance(value, dict) and (bool(value.get('error')) or value.get('passed') is False)


def clip(value, limit=200) -> str:
    value = str(value)
    return value if len(value) <= limit else value[:limit] + f'…(+{len(value) - limit})'


# --- supersession -------------------------------------------------------------

def resource_key(name: str, args: dict) -> tuple | None:
    """Results with the same key describe the same resource; the newer one wins."""
    if name == 'collaborate':
        name = 'collaboration_' + str(args.get('action', ''))
    if name == 'read_project_file':
        return ('file', args.get('path'), args.get('offset') or 0, args.get('limit') or 30000)
    if name == 'collaboration_read':
        return ('document', args.get('document_id'), args.get('section_id'), args.get('offset') or 0)
    if name == 'browse_directory':
        return ('directory', args.get('path', '.'), args.get('offset') or 0, args.get('limit') or 100)
    if name == 'search_code':
        return ('search', args.get('query'), args.get('path', '.'), args.get('offset') or 0)
    if name in {'list_project_files', 'detect_environment', 'collaboration_catalog', 'collaboration_snapshot',
                'collaboration_handoff', 'get_goal', 'collaboration_inspect_request', 'collaboration_dependencies',
                'collaboration_inspect_message'}:
        return (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
    if name == 'manage_process' and args.get('action') in {'list', 'status'}:
        return ('process', args.get('action'), args.get('process_id', ''))
    if name == 'manage_process' and args.get('action') == 'logs':
        return ('process-logs', args.get('process_id', ''), args.get('offset') or 0)
    if name == 'execute_command' and not args.get('background'):
        return ('command', args.get('cwd', '.'), args.get('command'), json.dumps(args.get('arguments', []), ensure_ascii=False))
    if name == 'recall_context':
        return ('recall', args.get('message_id'), args.get('offset') or 0)
    return None


def full_read(args: dict, message: BaseMessage) -> bool:
    """Whether a read covered the whole file, judged on the original result: a folded
    partial read no longer carries its truncation marker."""
    return not args.get('offset') and bool(facts_of(message).get('complete'))


def call_key(name: str, args: dict) -> str:
    """Calls with the same key are the same call: the tool and its exact arguments."""
    return json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)


def unchanged_of(message: BaseMessage) -> str | None:
    """The earlier message an unchanged-result notice stands for."""
    return facts_of(message).get('unchanged_of') if isinstance(message, ToolMessage) else None


def supersession(messages: list[BaseMessage], until: int) -> dict[int, str]:
    """Map an older ToolMessage index to the stub replacing it: redundant results whose
    information a newer result carries. Only results before `until` (the current turn)
    are replaced; newer results may supersede them.
    - the same call (tool and arguments) returned the identical result later;
    - the same call failed and later succeeded;
    - a newer result for the same target (resource_key), or a file rewritten after a read."""
    calls = calls_by_id(messages)
    latest: dict[tuple, int] = {}
    same_call: dict[str, int] = {}
    reads: dict[str, list[int]] = {}
    writes: dict[str, int] = {}
    stubs: dict[int, str] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage) or unchanged_of(message):
            # A notice stands for an earlier result, which therefore stays.
            continue
        call = calls.get(message.tool_call_id) or {}
        name, args = tool_name(message, calls), call.get('args') or {}
        ok = not failed(message)
        key = call_key(name, args)
        earlier = same_call.get(key)
        if earlier is not None and earlier < until:
            digest, previous = facts_of(message).get('digest'), facts_of(messages[earlier]).get('digest')
            if digest and digest == previous:
                stubs.setdefault(earlier, f'[Identical to newer {name} result {message.id} of the same call.]')
            elif ok and failed(messages[earlier]):
                stubs.setdefault(earlier, f'[Failed; the same {name} call succeeded later in message {message.id}.]')
        same_call[key] = index
        if name == 'write_project_file' and ok:
            for older in reads.pop(args.get('path'), []):
                if older < until:
                    stubs[older] = (f"[Stale: {args.get('path')} was rewritten by message {message.id} after this read; "
                                    'read it again for current content.]')
            older = writes.get(args.get('path'))
            if older is not None and older < until and '\nStructure: ' in text_of(messages[older]):
                # The receipt line stays; its outline is the newer write's.
                stubs[older] = (text_of(messages[older]).split('\n', 1)[0]
                                + f' [Structure superseded by the newer write in message {message.id}.]')
            writes[args.get('path')] = index
            continue
        key = resource_key(name, args)
        if key is None:
            continue
        if name == 'read_project_file' and ok and full_read(args, message):
            # A complete read replaces every earlier range of the same file.
            for older in reads.pop(args.get('path'), []):
                if older < until:
                    stubs.setdefault(older, f'[Superseded by a newer full read of {args.get("path")} in message {message.id}.]')
        previous = latest.get(key)
        if previous is not None and previous < until and (ok or failed(messages[previous])):
            stubs.setdefault(previous, f'[Superseded by newer {name} result {message.id} for the same target.]')
        if ok or previous is None or failed(messages[previous]):
            latest[key] = index
        if name == 'read_project_file' and ok:
            reads.setdefault(args.get('path'), []).append(index)
    return stubs


# --- per-tool digests ---------------------------------------------------------

STRUCTURE_LIMIT = 1500
STRUCTURE = [
    (re.compile(r'\.(py|pyi)$'), re.compile(r'^(?:\s{0,4})(?:async\s+def|def|class)\s+\w+.*|^[A-Z][A-Z0-9_]{2,}\s*[:=].*')),
    (re.compile(r'\.(js|jsx|ts|tsx|mjs|cjs|vue|svelte)$'),
     re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|class|interface|type|enum)\s+\w+.*'
                r'|^(?:export\s+)?(?:const|let)\s+\w+\s*=.*|^\s*<(?:template|script|style)\b.*|^\s*(?:app|router)\.\w+\(.*')),
    (re.compile(r'\.(md|markdown)$'), re.compile(r'^#{1,4}\s+\S.*')),
    (re.compile(r'\.(go|rs|java|kt|rb|php|cs|swift)$'),
     re.compile(r'^\s*(?:pub\s+)?(?:func|fn|def|class|struct|interface|enum|impl|type|public|private|protected)\b.*')),
    (re.compile(r'\.(css|scss|less)$'), re.compile(r'^[^\s{}][^{}]*\{\s*$|^:root.*')),
]


def structure(path, text: str, first_line=1) -> str:
    """Line-numbered declarations (or headings) of a source file, so an author whose
    written or read file was folded still knows what it contains and where."""
    pattern = next((p for match, p in STRUCTURE if match.search(str(path or ''))), None)
    if pattern is None:
        return ''
    found = [f'L{first_line + number} {clip(line.strip(), 100)}' for number, line in enumerate(text.splitlines())
             if pattern.match(line)]
    rendered, used = [], 0
    for item in found:
        if used + len(item) + 3 > STRUCTURE_LIMIT:
            rendered.append(f'…(+{len(found) - len(rendered)} more)')
            break
        rendered.append(item)
        used += len(item) + 3
    return ' | '.join(rendered)


def output_digest(output: str) -> list[str]:
    lines = [line.strip() for line in str(output).splitlines() if line.strip()]
    signals = [clip(line, 200) for line in dict.fromkeys(lines[:-3]) if SIGNAL.search(line)][:5]
    return signals + [clip(line, 200) for line in lines[-3:]]


def compact(value, depth=0):
    """Generic JSON shape: scalars, short strings, first list items, nested to depth 3."""
    if isinstance(value, str):
        return clip(value, 200)
    if isinstance(value, list):
        items = [compact(item, depth + 1) for item in value[:5]] if depth < 3 else []
        return items + ([f'…{len(value) - len(items)} more'] if len(value) > len(items) else [])
    if isinstance(value, dict):
        if depth >= 3:
            return f'{{{len(value)} keys}}'
        return {key: compact(item, depth + 1) for key, item in list(value.items())[:20]}
    return value


def pick(data: dict, *keys) -> dict:
    return {key: data[key] for key in keys if key in data and data[key] not in (None, '', [], {})}


def failure_digest(data: dict) -> dict:
    details = data.get('error_details') or {}
    return pick({'error': clip(data.get('error', ''), 300), 'code': details.get('code'),
                 'category': details.get('category')}, 'error', 'code', 'category')


def digest(name: str, args: dict, text: str, keep_outline=False, head=EXCERPT_HEAD) -> str | None:
    """A compact rendering that keeps decision-relevant facts, or None to fall back.
    keep_outline keeps every section ID of the newest outline of a document; head is
    how much of a file or document body is kept."""
    data = parse(text)
    if name == 'read_project_file' and data is None:
        header = re.match(r'\[sha256=(\w+); characters=(\d+)\]\n', text)
        body = text[header.end():] if header else text
        facts = {'path': args.get('path'), 'offset': args.get('offset') or 0, 'characters': len(body),
                 'lines': body.count('\n') + 1}
        if header:
            facts['sha256'] = header.group(1)
        more = re.search(r'\[truncated; next_offset=(\d+)\]', body)
        if more:
            facts['next_offset'] = int(more.group(1))
        shape = structure(args.get('path'), body, 1) if not args.get('offset') else ''
        if shape:
            facts['structure'] = shape
        return json.dumps({**facts, **({'head': clip(body[:head], head)} if head else {})}, ensure_ascii=False)
    if not isinstance(data, dict):
        return None
    if name in {'execute_command', 'manage_process'}:
        facts = pick(data, 'id', 'name', 'state', 'exit_code', 'timed_out', 'network', 'ready', 'ports', 'next_offset')
        if data.get('argv'):
            facts['argv'] = clip(' '.join(map(str, data['argv'])), 200)
        if 'processes' in data:
            facts['processes'] = [pick(p, 'id', 'name', 'state', 'exit_code', 'ports') for p in data['processes'][:10]]
        if data.get('output'):
            facts['output'] = output_digest(data['output'])
        return json.dumps({**facts, **failure_digest(data)}, ensure_ascii=False)
    if name == 'browser_verify':
        facts = pick(data, 'url', 'title', 'status', 'passed', 'expected_errors', 'counts', 'screenshot')
        facts['actions'] = [pick(a, 'action', 'selector', 'passed') for a in data.get('actions', [])[:10]]
        for key in ('page_errors', 'http_errors'):
            if data.get(key):
                facts[key] = [clip(item, 200) for item in data[key][:3]]
        errors = [clip(c.get('text', ''), 200) for c in data.get('console', []) if c.get('level') == 'error'][:3]
        if errors:
            facts['console_errors'] = errors
        if data.get('text'):
            facts['text'] = clip(data['text'], 300)
        return json.dumps({**facts, **failure_digest(data)}, ensure_ascii=False)
    if name == 'collaboration_publish' and isinstance(data.get('revisions'), list):
        revisions = []
        for item in data['revisions']:
            if not isinstance(item, dict):
                continue
            entry = pick(item, 'document_id', 'version', 'path', 'characters', 'budget', 'ready')
            if keep_outline and item.get('outline'):
                entry['outline'] = [pick(s, 'id', 'title') for s in item['outline'] if isinstance(s, dict)]
            revisions.append(entry)
        return json.dumps({'revisions': revisions, **pick(data, 'budget_notice', 'ready_hint'), **failure_digest(data)}, ensure_ascii=False)
    if name == 'collaboration_read':
        facts = pick(data, 'document_id', 'version', 'next_offset')
        if isinstance(data.get('section'), dict):
            facts['section'] = pick(data['section'], 'id', 'title')
        if data.get('outline'):
            facts['outline'] = [pick(s, 'id', 'title') if isinstance(s, dict) else clip(s, 80)
                                for s in data['outline'][:None if keep_outline else 20]]
        if data.get('content') and head:
            facts['content'] = clip(data['content'], head)
        return json.dumps({**facts, **failure_digest(data)}, ensure_ascii=False)
    if name == 'search_code':
        hits = [f"{m.get('path')}:{m.get('line')}: {clip(m.get('text', ''), 100)}" for m in data.get('matches', [])[:15]]
        return json.dumps({'matches': hits, 'total_shown': len(data.get('matches', [])),
                           **pick(data, 'next_offset', 'incomplete')}, ensure_ascii=False)
    if name == 'browse_directory':
        names = [f"{e.get('name')}{'/' if e.get('type') == 'directory' else ''}" for e in data.get('entries', [])[:60]]
        return json.dumps({'entries': names, **pick(data, 'next_offset')}, ensure_ascii=False)
    rendered = json.dumps(compact(data), ensure_ascii=False)
    return rendered if len(rendered) <= DIGEST_LIMIT else None


def fold_marker(name: str, message_id: str) -> str:
    return f'\n[Folded {name} result; recall_context(message_id="{message_id}") returns the original.]'


TARGET_ARGS = ('path', 'document_id', 'section_id', 'offset', 'limit', 'query', 'process_id', 'action', 'url',
               'message_id', 'identifier', 'name')


def pointer_facts(name: str, args: dict, text: str) -> dict:
    """What a one-line pointer keeps: the call's target, the result's size and status."""
    facts = {key: clip(args[key], 80) for key in TARGET_ARGS if args.get(key) not in (None, '', [], {})}
    if name == 'execute_command':
        facts['argv'] = clip(' '.join(map(str, [args.get('command', ''), *(args.get('arguments') or [])])), 120)
    facts['characters'] = len(text)
    data = parse(text)
    if isinstance(data, dict):
        facts.update({key: data[key] for key in ('version', 'exit_code', 'state', 'passed', 'next_offset', 'id')
                      if isinstance(data.get(key), (str, int, float, bool)) and data.get(key) != ''})
        if isinstance(data.get('revisions'), list):
            facts['revisions'] = [pick(r, 'document_id', 'version', 'ready') for r in data['revisions'][:5] if isinstance(r, dict)]
        if data.get('error'):
            facts.update(failure_digest(data))
            facts['error'] = clip(data['error'], 160)
    else:
        header = re.match(r'\[sha256=(\w+); characters=(\d+)\]', text)
        if header:
            facts['sha256'] = header.group(1)[:16]
        more = re.search(r'\[truncated; next_offset=(\d+)\]', text)
        if more:
            facts['next_offset'] = int(more.group(1))
    return facts


def pointer_message(message: ToolMessage, name: str, facts: dict) -> ToolMessage:
    pointer = json.dumps(facts.get('pointer') or {'characters': facts.get('characters')}, ensure_ascii=False)
    content = f'[Folded {name} result {pointer}; recall_context(message_id="{message.id}") returns it.]'
    meta = {key: value for key, value in facts.items() if key != 'outline'}
    return message.model_copy(update={'content': content, 'artifact': None,
                                      'additional_kwargs': {**message.additional_kwargs, 'fold': {**meta, 'level': POINTER}}})


def fold_result(message: ToolMessage, name: str, args: dict, level=EXCERPT, keep_outline=False) -> ToolMessage:
    """The message at the given tier. Excerpt: facts and the head of a body. Pointer:
    one line. The newest outline of a document keeps its section IDs at any tier until
    a newer outline replaces it. A message already folded that far is unchanged."""
    facts = facts_of(message)
    if facts.get('outline'):
        return message if keep_outline else pointer_message(message, name, facts)
    if facts['level'] >= level:
        return message
    text = text_of(message)
    if facts['level'] == 0 and len(text) <= (EXCERPT_KEEP if level == EXCERPT else POINTER_KEEP):
        return message
    if facts['level'] == 0:
        # Recorded once, from the original, so a later pointer does not need it.
        facts = {**facts, 'pointer': pointer_facts(name, args, text)}
        if keep_outline and (rendered := digest(name, args, text, keep_outline=True, head=EXCERPT_HEAD if level == EXCERPT else 0)):
            return message.model_copy(update={'content': rendered + fold_marker(name, message.id), 'artifact': None,
                                              'additional_kwargs': {**message.additional_kwargs,
                                                                    'fold': {**facts, 'level': level, 'outline': True}}})
    message = message.model_copy(update={'additional_kwargs': {**message.additional_kwargs, 'fold': facts}})
    if level == POINTER:
        return pointer_message(message, name, facts)
    rendered = digest(name, args, text)
    if rendered is None or len(rendered) + 200 >= len(text):
        rendered = text[:EXCERPT_KEEP]
    note = (f'\n[Excerpt: {len(rendered):,} of {facts["characters"]:,} characters kept; recall_context(message_id='
            f'"{message.id}") returns the original, or read the source again for its current text.]')
    return with_facts(message, EXCERPT, content=rendered + note, artifact=None)


# Folded call arguments stay in history in the place of the real ones, so a model can
# copy one into a new call as if it were the value; tools refuse them (folded_argument).
FOLDED_VALUE = re.compile(r'\A<\d+ characters written to .*; the file is on disk[^>]*>\Z|'
                          r' …\[\d+ characters folded; not the value\]\Z', re.S)


def folded_argument(value, path='') -> str | None:
    """Where a tool call passes a folded placeholder instead of a real value."""
    if isinstance(value, str):
        return path or 'arguments' if FOLDED_VALUE.search(value) else None
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        if found := folded_argument(item, f'{path}.{key}' if path else str(key)):
            return found
    return None


def shrink(value, limit=400, keep=200):
    """Strings longer than limit keep their first `keep` characters and a placeholder."""
    if isinstance(value, str) and len(value) > limit:
        return value[:keep] + f' …[{len(value) - keep} characters folded; not the value]'
    if isinstance(value, list):
        return [shrink(item, limit, keep) for item in value]
    if isinstance(value, dict):
        return {key: shrink(item, limit, keep) for key, item in value.items()}
    return value


CALL_LIMIT = 1500


def shrink_args(args: dict, level: int):
    """Excerpt: long strings keep a short head, unless the call stays large (a publish
    with many edits); pointer: long strings keep no text, only the arguments' shape."""
    if level == EXCERPT:
        shrunk = shrink(args, 400, 120)
        if len(json.dumps(shrunk, ensure_ascii=False)) <= CALL_LIMIT:
            return shrunk
    return shrink(args, 160, 0)


def fold_call(message: AIMessage, level=EXCERPT) -> AIMessage:
    """Large arguments (written file bodies, document edits, long commands) are on disk,
    in the published revision or in the audit."""
    if facts_of(message)['level'] >= level:
        return message
    calls = []
    for call in message.tool_calls:
        args = call.get('args') or {}
        if (call['name'] == 'write_project_file' and isinstance(args.get('content'), str) and len(args['content']) > 400
                and not FOLDED_VALUE.search(args['content'])):  # not a placeholder folded earlier
            body = args['content']
            shape = structure(args.get('path'), body)
            placeholder = (f'<{len(body)} characters written to {args.get("path")}; '
                           f'sha256 {hashlib.sha256(body.encode()).hexdigest()[:16]}…; '
                           + (f'structure: {shape}; ' if shape else '') + 'the file is on disk, not this text>')
            # The placeholder is already compact; shrinking would cut the structure.
            calls.append({**call, 'args': {**shrink_args({k: v for k, v in args.items() if k != 'content'}, level),
                                           'content': placeholder}})
            continue
        calls.append({**call, 'args': shrink_args(args, level)})
    if calls == message.tool_calls:
        return message
    return with_facts(message, level, tool_calls=calls)


# --- entry budget (L0) --------------------------------------------------------

UNCHANGED_NOTE = ('Identical to your earlier {name} result for the same arguments, which is still shown above in '
                  'this context; it is not repeated. Repeat the same call{force} to receive it again.')


def pageable(name: str) -> bool:
    """A tool that takes an offset: a capped result continues with a later call."""
    from app.agents.tools.validation import _schemas
    recomputable(name)  # makes sure built-in tools are registered
    schema = _schemas.get(name)
    return bool(schema) and 'offset' in schema.model_fields


def head(text: str, tokens: int) -> str:
    return text.encode()[:max(0, tokens) * 2].decode('utf-8', 'ignore')


def tail(text: str, tokens: int) -> str:
    data = text.encode()
    return data[len(data) - max(0, tokens) * 2:].decode('utf-8', 'ignore') if tokens > 0 else ''


def cap_text(text: str, tokens: int, message_id: str, keep_tail: bool) -> str:
    """At most `tokens` (estimated): the head (a paged read continues from it), or the
    head and the tail (a command's errors and summary come last)."""
    if estimate(text) <= tokens:
        return text
    room = tokens
    while True:
        if keep_tail:
            first, last = head(text, int(room * .6)), tail(text, int(room * .4))
            omitted = len(text) - len(first) - len(last)
            capped = (f'{first}\n…[{omitted:,} characters omitted here by the context budget; recall_context(message_id='
                      f'"{message_id}", offset={len(first)}) returns them.]\n{last}')
        else:
            shown = head(text, room)
            capped = (f'{shown}\n[Result capped by the context budget: {len(shown):,} of {len(text):,} characters shown; '
                      f'recall_context(message_id="{message_id}", offset={len(shown)}) returns the rest, or continue the '
                      'read with a later offset.]')
        over = estimate(capped) - tokens
        if over <= 0 or room <= 0:
            return capped
        room -= over


def cap_json(data, tokens: int, message_id: str, keep_tail: bool):
    """A JSON result keeps its structure: its longest strings are cut until it fits."""
    def strings(value, path=()):
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from strings(item, (*path, key))
        elif isinstance(value, list):
            for key, item in enumerate(value):
                yield from strings(item, (*path, key))

    def assign(value, path, replacement):
        for key in path[:-1]:
            value = value[key]
        value[path[-1]] = replacement

    data = json.loads(json.dumps(data))
    for _ in range(50):
        size = estimate(json.dumps(data, ensure_ascii=False))
        if size <= tokens:
            break
        path, longest = max(strings(data), key=lambda item: len(item[1]), default=((), ''))
        if not path or estimate(longest) < 200:
            return None
        keep = max(100, estimate(longest) - (size - tokens) - 100)
        if keep_tail:
            first, last = head(longest, int(keep * .6)), tail(longest, int(keep * .4))
            cut = f'{first}\n…[{len(longest) - len(first) - len(last):,} characters omitted by the context budget]\n{last}'
        else:
            first = head(longest, keep)
            cut = f'{first}\n…[{len(longest) - len(first):,} more characters omitted by the context budget]'
        assign(data, path, cut)
    else:
        return None
    if isinstance(data, dict):
        data['context_budget'] = (f'Capped to fit the context; recall_context(message_id="{message_id}") returns the '
                                  'full original' + (', or continue with a later offset.' if not keep_tail else '.'))
    return data


def cap_result(message: ToolMessage, name: str, tokens: int, original: str) -> ToolMessage:
    keep_tail = not pageable(name)
    data = parse(original)
    capped = None
    if isinstance(data, (dict, list)):
        shrunk = cap_json(data, tokens, message.id, keep_tail)
        if shrunk is not None:
            capped = json.dumps(shrunk, ensure_ascii=False)
    if capped is None:
        capped = cap_text(original, tokens, message.id, keep_tail)
    meta = {'level': 0, 'tokens': 12 + estimate(capped), 'characters': len(capped), 'complete': False,
            'digest': text_digest(original), 'capped': len(original)}
    return message.model_copy(update={'content': capped, 'artifact': None,
                                      'additional_kwargs': {**message.additional_kwargs, 'fold': meta}})


def unchanged_notice(message: ToolMessage, name: str, args: dict, earlier: ToolMessage, digest: str) -> ToolMessage:
    note = UNCHANGED_NOTE.format(name=name, force=' with force=true' if 'force' in args or name == 'collaboration_read' else '')
    target = {key: clip(args[key], 80) for key in TARGET_ARGS if args.get(key) not in (None, '', [], {})}
    content = json.dumps({'unchanged': True, **({'target': target} if target else {}), 'note': note}, ensure_ascii=False)
    meta = {'level': 0, 'tokens': 12 + estimate(content), 'characters': len(content), 'complete': True,
            'digest': digest, 'unchanged_of': earlier.id}
    return message.model_copy(update={'content': content, 'artifact': None,
                                      'additional_kwargs': {**message.additional_kwargs, 'fold': meta}})


def ingest(body: list[BaseMessage], budget: int | None = None) -> tuple[list[BaseMessage], list[BaseMessage], list[BaseMessage]]:
    """L0: results of the newest turn the model has not seen yet enter the context
    within the entry budget. Returns (body, originals changed, their replacements).
    - a recomputable result identical to the newest earlier result of the same call
      (tool and arguments), still verbatim in this context, becomes a short notice;
      repeating the call after a notice (or force=true) returns the result itself;
    - a result over WDRUNGE_RESULT_TOKENS is capped (a paged read keeps its head, other
      output its head and tail; JSON keeps its structure);
    - the results of one response together stay within WDRUNGE_RESPONSE_RESULT_TOKENS,
      the largest cut first.
    With a context budget, a result takes at most a quarter of it and a response a third.
    The original goes to the compaction audit; recall_context returns it by message ID."""
    single, together = result_tokens(), response_tokens()
    if budget:
        single, together = min(single, max(500, budget // 4)), min(together, max(1000, budget // 3))
    turns = [i for i, m in enumerate(body) if isinstance(m, AIMessage)]
    if not turns:
        return body, [], []
    start = turns[-1]
    fresh = [i for i in range(start + 1, len(body))
             if isinstance(body[i], ToolMessage) and 'fold' not in body[i].additional_kwargs]
    if not fresh:
        return body, [], []
    calls = calls_by_id(body)
    newest: dict[str, int] = {}
    for index in range(start):
        if isinstance(body[index], ToolMessage):
            call = calls.get(body[index].tool_call_id) or {}
            newest[call_key(tool_name(body[index], calls), call.get('args') or {})] = index
    result = list(body)
    sizes: dict[int, int] = {}
    for index in fresh:
        message = body[index]
        call = calls.get(message.tool_call_id) or {}
        name, args, text = tool_name(message, calls), call.get('args') or {}, text_of(message)
        digest = text_digest(text)
        earlier = newest.get(call_key(name, args))
        if earlier is not None and not args.get('force') and not failed(message) and recomputable(name, args):
            previous = body[earlier]
            facts = facts_of(previous)
            if (facts['level'] == 0 and not facts.get('unchanged_of') and facts.get('digest') == digest
                    and not failed(previous)):
                result[index] = unchanged_notice(message, name, args, previous, digest)
                continue
        if estimate(text) > single:
            result[index] = cap_result(message, name, single, text)
        else:
            result[index] = with_facts(message, 0)
        sizes[index] = facts_of(result[index])['tokens']
    # The results of one response share a budget: cut the largest to a common size.
    limit = together
    if sum(sizes.values()) > limit:
        ordered = sorted(sizes.values())
        level, remaining = limit, limit
        for position, size in enumerate(ordered):
            share = remaining // (len(ordered) - position)
            if size > share:
                level = share
                break
            remaining -= size
        for index, size in sizes.items():
            if size > level:
                message = body[index]
                result[index] = cap_result(message, tool_name(message, calls), max(500, level - 12), text_of(message))
    changed = [i for i in fresh if result[i] is not body[i] and text_of(result[i]) != text_of(body[i])]
    return result, [body[i] for i in changed], [result[i] for i in changed]


# --- task ledger --------------------------------------------------------------

def empty_ledger() -> dict:
    return {'files': {}, 'documents': {}, 'processes': {}, 'checks': {}, 'browser': {}, 'errors': {}, 'seen': []}


def ledger_of(messages: list[BaseMessage]) -> dict:
    for message in messages:
        if is_ledger(message):
            data = message.additional_kwargs['context_ledger']
            return {**empty_ledger(), **json.loads(json.dumps(data))}
    return empty_ledger()


def record_facts(ledger: dict, messages: list[BaseMessage], calls: dict | None = None) -> dict:
    """Merge facts from tool results into the ledger. Idempotent per message ID."""
    calls = calls or calls_by_id(messages)
    order = list(ledger['seen'])
    seen = set(order)
    for message in messages:
        if not isinstance(message, ToolMessage) or message.id in seen or text_of(message).startswith(('[Superseded', '[Stale')):
            continue
        seen.add(message.id)
        order.append(message.id)
        call = calls.get(message.tool_call_id) or {}
        name, args, text = tool_name(message, calls), call.get('args') or {}, text_of(message)
        raw = parse(text)
        data = raw if isinstance(raw, dict) else {}
        problem = failed(message)
        key = json.dumps(resource_key(name, args) or [name, args], sort_keys=True, ensure_ascii=False, default=str)[:300]
        if problem and (data.get('error_details') or {}).get('code') == 'reads_paused':
            continue  # a refused read during a read pause is no open failure
        if problem:
            ledger['errors'][key] = {'tool': name, 'error': clip(data.get('error') or text, 200),
                                     'code': (data.get('error_details') or {}).get('code'), 'message': message.id}
            continue
        ledger['errors'].pop(key, None)
        if name == 'write_project_file':
            written = re.search(r'Wrote (.+?) \((\d+) characters; sha256=(\w+)\)', text)
            path = args.get('path') or (written.group(1) if written else None)
            if path:
                outline = re.search(r'^Structure: (.+)$', text, re.M)
                # Re-inserted, so the most recently written files come last (and stay longest).
                ledger['files'].pop(path, None)
                ledger['files'][path] = {'op': 'wrote', 'message': message.id,
                                         **({'characters': int(written.group(2)), 'sha256': written.group(3)[:16]} if written else {}),
                                         **({'outline': outline.group(1)} if outline else {})}
                # A later failed write of this path no longer matters.
                ledger['errors'] = {k: v for k, v in ledger['errors'].items() if not (v['tool'] == 'write_project_file' and path in k)}
        elif name == 'read_project_file' and args.get('path'):
            entry = ledger['files'].get(args['path'], {})
            # Declarations of a whole-file read (judged on the original, not a folded copy).
            outline = structure(args['path'], re.sub(r'\A\[sha256=\w+; characters=\d+\]\n', '', text)) \
                if full_read(args, message) and not FOLDED_TEXT.search(text) and not unchanged_of(message) else ''
            if entry.get('op') != 'wrote':
                header = re.match(r'\[sha256=(\w+); characters=(\d+)\]', text)
                ledger['files'][args['path']] = {'op': 'read', 'message': message.id,
                                                 **({'sha256': header.group(1)[:16]} if header else {}),
                                                 **({'outline': outline or entry['outline']} if outline or entry.get('outline') else {})}
            elif outline:
                entry['outline'] = outline
        elif name in {'collaboration_publish', 'collaboration_read'} or (name == 'collaborate' and args.get('action') in {'publish', 'read'}):
            items = raw if isinstance(raw, list) else (data.get('revisions') or data.get('results') or data.get('documents') or [data])
            for item in items:
                if isinstance(item, dict) and item.get('document_id'):
                    ledger['documents'][item['document_id']] = {
                        'version': item.get('version'), 'path': item.get('path'),
                        'op': 'published' if 'publish' in name or args.get('action') == 'publish' else 'read'}
        elif name == 'execute_command' and data.get('id'):
            command = clip(' '.join(map(str, data.get('argv') or [args.get('command', '')])), 160)
            if args.get('background'):
                ledger['processes'][data['id']] = {'name': data.get('name'), 'argv': command, 'state': data.get('state'),
                                                   **pick(data, 'ready', 'exit_code'),
                                                   **({'ports': [args['ready_port']]} if data.get('ready') and args.get('ready_port') else {})}
            else:
                ledger['checks'][command] = {'exit_code': data.get('exit_code'), 'message': message.id}
        elif name == 'manage_process':
            for item in data.get('processes', [data]):
                if isinstance(item, dict) and item.get('id') in ledger['processes']:
                    ledger['processes'][item['id']].update(pick(item, 'state', 'exit_code', 'ports'))
        elif name == 'browser_verify' and data.get('url'):
            ledger['browser'][data['url']] = {'passed': data.get('passed'), 'message': message.id,
                                              **pick(data, 'expected_errors', 'screenshot')}
    ledger['seen'] = order[-400:]  # ordered, so the oldest IDs are the ones dropped
    for section, limit in (('checks', 15), ('browser', 8), ('errors', 10), ('files', 40), ('processes', 12), ('documents', 15)):
        ledger[section] = dict(list(ledger[section].items())[-limit:])
    return ledger


def render_ledger(ledger: dict) -> str:
    lines = []
    if ledger['files']:
        lines.append('Files: ' + '; '.join(f"{path} {entry['op']}" + (f" sha256 {entry['sha256']}…" if entry.get('sha256') else '')
                                           for path, entry in ledger['files'].items()))
    if ledger['documents']:
        lines.append('Shared documents: ' + '; '.join(f"{entry.get('path') or doc} v{entry.get('version')} {entry['op']}"
                                                      for doc, entry in ledger['documents'].items()))
    if ledger['processes']:
        # Running services matter; of the stopped ones only the latest few do.
        processes = list(ledger['processes'].items())
        stopped = [item for item in processes if item[1].get('state') != 'running']
        shown = [item for item in processes if item[1].get('state') == 'running' or item in stopped[-3:]]
        lines.append('Processes: ' + '; '.join(
            f"{entry.get('name')} ({pid}) {entry.get('state')}" + (f" ports {entry['ports']}" if entry.get('ports') else '')
            + (f" exit {entry['exit_code']}" if entry.get('exit_code') is not None else '') + f": {entry.get('argv')}"
            for pid, entry in shown) + (f' (+{len(stopped) - 3} older stopped)' if len(stopped) > 3 else ''))
    if ledger['checks']:
        lines.append('Commands run (last result): ' + '; '.join(f"`{command}` → exit {entry['exit_code']}"
                                                              for command, entry in ledger['checks'].items()))
    if ledger['browser']:
        lines.append('Browser checks: ' + '; '.join(
            f"{url} passed={entry['passed']}" + (' (errors expected)' if entry.get('expected_errors') else '')
            + (f" screenshot {entry['screenshot']}" if entry.get('screenshot') else '') for url, entry in ledger['browser'].items()))
    if ledger['errors']:
        lines.append('Unresolved tool failures: ' + '; '.join(
            f"{entry['tool']} [{entry.get('code') or 'error'}] {entry['error']} (message {entry['message']})"
            for entry in ledger['errors'].values()))
    text = '\n'.join(lines)
    text = text if len(text) <= LEDGER_LIMIT else text[:LEDGER_LIMIT] + ' …[ledger truncated]'
    index = code_index(ledger['files'])
    return f'{text}\n{index}' if text and index else text or index


def code_index_chars() -> int:
    """Characters of the code index: declarations of the files this agent wrote or read."""
    return _setting('WDRUNGE_CODE_INDEX_CHARS', 6000, 0)


def code_index(files: dict) -> str:
    """Declarations (line-numbered names, signatures, selectors) of files the agent
    wrote or read whole, newest first within WDRUNGE_CODE_INDEX_CHARS. The text of
    its own code folds out of the context; without this the agent re-read it to
    recall a name or a class."""
    budget, shown, omitted = code_index_chars(), [], 0
    for path, entry in reversed(list(files.items())):
        if not entry.get('outline'):
            continue
        line = f"- {path} ({entry['op']}): {entry['outline']}"
        if len(line) > budget:
            omitted += 1
            continue
        shown.append(line)
        budget -= len(line) + 1
    if not shown:
        return ''
    return ('Code index (declarations as of your last write or whole read; read only the lines you need):\n'
            + '\n'.join(shown) + (f'\n(+{omitted} older files not shown)' if omitted else ''))


def ledger_message(ledger: dict) -> HumanMessage | None:
    text = render_ledger(ledger)
    if not text:
        return None
    return HumanMessage(content=LEDGER_PREFIX + text, additional_kwargs={'context_ledger': ledger})


def latest_outlines(body: list[BaseMessage], calls: dict) -> set[int]:
    """Indexes of the newest result carrying each document's outline (a publish receipt
    or an outline read), whose section IDs survive folding: authors edit by those IDs,
    and losing them made agents reload the outline before every edit."""
    latest: dict[str, int] = {}
    for index, message in enumerate(body):
        if not isinstance(message, ToolMessage) or failed(message):
            continue
        name = tool_name(message, calls)
        data = parse(text_of(message))
        if not isinstance(data, dict):
            continue
        if name == 'collaboration_publish':
            for item in data.get('revisions') or ():
                if isinstance(item, dict) and item.get('outline') and item.get('document_id'):
                    latest[item['document_id']] = index
        elif name == 'collaboration_read' and data.get('outline') and data.get('document_id'):
            latest[data['document_id']] = index
    return set(latest.values())


def redundant(message: BaseMessage) -> bool:
    """A result whose information another message carries: a stub or a notice."""
    return isinstance(message, ToolMessage) and (facts_of(message)['level'] == STUB or bool(unchanged_of(message)))


def prune(messages: list[BaseMessage], until: int) -> list[BaseMessage]:
    """L1: before index `until` (a turn start), remove whole model turns whose results
    are all redundant and whose response has no text, and runtime notices a newer one
    replaced. Calls and their results are removed together."""
    from app.agents.progress import STREAK_KEY, read_only_turn
    later_work = [i for i, m in enumerate(messages) if isinstance(m, AIMessage) and not read_only_turn(m)]
    goal_states = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.additional_kwargs.get('goal_state')]
    removed: set[int] = set()
    for index, message in enumerate(messages[:until]):
        if isinstance(message, AIMessage) and message.tool_calls and not text_of(message).strip():
            ids = {call.get('id') for call in message.tool_calls}
            end = index + 1
            while end < len(messages) and isinstance(messages[end], ToolMessage):
                end += 1
            results = messages[index + 1:end]
            if end <= until and {m.tool_call_id for m in results} == ids and all(redundant(m) for m in results):
                removed.update(range(index, end))
        elif isinstance(message, HumanMessage):
            if index in goal_states[:-1]:
                removed.add(index)
            elif STREAK_KEY in message.additional_kwargs and any(i > index for i in later_work):
                # The streak it counted has ended; read_streak no longer needs it.
                removed.add(index)
    return [m for i, m in enumerate(messages) if i not in removed]


def fold(body: list[BaseMessage], budget: int | None = None) -> list[BaseMessage]:
    """L1 and L2: replace redundant results by stubs, fold recomputable results before
    the verbatim tier (excerpts, then pointers), shrink large call arguments there, prune
    redundant turns, and place an updated ledger at the verbatim boundary. Durable
    results are left for a work-segment summary (app.agents.memory)."""
    excerpt_start, boundary = tier_bounds(body, budget)
    if not boundary:
        return list(body)
    turns = [i for i, m in enumerate(body) if isinstance(m, AIMessage)]
    current = turns[-1] if turns else len(body)
    calls = calls_by_id(body)
    stubs = supersession(body, current)
    outlines = latest_outlines(body, calls)
    # An earlier result a notice in the verbatim tier stands for stays as it is.
    pinned = {unchanged_of(m) for m in body[boundary:] if unchanged_of(m)}
    ledger = record_facts(ledger_of(body), [m for m in body[:boundary] if not is_ledger(m)], calls)
    entry = ledger_message(ledger)
    previous = next((m for m in body if is_ledger(m)), None)
    if entry is not None and previous is not None and previous.content == entry.content:
        entry = previous  # Unchanged facts keep the same message and its cache prefix.
    start = turn_start(body, boundary)
    goal_states = [i for i, m in enumerate(body) if isinstance(m, HumanMessage) and m.additional_kwargs.get('goal_state')]
    folded, until = [], None
    for index, message in enumerate(body):
        if index == start:
            until = len(folded)
            if entry is not None:
                folded.append(entry)
        if is_ledger(message):
            continue
        if index in goal_states[:-1] and index < current and not message.content.startswith('[Superseded goal reminder'):
            message = message.model_copy(update={'content': '[Superseded goal reminder; a newer one follows.]'})
        level = EXCERPT if index >= excerpt_start else POINTER
        if isinstance(message, ToolMessage):
            call = calls.get(message.tool_call_id) or {}
            name, args = tool_name(message, calls), call.get('args') or {}
            if index in stubs and message.id not in pinned:
                stub = stubs[index] + f' recall_context(message_id="{message.id}") returns this older result.'
                if text_of(message) != stub:
                    message = with_facts(message, STUB, content=stub, artifact=None)
            elif index < boundary and message.id not in pinned and recomputable(name, args):
                message = fold_result(message, name, args, level, keep_outline=index in outlines)
        elif isinstance(message, AIMessage) and message.tool_calls and index < boundary:
            message = fold_call(message, level)
        folded.append(message)
    return prune(folded, len(folded) if until is None else until)


# --- work segments (L3) -------------------------------------------------------

SEGMENT_MIN_TOKENS = 1500
SEGMENT_MAX_TOKENS = 24000


def segment_break(message: BaseMessage) -> bool:
    """Messages a work segment never spans: task and user inputs, summaries, the ledger."""
    return isinstance(message, HumanMessage) and not message.additional_kwargs.get('reissuable_reminder') \
        and not message.additional_kwargs.get('goal_state')


def milestone(message: AIMessage, results: list[BaseMessage]) -> bool:
    """A turn that changed durable state: a successful call of a durable tool."""
    ok = {m.tool_call_id for m in results if isinstance(m, ToolMessage) and not failed(m)}
    return any(call.get('id') in ok and not recomputable(call['name'], call.get('args') or {})
               for call in message.tool_calls)


def segments(body: list[BaseMessage], budget: int | None = None) -> list[tuple[int, int]]:
    """[start, end) spans of completed work before the verbatim tier (the ledger marks
    its boundary once anything was folded): whole turns from one durable change to the next, between inputs.
    A span holding the newest outline of a document (its section IDs) or a result an
    unchanged notice stands for is kept; unresolved failures stay listed in the task
    ledger with their message IDs. Spans are bounded by SEGMENT_MAX_TOKENS."""
    until = next((i for i, m in enumerate(body) if is_ledger(m)), None)
    if until is None:
        until = verbatim_boundary(body, budget)
    if not until:
        return []
    calls = calls_by_id(body)
    keep = latest_outlines(body, calls)
    pinned = {unchanged_of(m) for m in body[until:] if unchanged_of(m)}
    spans, begin, size, index = [], None, 0, 0
    while index < until:
        message = body[index]
        if segment_break(message) or isinstance(message, ToolMessage):
            begin, size = None, 0  # an orphan result never starts a segment
            index += 1
            continue
        if isinstance(message, HumanMessage):  # a reminder within the work
            begin = index if begin is None else begin
            size += facts_of(message)['tokens']
            index += 1
            continue
        end = index + 1
        while end < until and isinstance(body[end], ToolMessage):
            end += 1
        if message.tool_calls and {m.tool_call_id for m in body[index + 1:end]} != {c.get('id') for c in message.tool_calls}:
            begin, size, index = None, 0, end
            continue
        turn = sum(facts_of(m)['tokens'] for m in body[index:end])
        if begin is not None and size + turn > SEGMENT_MAX_TOKENS:
            spans.append((begin, index))
            begin, size = None, 0
        begin = index if begin is None else begin
        size += turn
        if milestone(message, body[index + 1:end]):
            spans.append((begin, end))
            begin, size = None, 0
        index = end
    complete = []
    for begin, end in spans:
        span = body[begin:end]
        if (sum(facts_of(m)['tokens'] for m in span) >= SEGMENT_MIN_TOKENS
                and not any(m.id in pinned for m in span)
                and not any(i in keep for i in range(begin, end))):
            complete.append((begin, end))
    return complete
