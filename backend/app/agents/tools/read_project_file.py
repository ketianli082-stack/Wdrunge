"""Read a UTF-8 project file within a role's scope."""

from app.agents.tools.validation import tool

from app.agents.tools._workspace import resolve_allowed, published_documents, published_hint
from app.agents.tools._file_edits import file_hash
from typing import Annotated
from pydantic import Field


def make_read_project_file(allowed_roots: tuple[str, ...]):
    @tool("read_project_file")
    def read_project_file(path: str, offset: Annotated[int, Field(ge=0)] = 0, limit: Annotated[int, Field(ge=1, le=30000)] = 30000, include_hash: bool = False) -> str:
        """Read UTF-8 text at a relative path. offset/limit page through characters.
        include_hash=true includes the full-file SHA-256 for write_project_file's
        expected_hash. Read relevant content before making exact partial edits.
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
        excerpt = content[offset:offset+limit]
        if len(content) > offset+limit:
            excerpt += f"\n[truncated; next_offset={offset+limit}]"
        return (f"[sha256={file_hash(data)}; characters={len(content)}]\n" if include_hash else "") + excerpt

    return read_project_file
