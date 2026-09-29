"""Read a UTF-8 project file within a role's scope."""

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed


def make_read_project_file(allowed_roots: tuple[str, ...]):
    @tool("read_project_file")
    def read_project_file(path: str) -> str:
        """Read a project text file by its path relative to the repository root."""
        target = resolve_allowed(path, allowed_roots)
        if not target.is_file():
            return f"File not found: {path}"
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Not a UTF-8 text file: {path}"
        return content[:30000] + ("\n[truncated]" if len(content) > 30000 else "")

    return read_project_file
