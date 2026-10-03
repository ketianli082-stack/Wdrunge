"""Reissue current goal state at invocation time, including after context cleaning.

These messages are not saved into model history and never acknowledge inbox events.
"""
from app.agents.memory import reminder
from app.collaboration.tools import active


async def goal_messages():
    ctx = active.get()
    if not ctx:
        return []
    content = await ctx.service.goal_reminder(ctx.project, ctx.conversation, ctx.request)
    return [reminder(content)] if content else []


def with_goal_state(messages, goal):
    """Store the goal reminder in the context only when it changed, instead of appending
    it to every call: the history stays append-only and cacheable. Older copies are
    stubbed by context folding; summaries drop them and the next call re-adds one."""
    from langchain_core.messages import HumanMessage
    if not goal:
        return list(messages)
    content = goal[0].content
    latest = next((m for m in reversed(messages) if m.additional_kwargs.get('goal_state')), None)
    if latest is not None and latest.content == content:
        return list(messages)
    return [*messages, HumanMessage(content=content, additional_kwargs={'reissuable_reminder': True, 'goal_state': True})]
