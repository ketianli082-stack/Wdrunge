"""Create or replace a text file within a role's assigned directories."""

from pathlib import Path

from langchain_core.tools import tool

from app.tools._workspace import resolve_allowed


def make_write_project_file(allowed_roots: tuple[str, ...]):
    @tool("write_project_file")
    def write_project_file(path: str, content: str) -> str:
        """Write a complete UTF-8 project file at a path relative to the selected workspace."""
        target = resolve_allowed(path, allowed_roots)
        from app.collaboration.tools import active
        ctx = active.get()
        if ctx and bool(Path(path).parts) and Path(path).parts[0].lower() == 'docs' and target.suffix.lower() == '.md':
            return 'Shared Markdown must use collaborate publish with a base version and summary; use base_version=0 for a new document.'
        if ctx and ctx.service.store.sqlite_path:
            import sqlite3
            import json
            with sqlite3.connect(ctx.service.store.sqlite_path) as db:
                records = db.execute("SELECT data FROM collaboration_records WHERE kind='document' AND project=?", (ctx.project,)).fetchall()
                if any(str(Path(path)) == json.loads(row[0])['path'] for row in records):
                    return 'Managed document: use collaborate publish with base_version and a change summary.'
        if len(content) > 100000:
            return "File is too large; keep content under 100000 characters."
        if target.exists() and not target.is_file():
            return f"Not a regular file: {path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {path} ({len(content)} characters)."

    return write_project_file
