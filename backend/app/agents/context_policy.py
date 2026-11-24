"""How a tool's results may be compacted, declared on the tool itself.

- recomputable: the result can be obtained again (a search, a listing, a status, a
  check that can be re-run) or its decision-relevant facts are kept by the fold digest
  and the task ledger. Older results fold to excerpts and pointers.
- desk: a recomputable read of a resource (a file range, a document section) that the
  agent works from. Older results leave the transcript for the desk
  (app.agents.context_desk), where the resource's current text stays while it is used.
- durable: the result records state or a decision (a publication, a transition, a
  message, a journal note). It is never cut mechanically; only a model summary of a
  completed work segment, or of the whole history, replaces it.

Tools that declare nothing are durable, so a new tool is never folded by mistake.
"""
from __future__ import annotations

RECOMPUTABLE, DURABLE, DESK = 'recomputable', 'durable', 'desk'
_POLICIES: dict[str, str] = {}


def declare(tool, policy: str):
    """Record the policy on the tool (metadata) and by its name, which is all a stored
    history has. Returns the tool, so it can wrap a definition."""
    if policy not in (RECOMPUTABLE, DURABLE, DESK):
        raise ValueError(f'Unknown context policy {policy!r}')
    tool.metadata = {**(tool.metadata or {}), 'context_policy': policy}
    _POLICIES[tool.name] = policy
    return tool


_loaded = False


def _load_builtin() -> None:
    """Built-in tools declare their policy when they are built. A stored history can be
    folded before any agent built them (a restarted process, a test), so build the
    workspace-bound ones once without roots; this registers their names only."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import app.agents.tools.development, app.agents.tools.browser_verify  # noqa: F401
    import app.agents.tools.run_project_check, app.agents.tools.run_service_check  # noqa: F401
    import app.agents.tools.recall_context, app.agents.tools.current_utc_time  # noqa: F401
    import app.collaboration.tools  # noqa: F401
    from app.agents.tools.file_management import make_file_management
    from app.agents.tools.list_project_files import make_list_project_files
    from app.agents.tools.read_project_file import make_read_project_file
    make_file_management((), ()), make_list_project_files(()), make_read_project_file(())


def canonical(name: str, args: dict | None = None) -> str:
    """The tool name a stored history is judged by: the Leader's dispatcher names the
    action in its arguments."""
    if name == 'collaborate':
        action = str((args or {}).get('action', ''))
        return next((n for n in (f'collaboration_{action}', action) if n in _POLICIES), name)
    return name


def policy_of(name: str, args: dict | None = None) -> str:
    if not _loaded:
        _load_builtin()
    return _POLICIES.get(canonical(name, args), DURABLE)


def recomputable(name: str, args: dict | None = None) -> bool:
    return policy_of(name, args) in (RECOMPUTABLE, DESK)


def desk(name: str, args: dict | None = None) -> bool:
    return policy_of(name, args) == DESK
