"""Path checks shared by project file tools."""

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def resolve_allowed(path: str, allowed_roots: tuple[str, ...]) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Use a relative path inside the assigned project area")

    candidate = (WORKSPACE_ROOT / relative).resolve()
    roots = [(WORKSPACE_ROOT / root).resolve() for root in allowed_roots]
    workspace = WORKSPACE_ROOT.resolve()
    if not candidate.is_relative_to(workspace):
        raise ValueError("Path is outside the project workspace")
    if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
        raise ValueError("Path is outside this agent's assigned project area")
    return candidate
