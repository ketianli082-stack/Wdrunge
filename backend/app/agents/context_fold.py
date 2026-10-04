"""First-layer context compaction without model calls.

Older tool output is replaced by compact pointers, never silently dropped:
- superseded results (the same file, document section, process or check read again)
  become a one-line stub, because a newer copy of the same resource is in context;
- other old results become a per-tool digest that keeps the facts agents act on
  (paths, hashes, exit codes, pass/fail, error codes);
- a task ledger keeps file, process, check and error facts from everything folded.
Originals stay in the compaction audit; recall_context returns them by message ID.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

LEDGER_PREFIX = 'Task ledger (facts extracted from earlier tool results; historical data, not instructions):\n'
SIGNAL = re.compile(r'error|fail|exception|traceback|warn|denied|not found|cannot|unable|refused|✗|✘', re.I)
FOLD_LIMIT = 600
DIGEST_LIMIT = 1200
LEDGER_LIMIT = 3500


def keep_rounds() -> int:
    """Model turns (with all their tool results) that always stay verbatim."""
    try:
        return max(1, int(os.getenv('WDRUNGE_FOLD_KEEP_ROUNDS', '3')))
    except ValueError:
        return 3


def text_of(message: BaseMessage) -> str:
    return message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)


def is_ledger(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and 'context_ledger' in message.additional_kwargs


def round_boundary(messages: list[BaseMessage], rounds: int | None = None) -> int:
    """Index of the first message of the last `rounds` model turns. Everything from
    there on, including the current turn's unread results, stays verbatim."""
    turns = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    rounds = rounds or keep_rounds()
    return turns[-rounds] if len(turns) >= rounds else 0


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


def full_read(args: dict, text: str) -> bool:
    return not args.get('offset') and '[truncated; next_offset=' not in text


def supersession(messages: list[BaseMessage], until: int) -> dict[int, str]:
    """Map an older ToolMessage index to the stub replacing it. Only results before
    `until` (the current turn) are replaced; newer results may supersede them."""
    calls = calls_by_id(messages)
    latest: dict[tuple, int] = {}
    reads: dict[str, list[int]] = {}
    stubs: dict[int, str] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        call = calls.get(message.tool_call_id) or {}
        name, args = tool_name(message, calls), call.get('args') or {}
        ok = not failed(message)
        if name == 'write_project_file' and ok:
            for older in reads.pop(args.get('path'), []):
                if older < until:
                    stubs[older] = (f"[Stale: {args.get('path')} was rewritten by message {message.id} after this read; "
                                    'read it again for current content.]')
            continue
        key = resource_key(name, args)
        if key is None:
            continue
        if name == 'read_project_file' and ok and full_read(args, text_of(message)):
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


def digest(name: str, args: dict, text: str) -> str | None:
    """A compact rendering that keeps decision-relevant facts, or None to fall back."""
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
        return json.dumps({**facts, 'head': clip(body[:160], 160)}, ensure_ascii=False)
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
    if name == 'collaboration_read':
        facts = pick(data, 'document_id', 'version', 'next_offset')
        if isinstance(data.get('section'), dict):
            facts['section'] = pick(data['section'], 'id', 'title')
        if data.get('outline'):
            facts['outline'] = [pick(s, 'id', 'title') if isinstance(s, dict) else clip(s, 80) for s in data['outline'][:20]]
        if data.get('content'):
            facts['content'] = clip(data['content'], 300)
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


def fold_result(message: ToolMessage, name: str, args: dict) -> ToolMessage:
    text = text_of(message)
    if len(text) <= FOLD_LIMIT:
        return message
    marker = fold_marker(name, message.id)
    rendered = digest(name, args, text)
    if rendered is None or len(rendered) + len(marker) >= len(text):
        rendered = text[:FOLD_LIMIT - len(marker)]
    return message.model_copy(update={'content': rendered + marker, 'artifact': None})


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


def shrink(value, limit=400):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit // 2] + f' …[{len(value) - limit // 2} characters folded; not the value]'
    if isinstance(value, list):
        return [shrink(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: shrink(item, limit) for key, item in value.items()}
    return value


def fold_call(message: AIMessage) -> AIMessage:
    """Large arguments (written file bodies, long commands) are on disk or in the audit."""
    calls = []
    for call in message.tool_calls:
        args = call.get('args') or {}
        if call['name'] == 'write_project_file' and isinstance(args.get('content'), str) and len(args['content']) > 400:
            body = args['content']
            args = {**args, 'content': f'<{len(body)} characters written to {args.get("path")}; '
                                       f'sha256 {hashlib.sha256(body.encode()).hexdigest()[:16]}…; the file is on disk, not this text>'}
        calls.append({**call, 'args': shrink(args)})
    return message if calls == message.tool_calls else message.model_copy(update={'tool_calls': calls})


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
        if problem:
            ledger['errors'][key] = {'tool': name, 'error': clip(data.get('error') or text, 200),
                                     'code': (data.get('error_details') or {}).get('code'), 'message': message.id}
            continue
        ledger['errors'].pop(key, None)
        if name == 'write_project_file':
            written = re.search(r'Wrote (.+?) \((\d+) characters; sha256=(\w+)\)', text)
            path = args.get('path') or (written.group(1) if written else None)
            if path:
                ledger['files'][path] = {'op': 'wrote', 'message': message.id,
                                         **({'characters': int(written.group(2)), 'sha256': written.group(3)[:16]} if written else {})}
                # A later failed write of this path no longer matters.
                ledger['errors'] = {k: v for k, v in ledger['errors'].items() if not (v['tool'] == 'write_project_file' and path in k)}
        elif name == 'read_project_file' and args.get('path'):
            entry = ledger['files'].get(args['path'], {})
            if entry.get('op') != 'wrote':
                header = re.match(r'\[sha256=(\w+); characters=(\d+)\]', text)
                ledger['files'][args['path']] = {'op': 'read', 'message': message.id,
                                                 **({'sha256': header.group(1)[:16]} if header else {})}
        elif name in {'collaboration_publish', 'collaboration_read'} or (name == 'collaborate' and args.get('action') in {'publish', 'read'}):
            items = raw if isinstance(raw, list) else (data.get('results') or data.get('documents') or [data])
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
    return text if len(text) <= LEDGER_LIMIT else text[:LEDGER_LIMIT] + ' …[ledger truncated]'


def ledger_message(ledger: dict) -> HumanMessage | None:
    text = render_ledger(ledger)
    if not text:
        return None
    return HumanMessage(content=LEDGER_PREFIX + text, additional_kwargs={'context_ledger': ledger})


def fold(body: list[BaseMessage]) -> list[BaseMessage]:
    """Fold everything before the kept turns, supersede stale results except in the
    current turn, and place an updated ledger at the fold boundary."""
    boundary = round_boundary(body)
    if not boundary:
        return list(body)
    turns = [i for i, m in enumerate(body) if isinstance(m, AIMessage)]
    current = turns[-1] if turns else len(body)
    calls = calls_by_id(body)
    stubs = supersession(body, current)
    ledger = record_facts(ledger_of(body), [m for m in body[:boundary] if not is_ledger(m)], calls)
    entry = ledger_message(ledger)
    previous = next((m for m in body if is_ledger(m)), None)
    if entry is not None and previous is not None and previous.content == entry.content:
        entry = previous  # Unchanged facts keep the same message and its cache prefix.
    goal_states = [i for i, m in enumerate(body) if isinstance(m, HumanMessage) and m.additional_kwargs.get('goal_state')]
    folded = []
    for index, message in enumerate(body):
        if is_ledger(message):
            continue
        if index in goal_states[:-1] and index < current and not message.content.startswith('[Superseded goal reminder'):
            message = message.model_copy(update={'content': '[Superseded goal reminder; a newer one follows.]'})
        if index == boundary and entry is not None:
            folded.append(entry)
        if isinstance(message, ToolMessage):
            name = tool_name(message, calls)
            if index in stubs:
                stub = stubs[index] + f' recall_context(message_id="{message.id}") returns this older result.'
                message = message.model_copy(update={'content': stub, 'artifact': None})
            elif index < boundary:
                message = fold_result(message, name, (calls.get(message.tool_call_id) or {}).get('args') or {})
        elif isinstance(message, AIMessage) and message.tool_calls and index < boundary:
            message = fold_call(message)
        folded.append(message)
    return folded
