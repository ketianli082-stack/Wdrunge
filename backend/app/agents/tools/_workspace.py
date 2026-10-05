"""Path checks shared by project file tools."""

from pathlib import Path
from contextvars import ContextVar
from app.agents.tools.validation import ToolFailure


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
source_workspace: ContextVar[Path | None] = ContextVar("source_workspace", default=None)
active_workspace: ContextVar[Path | None] = ContextVar("active_workspace", default=None)


def workspace_root() -> Path:
    return active_workspace.get() or WORKSPACE_ROOT


def effective_roots(allowed_roots: tuple[str, ...]) -> tuple[str, ...]:
    # The built-in role areas describe Wdrunge itself. Other projects can use
    # different layouts, so their selected directory is the project boundary.
    return allowed_roots if (source_workspace.get() or workspace_root()).resolve() == WORKSPACE_ROOT.resolve() else (".",)


def control_paths() -> list[Path]:
    from platformdirs import user_data_path
    from app.execution import permissions as policy
    paths = [user_data_path('Wdrunge', 'Wdrunge').resolve()]
    current = getattr(policy, 'current', None)
    database = getattr(getattr(current, 'store', None), 'sqlite_path', None)
    if database:
        paths += [Path(str(database) + suffix).resolve() for suffix in ('', '-wal', '-shm')]
    return paths


def is_control_path(candidate: Path) -> bool:
    candidate = candidate.resolve()
    return any(candidate == root or candidate.is_relative_to(root) for root in control_paths())


def resolve_allowed(path: str, allowed_roots: tuple[str, ...]) -> Path:
    relative = Path(path)
    def protected(parts):
        return any(part in {'.git', '.wdrunge-runs', '.aws', '.ssh', '.codex', '.agents', '.env'}
                   or part.startswith('.env.') and part not in {'.env.example', '.env.sample'} for part in parts)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Use a relative path inside the assigned project area")
    def reject_protected():
        raise ToolFailure('Protected configuration or credential path cannot be accessed by project file tools',
            'permission_denied', 'protected_path',
            'Do not bypass this boundary with execute_command or another file tool. Use .env.example/.env.sample for non-secret configuration templates; '
            'existing configuration can be read only through read_external_file with explicit authorization. Secret writes require human handling.',
            'after_permission')
    if protected(relative.parts):
        reject_protected()

    workspace = workspace_root().resolve()
    candidate = (workspace / relative).resolve()
    if is_control_path(candidate):
        raise PermissionError('Agent tools cannot access Wdrunge control-plane files')
    roots = [(workspace / root).resolve() for root in effective_roots(allowed_roots)]
    if not candidate.is_relative_to(workspace):
        raise ValueError("Path is outside the project workspace")
    if protected(candidate.relative_to(workspace).parts):
        reject_protected()
    if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
        raise ValueError("Path is outside this agent's assigned project area")
    return candidate


def published_documents(path: str) -> list[dict]:
    """Shared documents of the active project at `path` or under it (as a folder)."""
    from app.collaboration.tools import active
    import json
    import sqlite3
    ctx = active.get()
    database = getattr(getattr(ctx.service, 'store', None), 'sqlite_path', None) if ctx else None
    if not database:
        return []
    prefix = str(Path(path))
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT data FROM collaboration_records WHERE kind='document' AND project=?", (ctx.project,)).fetchall()
    documents = [json.loads(row[0]) for row in rows]
    return [{'document_id': d['id'], 'path': d['path'], 'version': d['version']} for d in documents
            if prefix in {'.', d['path']} or d['path'].startswith(prefix.rstrip('/') + '/')]


def published_hint(documents: list[dict]) -> str:
    listed = ', '.join(f"{d['path']} (document_id={d['document_id']}, version={d['version']})" for d in documents[:5])
    return (f' Published shared document(s): {listed}. They live in the project, not necessarily in this task '
            'snapshot; read them with collaboration_read and change them with collaboration_publish.')


def tool_available(name: str) -> bool:
    """Generators bound to Wdrunge's own layout are shown only where they can run."""
    root = workspace_root()
    if name == 'refresh_api_contract':
        return (root / 'backend' / 'scripts' / 'export_openapi.py').is_file()
    if name == 'refresh_frontend_client':
        import json
        try:
            return 'api:generate' in json.loads((root / 'frontend' / 'package.json').read_text()).get('scripts', {})
        except (OSError, ValueError, AttributeError):
            return False
    return True
