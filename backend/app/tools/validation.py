"""Strict schemas and recoverable agent-facing tool errors."""
import logging
from langchain_core.tools import tool as langchain_tool
from pydantic import ConfigDict
from langgraph.prebuilt.tool_node import ToolInvocationError

logger = logging.getLogger(__name__)


def tool(*args, **kwargs):
    def strict(result):
        result.args_schema.model_config = ConfigDict(**{**result.args_schema.model_config, 'strict': True, 'extra': 'forbid'})
        result.args_schema.model_rebuild(force=True)
        return result
    result = langchain_tool(*args, **kwargs)
    if hasattr(result, 'args_schema'):
        return strict(result)
    return lambda fn: strict(result(fn))


def tool_error(exc: Exception) -> str:
    # Cancellation is BaseException and must propagate so stopping/reset works.
    if not isinstance(exc, (ValueError, LookupError, OSError, TypeError, RuntimeError, ToolInvocationError)):
        logger.error('Tool execution failed', exc_info=(type(exc), exc, exc.__traceback__))
    return f'Tool error ({type(exc).__name__}): {str(exc)[:2500]}. Inspect the arguments and current state before retrying; do not report this operation as completed.'
