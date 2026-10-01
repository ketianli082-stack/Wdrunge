"""Deterministic Markdown indexing and exact, atomic in-memory edits."""
import hashlib


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def outline(content: str) -> list[dict]:
    from markdown_it import MarkdownIt
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    entries = [{"id": "root", "title": "Document", "level": 0, "start": 0, "body_start": 0, "parent": None}]
    stack, counts = [entries[0]], {}
    tokens = MarkdownIt('commonmark').parse(content)
    for i, token in enumerate(tokens):
        if token.type != 'heading_open' or token.level != 0 or token.map is None:
            continue
        level, title = int(token.tag[1:]), tokens[i+1].content
        while stack[-1]['level'] >= level:
            stack.pop()
        parent = stack[-1]['id']
        key = f'{parent}/{title}'
        counts[key] = counts.get(key, 0) + 1
        entry = dict(id=digest(f'{key}:{counts[key]}')[:16], title=title, level=level,
                     start=offsets[token.map[0]], body_start=offsets[token.map[1]], parent=parent)
        entries.append(entry)
        stack.append(entry)
    for i, entry in enumerate(entries):
        entry['body_end'] = entries[i+1]['start'] if i+1 < len(entries) else len(content)
        entry['end'] = next((e['start'] for e in entries[i+1:] if e['level'] <= entry['level']), len(content))
        entry['characters'] = entry['body_end']-entry['body_start']
    return entries


def page(content: str, offset: int, limit: int) -> tuple[str, int | None, bool]:
    """Prefer complete top-level Markdown blocks; explicitly label oversized fragments."""
    from markdown_it import MarkdownIt
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1]+len(line))
    boundaries = {0, len(content)}
    for token in MarkdownIt('commonmark').parse(content):
        if token.level == 0 and token.map:
            boundaries.update(offsets[i] for i in token.map)
    end = min(len(content), offset+limit)
    natural = [b for b in boundaries if offset < b <= end]
    if natural:
        end = max(natural)
    return content[offset:end], end if end < len(content) else None, offset not in boundaries or end not in boundaries


def section(content: str, section_id: str) -> dict:
    return next((s for s in outline(content) if s['id'] == section_id), None) or (_ for _ in ()).throw(ValueError("Section not found; reload outline"))


def edit_document(content: str, edits: list[dict]) -> str:
    for edit in edits:
        s = section(content, edit['section_id'])
        start, end = s['body_start'], s['body_end']
        op = edit['operation']
        if op == 'replace_text':
            old = edit.get('old_text', '')
            body = content[start:end]
            if not old or body.count(old) != 1:
                raise ValueError('old_text must match exactly once in the direct section body')
            content = content[:start] + body.replace(old, edit.get('new_text', ''), 1) + content[end:]
        elif op == 'replace_section_body':
            content = content[:start] + edit.get('new_text', '') + content[end:]
        elif op == 'insert_section':
            content = content[:s['end']] + '\n' + edit['new_text'] + '\n' + content[s['end']:]
        elif op == 'delete_section':
            if s['id'] == 'root':
                raise ValueError('Cannot delete document root')
            if s['body_end'] != s['end'] and not edit.get('include_children'):
                raise ValueError('Explicit include_children required')
            content = content[:s['start']] + content[s['end']:]
        else:
            raise ValueError('Unsupported edit operation')
    if len(content) > 500_000:
        raise ValueError('Document exceeds 500,000 characters')
    return content
