"""Path checks shared by project file tools."""

from pathlib import Path
from contextvars import ContextVar


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
source_workspace: ContextVar[Path | None] = ContextVar("source_workspace", default=None)
active_workspace: ContextVar[Path | None] = ContextVar("active_workspace", default=None)


def workspace_root() -> Path:
    return active_workspace.get() or WORKSPACE_ROOT


def effective_roots(allowed_roots: tuple[str, ...]) -> tuple[str, ...]:
    # The built-in role areas describe Wdrunge itself. Other projects can use
    # different layouts, so their selected directory is the project boundary.
    return allowed_roots if (source_workspace.get() or workspace_root()).resolve() == WORKSPACE_ROOT.resolve() else (".",)


def resolve_allowed(path: str, allowed_roots: tuple[str, ...]) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or any(part in {".git", ".wdrunge-runs", ".aws", ".codex", ".agents"} for part in relative.parts):
        raise ValueError("Use a relative path inside the assigned project area")

    workspace = workspace_root().resolve()
    candidate = (workspace / relative).resolve()
    roots = [(workspace / root).resolve() for root in effective_roots(allowed_roots)]
    if not candidate.is_relative_to(workspace):
        raise ValueError("Path is outside the project workspace")
    if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
        raise ValueError("Path is outside this agent's assigned project area")
    return candidate
