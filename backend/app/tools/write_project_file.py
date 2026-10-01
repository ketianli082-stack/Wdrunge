"""Create, replace, or atomically edit text within a role's assigned directories."""

from pathlib import Path

from app.tools.validation import tool

from app.tools._workspace import resolve_allowed
from app.tools._file_edits import update_file
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    old_text: str = Field(min_length=1)
    new_text: str


class FileWrite(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    path: str = Field(min_length=1)
    content: str | None = Field(default=None, max_length=100000)
    edits: list[TextEdit] | None = Field(default=None, min_length=1, max_length=100)
    expected_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def exactly_one_mode(self):
        if (self.content is None) == (self.edits is None):
            raise ValueError('Provide exactly one of content (whole file) or edits (partial changes)')
        return self


def make_write_project_file(allowed_roots: tuple[str, ...]):
    @tool("write_project_file", args_schema=FileWrite)
    def write_project_file(path: str, content: str | None = None, edits: list[TextEdit] | None = None, expected_hash: str | None = None) -> str:
        """Create a UTF-8 file with content, or edit an existing file with edits:
        [{old_text: exact nonempty original text, new_text: replacement text}].
        Provide exactly one mode. Each old_text must match once; all edits commit
        together, in order, or none do. Prefer edits for existing files. Read first;
        include surrounding context for ambiguous matches. Optionally pass the
        SHA-256 from read_project_file(include_hash=true) as expected_hash to reject
        stale edits. Paths are relative to the workspace. Managed Markdown still
        requires collaborate publish with a base version and change summary.
        """
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
        length, revision = update_file(target, content, edits, expected_hash)
        return f"Wrote {path} ({length} characters; sha256={revision})."

    return write_project_file
