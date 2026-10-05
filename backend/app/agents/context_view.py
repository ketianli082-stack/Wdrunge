"""Read-only view of each agent's model-facing context in one conversation.

A context is what the agent's next model call would receive: the Leader's session for
the conversation, a worker's direct thread, or one attempt of a collaboration task.
It is the compacted history (folded results stay folded); recall_context still
returns originals.
"""
from __future__ import annotations

import hashlib
import json

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, messages_from_dict


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(part if isinstance(part, str) else str(part.get('text', '')) if isinstance(part, dict) else str(part)
                       for part in content)
    return json.dumps(content, ensure_ascii=False, default=str)


def message_entry(message: BaseMessage) -> dict:
    entry = {'id': message.id, 'type': message.type, 'content': _text(message.content)}
    if isinstance(message, AIMessage):
        entry['tool_calls'] = [
            *({'id': call.get('id'), 'name': call['name'], 'arguments': json.dumps(call.get('args', {}), ensure_ascii=False, default=str)}
              for call in message.tool_calls),
            *({'id': call.get('id'), 'name': call.get('name') or 'invalid', 'arguments': str(call.get('args') or ''), 'invalid': True}
              for call in message.invalid_tool_calls),
        ]
        reasoning = message.additional_kwargs.get('reasoning_content')
        if isinstance(reasoning, str) and reasoning:
            entry['reasoning'] = reasoning
    elif isinstance(message, ToolMessage):
        entry.update(tool_call_id=message.tool_call_id, name=message.name, status=message.status)
    return entry


def revision_of(entries: list[dict]) -> str:
    return hashlib.sha1(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def list_contexts(team, service, project: str, conversation_id: str) -> list[dict]:
    """Every context of the conversation, Leader first, then newest first."""
    running = bool(getattr(team, '_conversation_tasks', {}).get(conversation_id))
    contexts = []
    store = getattr(team, '_store', None)
    if conversation_id in team.leader_contexts or (store and await store.load_context_memory(f'leader:{conversation_id}')):
        contexts.append({'key': 'leader', 'agent': 'leader', 'label': '协调会话', 'running': running, 'updated': None})
    for role, worker in team.workers.items():
        if conversation_id in worker._threads:
            contexts.append({'key': f'direct:{role}', 'agent': role, 'label': '直接指派', 'thread': worker._threads[conversation_id],
                             'running': running and worker.status == 'running', 'updated': None})
    tasks = []
    if service is not None:
        async with service.reading() as c:
            requests = [r for r in await service._list(c, 'request', project) if r.get('conversation') == conversation_id]
        for request in requests:
            journal = await service.journal(project, request['id'])
            # A resumed execution continues its thread; one context per thread, latest record.
            threads: dict[str, dict] = {}
            for attempt in journal['attempts']:
                if attempt.get('thread'):
                    threads.pop(attempt['thread'], None)
                    threads[attempt['thread']] = attempt
            label = (request.get('summary') or request.get('goal') or request['id']).strip().splitlines()[0][:80]
            for index, (thread, attempt) in enumerate(threads.items()):
                tasks.append({'key': f"task:{request['id']}:{thread}", 'agent': request['recipient'],
                              'label': label if len(threads) == 1 else f"{label} · 第 {index + 1} 段",
                              'thread': thread, 'updated': attempt.get('ended') or attempt.get('started'),
                              'running': request['state'] == 'running' and index == len(threads) - 1 and 'ended' not in attempt})
    tasks.sort(key=lambda item: item['updated'] or 0, reverse=True)
    return [*contexts, *tasks]


async def context_messages(team, context: dict, conversation_id: str) -> list[BaseMessage]:
    if context['key'] == 'leader':
        live = team.leader_contexts.get(conversation_id)
        if live is not None:
            return list(live)
        state = await team._store.load_context_memory(f'leader:{conversation_id}') if team._store else None
        return messages_from_dict(state['messages']) if state else []
    checkpointer = getattr(team, '_checkpointer', None) or team.workers[context['agent']]._checkpointer
    saved = await checkpointer.aget_tuple({'configurable': {'thread_id': context['thread']}})
    return list(((saved.checkpoint.get('channel_values') or {}).get('messages') or []) if saved else [])
