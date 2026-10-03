"""Attribute every model call to its session (conversation), task and purpose.

The scope is a context variable set where a session's work starts (Leader turn,
scheduled task execution); model calls made under it, including memory summaries
and the command reviewer, inherit it without extra plumbing.
"""
from contextvars import ContextVar
import logging

logger = logging.getLogger(__name__)

usage_scope: ContextVar[dict | None] = ContextVar('usage_scope', default=None)


def current_scope() -> dict:
    scope = dict(usage_scope.get() or {})
    try:
        from app.collaboration.tools import active
        ctx = active.get()
    except Exception:
        ctx = None
    if ctx:
        scope.setdefault('project_id', ctx.project)
        if ctx.conversation:
            scope.setdefault('conversation_id', ctx.conversation)
        if ctx.request:
            scope.setdefault('request_id', ctx.request)
    return scope


def response_usage(response) -> dict:
    """Provider usage of one response: input, cached, output and timing metadata."""
    usage = getattr(response, 'usage_metadata', None) or {}
    metadata = getattr(response, 'response_metadata', None) or {}
    fallback = metadata.get('token_usage') if isinstance(metadata, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    cached = (usage.get('input_token_details') or {}).get('cache_read')
    if cached is None:
        cached = (fallback.get('prompt_tokens_details') or {}).get('cached_tokens')
    return {
        'input_tokens': usage.get('input_tokens', fallback.get('prompt_tokens')),
        'output_tokens': usage.get('output_tokens', fallback.get('completion_tokens')),
        'total_tokens': usage.get('total_tokens', fallback.get('total_tokens')),
        'cached_tokens': cached,
        'model': (metadata.get('model_name') or metadata.get('model')) if isinstance(metadata, dict) else None,
        'latency_ms': metadata.get('wdrunge_latency_ms') if isinstance(metadata, dict) else None,
        'retries': metadata.get('wdrunge_retries') if isinstance(metadata, dict) else None,
    }


async def record_call(store, agent_id: str, response, purpose: str) -> None:
    """Record a model call made outside an Agent (reviewer, title model)."""
    if store is None:
        logger.warning('No store to record %s %s usage', agent_id, purpose)
        return
    usage = response_usage(response)
    if not isinstance(usage['total_tokens'], int):
        logger.warning('%s %s call reported no token usage (%s); not recorded', agent_id, purpose,
                       sorted((getattr(response, 'response_metadata', None) or {}).keys()))
        return
    cached = usage['cached_tokens'] if isinstance(usage['cached_tokens'], int) else None
    await store.record_agent_usage(agent_id, usage['total_tokens'], usage['input_tokens'] or 0, cached,
                                   purpose=purpose, **{k: usage[k] for k in ('output_tokens', 'model', 'latency_ms', 'retries')},
                                   **current_scope())
