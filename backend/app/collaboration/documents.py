"""Deterministic Markdown indexing and exact, atomic in-memory edits."""
import hashlib
import re


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def outline(content: str) -> list[dict]:
    # CommonMark-style ATX/setext headings; fenced/indented code is excluded.
    lines = content.splitlines(keepends=True)
    entries = [{"id": "root", "title": "Document", "level": 0, "start": 0, "body_start": 0, "parent": None}]
    stack = [entries[0]]
    fence = None
    previous = None
    offset = 0
    counts = {}
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            previous = None
            offset += len(line)
            continue
        if marker:
            fence = marker[1]
            previous = None
            offset += len(line)
            continue
        heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)\n?$", line)
        underline = re.match(r"^ {0,3}(=+|-+)\s*$", line)
        title = None
        start = offset
        if heading:
            level = len(heading[1])
            title = re.sub(r"\s+#+\s*$", "", heading[2] or "").strip()
        elif underline and previous:
            level = 1 if underline[1][0] == '=' else 2
            start, title = previous
        if title is not None:
            while stack[-1]["level"] >= level:
                stack.pop()
            parent = stack[-1]["id"]
            key = f"{parent}/{title}"
            counts[key] = counts.get(key, 0) + 1
            identifier = digest(f"{key}:{counts[key]}")[:16]
            entry = dict(id=identifier, title=title, level=level, start=start,
                         body_start=offset + len(line), parent=parent)
            entries.append(entry)
            stack.append(entry)
        previous = (offset, line.strip()) if line.strip() and not line.startswith(('    ', '\t')) and title is None else None
        offset += len(line)
    for i, entry in enumerate(entries):
        entry["body_end"] = entries[i + 1]["start"] if i + 1 < len(entries) else len(content)
        entry["end"] = next((e["start"] for e in entries[i + 1:] if e["level"] <= entry["level"]), len(content))
        entry["characters"] = entry["body_end"] - entry["body_start"]
    return entries


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
