"""How a tool's results may be compacted, declared on the tool itself.

- recomputable: the result can be obtained again (a read, a search, a listing, a
  status, a check that can be re-run) or its decision-relevant facts are kept by the
  fold digest and the task ledger. Older results fold to excerpts and pointers.
- durable: the result records state or a decision (a publication, a transition, a
  message, a journal note). It is never cut mechanically; only a model summary of a
  completed work segment, or of the whole history, replaces it.

Tools that declare nothing are durable, so a new tool is never folded by mistake.
"""
from __future__ import annotations

RECOMPUTABLE, DURABLE = 'recomputable', 'durable'
_POLICIES: dict[str, str] = {}


def declare(tool, policy: str):
    """Record the policy on the tool (metadata) and by its name, which is all a stored
    history has. Returns the tool, so it can wrap a definition."""
    if policy not in (RECOMPUTABLE, DURABLE):
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


def policy_of(name: str, args: dict | None = None) -> str:
    if not _loaded:
        _load_builtin()
    if name == 'collaborate':
        # The Leader's dispatcher names the action in its arguments.
        action = str((args or {}).get('action', ''))
        name = next((n for n in (f'collaboration_{action}', action) if n in _POLICIES), name)
    return _POLICIES.get(name, DURABLE)


def recomputable(name: str, args: dict | None = None) -> bool:
    return policy_of(name, args) == RECOMPUTABLE
