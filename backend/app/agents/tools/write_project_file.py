"""Create, replace, or atomically edit text within a role's assigned directories."""

import re
from pathlib import Path

from app.agents.tools.validation import tool

from app.agents.tools._workspace import resolve_allowed, published_documents
from app.agents.tools._file_edits import update_file
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    old_text: str = Field(min_length=1)
    new_text: str


class FileWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1)
    content: str | None = Field(default=None, max_length=100000)
    edits: list[TextEdit] | None = Field(default=None, min_length=1, max_length=100,
        examples=[[{'old_text': 'exact original text', 'new_text': 'replacement text'}]])
    expected_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def exactly_one_mode(self):
        if (self.content is None) == (self.edits is None):
            raise ValueError('Provide exactly one of content (whole file) or edits (partial changes)')
        return self


def write_view(result: str, arguments: dict) -> str:
    """The receipt without the 64-character hash (Blog5: 2 of 195 writes used
    expected_hash; read_project_file(include_hash=true) returns it when needed)."""
    return re.sub(r'\A(Wrote .+? \(\d+ characters); sha256=\w+\)', r'\1)', result, count=1) if isinstance(result, str) else result


def make_write_project_file(allowed_roots: tuple[str, ...]):
    @tool("write_project_file", args_schema=FileWrite, model_view=write_view)
    def write_project_file(path: str, content: str | None = None, edits: list[TextEdit] | None = None, expected_hash: str | None = None) -> str:
        """Create a file with content, or atomically edit an existing one with
        edits [{old_text, new_text}] (each old_text matches once). One mode only;
        expected_hash rejects stale edits. Not for managed docs (use collaboration_publish).
        """
        target = resolve_allowed(path, allowed_roots)
        from app.collaboration.tools import active
        ctx = active.get()
        published = [d for d in published_documents(path) if d['path'] == str(Path(path))]
        if published:
            d = published[0]
            raise ValueError(f"{path} is already a published shared document (document_id={d['document_id']}, version={d['version']}); "
                             'it already counts as delivered. To change it use collaboration_publish with '
                             f"changes=[{{document_id: '{d['document_id']}', base_version: {d['version']}, summary, edits}}].")
        if ctx and bool(Path(path).parts) and Path(path).parts[0].lower() == 'docs' and target.suffix.lower() == '.md':
            raise ValueError('Shared Markdown must use collaboration_publish (legacy: collaborate publish) with changes=[{path,base_version:0,summary,content}] for new documents; existing documents require document_id, base_version and edits.')
        length, revision = update_file(target, content, edits, expected_hash)
        receipt = f"Wrote {path} ({length} characters; sha256={revision})."
        # The written text folds out of the context later; its declarations (names,
        # signatures, selectors, with line numbers) stay in the receipt and the ledger.
        from app.agents.context_fold import structure
        try:
            outline = structure(path, target.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError):
            outline = ''
        return receipt + (f"\nStructure: {outline}" if outline else '')

    return write_project_file
