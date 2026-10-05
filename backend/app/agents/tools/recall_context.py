"""Return the original of a folded, superseded or summarized message by its ID."""
import json
from typing import Annotated

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from pydantic import Field

from app.agents.tools.validation import tool, ToolFailure
from app.agents.context_policy import RECOMPUTABLE


async def recall(store, scope: str, message_id: str, offset: int = 0, limit: int = 12000) -> str:
    from app.agents.memory import content_text, recall_original
    message = await recall_original(store, scope, message_id)
    if message is None:
        raise ToolFailure(f'No compacted message {message_id} in this context. Only IDs named by folded, '
                          'superseded or summarized results of your own context can be recalled.',
                          'not_found', 'not_found', 'Use an ID from a fold marker or stub, or re-read the source.',
                          'correct_arguments')
    text = content_text(message)
    if isinstance(message, AIMessage) and message.tool_calls:
        text += '\n[tool calls]\n' + json.dumps(message.tool_calls, ensure_ascii=False)
    excerpt = text[offset:offset + limit]
    end = offset + len(excerpt)
    more = f'; next_offset={end}' if end < len(text) else ''
    return f'[Original {message.type} message {message_id}; characters {len(text)}; showing {offset}-{end}{more}]\n{excerpt}'


def make_recall_context(store_getter):
    @tool(context_policy=RECOMPUTABLE)
    async def recall_context(message_id: Annotated[str, Field(min_length=1, max_length=120)],
                             config: RunnableConfig,
                             offset: Annotated[int, Field(ge=0)] = 0,
                             limit: Annotated[int, Field(ge=1, le=30000)] = 12000) -> str:
        """Return the exact original of a folded, superseded or summarized message from
        your own context, by the message ID its marker names. offset/limit page through
        characters. Prefer this to re-running a tool or re-reading an unchanged source."""
        return await recall(store_getter(), config['configurable']['thread_id'], message_id, offset, limit)
    return recall_context


# Schema for models whose tool calls are dispatched by name (the Leader loop).
recall_context = make_recall_context(lambda: None)
