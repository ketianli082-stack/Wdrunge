"""The text format of a read_project_file result, written and parsed in one place.

    [sha256=<hash>; characters=<total>]          only with include_hash
    <body>
    [lines 12–40, characters 9800–13400 of 23528; next_offset=13400]

The last line appears on a partial read (one that does not start at 0 or does not
reach the end), so the reader knows where it is in the file and how long the file is;
next_offset is absent when the read reached the end. A whole-file read is the body
alone. A read past the end returns one line saying so. The tool, the desk and the
context fold all go through parse_read; histories written before the position line
existed carry `[truncated; next_offset=N]`, which is still understood.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.tools.arguments import strip_notice

HEADER = re.compile(r'\A\[sha256=(\w+); characters=(\d+)\]\n')
POSITION = re.compile(r'\n?\[(?:lines (\d+)–(\d+), )?characters (\d+)–(\d+)(?: of (\d+))?(?:; next_offset=(\d+))?\]\Z')
LEGACY = re.compile(r'\n?\[truncated; next_offset=(\d+)\]\Z')
PAST_END = re.compile(r'\A\[offset (\d+) 处没有内容；文件共 (\d+) 字符\]\Z')


@dataclass
class ReadResult:
    body: str
    start: int = 0
    total: int | None = None        # characters in the file, when the result says
    next_offset: int | None = None  # where the file continues; None at its end
    sha: str | None = None
    lines: tuple[int, int] | None = None

    @property
    def end(self) -> int:
        return self.start + len(self.body)

    @property
    def whole(self) -> bool:
        return self.start == 0 and self.next_offset is None


def parse_read(text: str, offset: int = 0) -> ReadResult:
    """Parse a result; `offset` is the call's offset, used when the text carries no position."""
    text = strip_notice(text)
    sha = total = None
    header = HEADER.match(text)
    if header:
        sha, total, text = header.group(1), int(header.group(2)), text[header.end():]
    past = PAST_END.match(text)
    if past:
        return ReadResult('', int(past.group(1)), int(past.group(2)), None, sha)
    position = POSITION.search(text)
    if position:
        first, last, start, _end, of, following = position.groups()
        return ReadResult(text[:position.start()], int(start), int(of) if of else total,
                          int(following) if following else None, sha, (int(first), int(last)) if first else None)
    legacy = LEGACY.search(text)
    if legacy:
        return ReadResult(text[:legacy.start()], int(offset or 0), total, int(legacy.group(1)), sha)
    return ReadResult(text, int(offset or 0), total, None, sha)


def format_read(body: str, start: int = 0, total: int | None = None, sha: str | None = None,
                lines: tuple[int, int] | None = None, more: bool | None = None) -> str:
    """Render a result. `more` says whether the file continues after the body when the
    total is unknown (a range merged from older results)."""
    end = start + len(body)
    if total is not None and start > 0 and start >= total:
        return (f'[sha256={sha}; characters={total}]\n' if sha else '') + f'[offset {start} 处没有内容；文件共 {total} 字符]'
    continues = more if total is None else end < total
    head = f'[sha256={sha}; characters={total}]\n' if sha and total is not None else ''
    if start == 0 and not continues:
        return head + body
    where = (f'lines {lines[0]}–{lines[1]}, ' if lines else '') + f'characters {start}–{end}' + (f' of {total}' if total is not None else '')
    return f"{head}{body}\n[{where}{f'; next_offset={end}' if continues else ''}]"
