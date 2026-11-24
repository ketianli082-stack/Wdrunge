"""Read a UTF-8 project file within a role's scope."""

from app.agents.tools.validation import tool
from app.agents.context_policy import DESK
from app.agents.read_result import format_read

from app.agents.tools._workspace import resolve_allowed, published_documents, published_hint
from app.agents.tools._file_edits import file_hash
from typing import Annotated
from pydantic import Field

READ_LIMIT = 30000


def line_span(content: str, start_line: int, line_count: int | None) -> tuple[int, int, tuple[int, int]]:
    """Character range of the requested lines (the code index and search results give
    line numbers), and the lines it covers."""
    lines = content.splitlines(keepends=True)
    first = min(start_line, len(lines) + 1)
    last = len(lines) if line_count is None else min(len(lines), first + line_count - 1)
    start = sum(len(line) for line in lines[:first - 1])
    return start, start + sum(len(line) for line in lines[first - 1:last]), (first, max(first, last))


def make_read_project_file(allowed_roots: tuple[str, ...]):
    @tool("read_project_file", context_policy=DESK)
    def read_project_file(path: str, offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=READ_LIMIT)] = READ_LIMIT,
                          start_line: Annotated[int | None, Field(ge=1, description='Read from this line (1-based) instead of a character offset')] = None,
                          line_count: Annotated[int | None, Field(ge=1, description='Lines to read from start_line; omitted = to the end')] = None,
                          include_hash: bool = False,
                          force: Annotated[bool, Field(description='Return the text even when your desk already shows it')] = False) -> str:
        """Read UTF-8 text at a relative path: the whole file, lines (start_line,
        line_count) or characters (offset, limit). A partial result ends with its
        position and the file length. include_hash=true adds the SHA-256 for
        write_project_file's expected_hash.
        """
        target = resolve_allowed(path, allowed_roots)
        if not target.is_file():
            documents = published_documents(path)
            hint = published_hint(documents) if documents else ' Use list_project_files to inspect available paths.'
            raise FileNotFoundError(f"File not found in this workspace: {path}.{hint}")
        try:
            data = target.read_bytes()
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Not a UTF-8 text file: {path}") from exc
        lines = None
        if start_line is not None:
            if offset:
                raise ValueError('Give start_line or offset, not both')
            offset, end, lines = line_span(content, start_line, line_count)
            if end - offset > limit:
                # Cut at the limit: the position line reports only whole lines returned.
                end = offset + limit
                lines = (lines[0], lines[0] + content[offset:end].count('\n') - 1) if '\n' in content[offset:end] else None
        elif line_count is not None:
            raise ValueError('line_count needs start_line')
        else:
            end = offset + limit
        return format_read(content[offset:end], offset, len(content), file_hash(data) if include_hash else None, lines)

    return read_project_file
