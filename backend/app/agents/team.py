"""Team registry and leader-coordinated parallel specialist handoffs."""
from contextlib import aclosing

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict, messages_from_dict
from pydantic import ValidationError

from app.agents.memory import ContextMemory, reminder, is_context_overflow
from app.agents.context_meter import calibrate
from app.agents.base import AgentSpec, AgentStatus
from app.collaboration.tools import Context, active, collaborate, COLLABORATION_TOOLS, collaboration_action, model_collaboration_tools
from app.collaboration.context import encode_handoff, coordination_update
from app.agents.leader import LeaderAgent
from app.agents.identity import member_name
from app.agents.worker import WorkerAgent
from app.agents.tools.current_utc_time import current_utc_time
from app.agents.tools.checkpoint_context import ContextCheckpoint, checkpoint_context
from app.agents.tools.recall_context import recall, recall_context
from app.agents.tools.delegate_task import Delegation, delegate_task
from app.agents.tools.delegate_tasks import DelegationBatch, delegate_tasks
from app.agents.tools.list_project_files import make_list_project_files
from app.agents.tools.read_project_file import make_read_project_file
from app.agents.tools.refresh_api_contract import refresh_api_contract
from app.agents.tools.refresh_frontend_client import refresh_frontend_client
from app.agents.tools.run_project_check import run_project_check
from app.agents.tools.run_service_check import run_service_check
from app.agents.tools.write_project_file import make_write_project_file
from app.agents.tools._workspace import active_workspace, workspace_root
from app.agents.tools.development import development_tools
from app.agents.tools.file_management import make_file_management
from app.agents.tools.browser_verify import browser_verify
from app.agents.tools.validation import exception_result, validation_failure, failure_result, normalize_failure, folded_call_failure

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from app.storage import ChatStore


ROLE_CONFIG = {
    "product": {
        "name": member_name("product"),
        "prompt": "product.md",
        "skills": ("requirements.md",),
        "read": ("docs", "README.md", "backend/openapi.json"),
        "write": ("docs/product",),
    },
    "backend": {
        "name": member_name("backend"),
        "prompt": "backend.md",
        "skills": ("api_contract.md",),
        "read": ("docs", "backend/app", "backend/pyproject.toml", "backend/tests"),
        "write": ("backend/app", "docs/backend"),
    },
    "designer": {
        "name": member_name("designer"),
        "prompt": "designer.md",
        "skills": ("visual_design.md",),
        "read": ("docs", "frontend/src", "frontend/public"),
        "write": ("frontend/public", "docs/design"),
    },
    "frontend": {
        "name": member_name("frontend"),
        "prompt": "frontend.md",
        "skills": ("api_contract.md",),
        "read": ("docs", "frontend/src", "frontend/public", "backend/openapi.json"),
        "write": ("frontend/src", "docs/frontend"),
    },
    "testing": {
        "name": member_name("testing"),
        "prompt": "testing.md",
        "skills": ("test_strategy.md",),
        "read": ("docs", "backend/app", "backend/tests", "frontend/src", "backend/openapi.json"),
        "write": ("backend/tests", "frontend/tests", "docs/testing"),
    },
}

WORKER_ACK = {
    "product": "收到，我先整理产品需求。",
    "backend": "收到，我马上开始后端开发。",
    "designer": "收到，我来处理视觉设计。",
    "frontend": "收到，我马上开始前端开发。",
    "testing": "收到，我来检查和测试。",
}

MAX_DELEGATIONS = 12
logger = logging.getLogger(__name__)


@dataclass
class LeaderCheckpoint:
    handoff: ContextCheckpoint
    history_cutoff: int


def message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(
        part.get("text", "") for part in message.content if isinstance(part, dict)
    )


class Team:
    def __init__(self):
        self.leader = LeaderAgent(
            AgentSpec(
                id="leader", name=member_name("leader"), prompt_file="leader.md", tools=(delegate_task, delegate_tasks, checkpoint_context)
            )
        )
        self.workers: dict[str, WorkerAgent] = {}
        for role, config in ROLE_CONFIG.items():
            read_roots = config["read"]
            write_roots = config["write"]
            tools = (
                make_list_project_files(read_roots),
                make_read_project_file(read_roots),
                make_write_project_file(write_roots),
            )
            tools += make_file_management(read_roots, write_roots)
            if role in {'backend', 'frontend', 'testing'}:
                tools += development_tools
            if role in {'frontend', 'testing'}:
                tools += (browser_verify,)
            if role == "testing":
                tools += (run_project_check, run_service_check,)
            if role == "backend":
                tools += (refresh_api_contract,)
            if role == "frontend":
                tools += (refresh_frontend_client,)
            if role == "product":
                tools += (current_utc_time,)
            worker = WorkerAgent(
                AgentSpec(
                    id=role,
                    name=config["name"],
                    prompt_file=config["prompt"],
                    tools=tools,
                    skills=config["skills"],
                )
            )
            worker.system_prompt()  # Validate prompt and skill files during startup.
            self.workers[role] = worker
        self.leader.system_prompt()
        self._lock = asyncio.Lock()
        self._leader_checkpoints: dict[str, LeaderCheckpoint] = {}
        self._store: ChatStore | None = None
        self.collaboration = None
        self._pending_leader_receipts = {}
        self._context_memories = {}
        # The Leader's current model-facing messages per conversation, for inspection;
        # the same list the running turn mutates, kept after the turn ends.
        self.leader_contexts: dict[str, list[BaseMessage]] = {}

    async def configure_persistence(
        self, store: "ChatStore", checkpointer: "BaseCheckpointSaver"
    ) -> None:
        self._store = store
        self._checkpointer = checkpointer
        self.leader._store = store
        totals = await store.load_agent_usage()
        cache = await store.load_agent_cache_usage()
        for agent in (self.leader, *self.workers.values()):
            agent.total_tokens = totals.get(agent.spec.id, 0)
            agent.cache_input_tokens, agent.cached_tokens = cache.get(agent.spec.id, (0, 0))
        self._leader_checkpoints.clear()
        self._context_memories.clear()
        self.leader_contexts.clear()
        for row in await store.load_leader_checkpoints():
            self._leader_checkpoints[row["conversation_id"]] = LeaderCheckpoint(
                handoff=ContextCheckpoint.model_validate(row["handoff_json"]),
                history_cutoff=row["history_cutoff"],
            )
        for worker in self.workers.values():
            await worker.configure_persistence(store, checkpointer)

    def clear_runtime_memory(self):
        """Called only after all coordinator and scheduler tasks have stopped."""
        self._leader_checkpoints.clear()
        self._context_memories.clear()
        self.leader_contexts.clear()
        self._pending_leader_receipts.clear()
        for agent in (self.leader, *self.workers.values()):
            agent.total_tokens = 0
            agent.context_tokens = 0
            agent.cache_input_tokens = agent.cached_tokens = 0
            agent.status = AgentStatus.IDLE
        for worker in self.workers.values():
            worker._threads.clear()
            worker._context_by_session.clear()
            worker._graph = None

    def snapshot(self) -> list[dict]:
        items = [self.leader.snapshot(), *(worker.snapshot() for worker in self.workers.values())]
        scheduler = getattr(self, 'scheduler', None)
        if scheduler:
            for item in items:
                if item['id'] != 'leader':
                    item['status'] = 'running' if item['id'] in scheduler.running_roles else 'idle'
        return items

    def usage_snapshot(self) -> dict:
        members = [self.leader.usage_snapshot(), *(worker.usage_snapshot() for worker in self.workers.values())]
        scheduler = getattr(self, 'scheduler', None)
        if scheduler:
            for member in members:
                live = [worker for worker in scheduler.active_workers.values() if worker.spec.id == member['id']]
                if live:
                    member['total_tokens'] += sum(worker.total_tokens for worker in live)
                    member['cache_input_tokens'] += sum(worker.cache_input_tokens for worker in live)
                    member['cached_tokens'] += sum(worker.cached_tokens for worker in live)
                    # Each instance has its own context window; adding contexts
                    # would invent an occupancy that no model actually sees.
                    member['context_tokens'] = max(worker.context_tokens for worker in live)
                    member['context_percent'] = round(member['context_tokens'] / member['context_window'] * 100, 1)
        cache_input = sum(member["cache_input_tokens"] for member in members)
        cached = sum(member["cached_tokens"] for member in members)
        return {"total_tokens": sum(member["total_tokens"] for member in members),
                "cache_input_tokens": cache_input, "cached_tokens": cached,
                "cache_percent": round(cached * 100 / cache_input, 1) if cache_input else None,
                "agents": members}

    async def cancel_conversation(self, conversation_id):
        tasks = list(getattr(self, '_conversation_tasks', {}).get(conversation_id, set()))
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stream(self, history, *, conversation_id=None, workspace_path=None, wake_only=False, generation=None):
        session_id = conversation_id or uuid4().hex
        from app.agents.usage import usage_scope
        # Every model call of this turn (Leader, delegated workers, summaries, reviews)
        # is attributed to this session.
        usage_scope.set({**(usage_scope.get() or {}), 'conversation_id': session_id})
        if not hasattr(self, '_conversation_tasks'):
            self._conversation_tasks = {}
        task = asyncio.current_task()
        self._conversation_tasks.setdefault(session_id, set()).add(task)
        try:
            if self.collaboration:
                project = await self.collaboration.project_for_conversation(session_id)
                if project and await self.collaboration.conversation_stopped(project, session_id, generation):
                    return
                if project and generation is None:
                    async with self.collaboration.reading() as c:
                        control = await self.collaboration._get(c, 'conversation_run', project, session_id, False)
                        generation = control['generation'] if control else None
            async with aclosing(self._stream(history, conversation_id=session_id, workspace_path=workspace_path, wake_only=wake_only, generation=generation)) as events:
                async for event in events:
                    yield event
        finally:
            self._conversation_tasks[session_id].discard(task)
            if not self._conversation_tasks[session_id]:
                del self._conversation_tasks[session_id]

    async def _stream(
        self, history: list[BaseMessage], *, conversation_id: str | None = None,
        workspace_path: Path | None = None, wake_only: bool = False, generation: str | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Let the Leader reply or function-call specialists as needed."""
        session_id = conversation_id or uuid4().hex
        async with self._lock:
            token = active_workspace.set(workspace_path.resolve() if workspace_path else None)
            collaboration_token = None
            try:
                if self.collaboration:
                    project = await self.collaboration.project_for_conversation(session_id)
                    if project:
                        if await self.collaboration.conversation_stopped(project, session_id, generation):
                            return
                        if wake_only and not await self.collaboration.inbox(project, 'leader', f'leader:{session_id}', session_id):
                            return
                        await self.collaboration.leader_session(project, session_id, state='running', last_wake=time.time(), retry_after=0, **({'failures': 0} if not wake_only else {}))
                        context = Context(self.collaboration, project, 'leader', session_id, f'leader:{session_id}')
                        collaboration_token = active.set(context)
                async for event in self._run(history, session_id):
                    yield event
            finally:
                try:
                    if collaboration_token is not None:
                        ctx = active.get()
                        state = 'failed' if self.leader.status == AgentStatus.FAILED else await self.collaboration.leader_rest_state(ctx.project, session_id)
                        await self.collaboration.leader_session(ctx.project, session_id, state=state)
                        self.leader.status = AgentStatus(state)
                finally:
                    from app.execution.processes import processes
                    await processes.close(workspace=workspace_root().resolve())
                    if collaboration_token is not None:
                        active.reset(collaboration_token)
                    active_workspace.reset(token)
                    for agent in (self.leader, *self.workers.values()):
                        if agent.status == AgentStatus.RUNNING:
                            agent.status = AgentStatus.IDLE

    async def _run(self, history: list[BaseMessage], conversation_id: str) -> AsyncIterator[tuple[str, dict]]:
        self._pending_leader_receipts.pop(conversation_id, None)
        self.leader.status = AgentStatus.RUNNING
        yield "status", {"id": "leader", "status": AgentStatus.RUNNING}
        system = SystemMessage(content=f"{self.leader.full_system_prompt(collaborative=active.get() is not None)}\n\nCurrent project workspace: {workspace_root().resolve()}. Assignments and project files must stay in this workspace.")
        scope = f"leader:{conversation_id}"
        memory = ContextMemory(self.leader, scope, [delegate_task, delegate_tasks, checkpoint_context, recall_context, *model_collaboration_tools()])
        saved_memory = self._context_memories.get(scope)
        if saved_memory is None and self._store:
            saved_memory = await self._store.load_context_memory(scope)
        checkpoint = self._leader_checkpoints.get(conversation_id)
        if saved_memory and saved_memory['history_cutoff'] <= len(history):
            # Continue the Leader's own session: its earlier tool calls, results and
            # reasoning stay, and the prefix stays cacheable. Its last reply is already in
            # the saved view, so skip the stored copy in the chat history.
            tail = history[saved_memory['history_cutoff']:]
            if tail and isinstance(tail[0], AIMessage) and saved_memory.get('reply') and message_text(tail[0]) == saved_memory['reply']:
                tail = tail[1:]
            messages = [system, *messages_from_dict(saved_memory['messages']), *tail]
        elif checkpoint and checkpoint.history_cutoff <= len(history):
            messages: list[BaseMessage] = [
                system,
                memory.summary_message(checkpoint.handoff.as_prompt()),
                *history[checkpoint.history_cutoff:],
            ]
        else:
            messages = [system, *history]
        snapshot = None
        if active.get():
            ctx = active.get()
            previous = (saved_memory or {}).get('snapshot') if saved_memory and saved_memory['history_cutoff'] <= len(history) else None
            text, snapshot = coordination_update(await ctx.service.handoff(ctx.project, 'leader'), previous)
            messages.append(HumanMessage(content=text, additional_kwargs={'coordination_state': True}))
        self.leader_contexts[conversation_id] = messages

        async def save_memory(current, reply=None):
            self.leader_contexts[conversation_id] = current
            state = {'history_cutoff': len(history), 'messages': messages_to_dict(current[1:]), 'snapshot': snapshot}
            if reply is not None:
                state['reply'] = reply
            elif (self._context_memories.get(scope) or {}).get('reply'):
                state['reply'] = self._context_memories[scope]['reply']
            self._context_memories[scope] = state
            if self._store:
                await self._store.save_context_memory(scope, state)

        async def prepare(current, *, force_clean=False):
            result = await memory.prepare(current, force_clean=force_clean)
            if result.changed:
                await save_memory(result.messages)
            return result.messages

        from app.collaboration.reminders import goal_messages, with_goal_state

        async def record_tool_results(current):
            # Raw results, before context folding may shorten them.
            if not self._store:
                return
            calls = {call['id']: call for m in current if isinstance(m, AIMessage) for call in m.tool_calls if call.get('id')}
            rows = []
            for m in current:
                if isinstance(m, ToolMessage) and m.tool_call_id in calls:
                    call = calls[m.tool_call_id]
                    rows.append({'id': m.tool_call_id, 'tool': call['name'], 'status': 'error' if m.status == 'error' else 'success',
                                 'arguments': json.dumps(call.get('args', {}), ensure_ascii=False, default=str),
                                 'result': m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)})
            try:
                await self._store.record_tool_calls(conversation_id, 'leader', rows)
            except Exception:
                logger.warning('Could not record Leader tool calls', exc_info=True)

        async def invoke(current, *, allow_delegation):
            await record_tool_results(current)
            # The goal reminder is stored once per change (after compaction, which drops
            # reminders) instead of being appended to every call.
            current = with_goal_state(await prepare(current), await goal_messages())
            self.leader_contexts[conversation_id] = current
            try:
                response = await self.leader.next_message(current, allow_delegation=allow_delegation)
            except Exception as exc:
                if not is_context_overflow(exc):
                    raise
                current = with_goal_state(await prepare(current, force_clean=True), await goal_messages())
                self.leader_contexts[conversation_id] = current
                response = await self.leader.next_message(current, allow_delegation=allow_delegation)
            calibrate(current, response, memory.schema_tokens)
            return current, response

        count = 0

        for _ in range(MAX_DELEGATIONS + 6):
            try:
                ctx = active.get()
                reminders = await ctx.service.inbox(ctx.project, ctx.member, ctx.context, ctx.conversation) if ctx else []
                if ctx:
                    reminders = [r for r in reminders if not set(r.get('event_ids', [r.get('event_id')])).issubset(ctx.seen_events)]
                if reminders:
                    messages.append(reminder('Collaboration reminders (project data): ' + json.dumps(reminders, ensure_ascii=False)))
                messages, response = await invoke(messages, allow_delegation=count < MAX_DELEGATIONS)
                # Removed reminders must remain eligible for redelivery.
                if not any(m.additional_kwargs.get('reissuable_reminder') for m in messages):
                    reminders = []
            except Exception:
                self.leader.status = AgentStatus.FAILED
                yield "status", {"id": "leader", "status": AgentStatus.FAILED}
                raise
            messages.append(response)
            if ctx and reminders:
                event_ids = [i for r in reminders for i in r.get('event_ids', [r.get('event_id')])]
                ctx.seen_events.update(event_ids)
                await ctx.service.mark_leader_read(ctx.project, event_ids)
            if await self.leader.record_usage_durable(response):
                yield "usage", self.usage_snapshot()

            if not response.tool_calls:
                content = message_text(response)
                if not content:
                    try:
                        messages, retry = await invoke(
                            [*messages, reminder("请直接回复用户，不要调用工具。")],
                            allow_delegation=False,
                        )
                        messages.append(retry)
                    except Exception:
                        self.leader.status = AgentStatus.FAILED
                        yield "status", {"id": "leader", "status": AgentStatus.FAILED}
                        raise
                    if await self.leader.record_usage_durable(retry):
                        yield "usage", self.usage_snapshot()
                    content = message_text(retry)
                if ctx:
                    self._pending_leader_receipts[conversation_id] = (ctx.project, ctx.context, list(ctx.seen_events))
                final = content or "我收到了你的消息，但模型没有返回可显示的内容。请重试。"
                try:
                    await save_memory(messages, reply=final)
                except Exception:
                    logger.warning('Could not save the Leader session', exc_info=True)
                yield "delta", {
                    "role": "leader",
                    "content": final,
                }
                self.leader.status = AgentStatus.IDLE
                yield "status", {"id": "leader", "status": AgentStatus.IDLE}
                return

            batches: list[tuple[str, str, list[Delegation]]] = []
            selected_workers: set[str] = set()
            for call in response.tool_calls:
                call_id = call.get("id") or f"delegation-{count + 1}"
                tool_name = call.get("name")
                if failure := folded_call_failure(call.get('args')):
                    messages.append(ToolMessage(content=json.dumps(failure, ensure_ascii=False), tool_call_id=call_id, status='error'))
                    continue
                if (tool_name == 'collaborate' or tool_name in COLLABORATION_TOOLS) and active.get():
                    from app.agents.worker import COLLABORATION_LABELS
                    action = collaboration_action(tool_name, call.get('args', {}))
                    activity = {'id': uuid4().hex, 'agent': 'leader', 'tool': tool_name,
                                'description': COLLABORATION_LABELS.get(action, '协调团队工作')}
                    yield 'activity', {**activity, 'phase': 'running'}
                    try:
                        candidate = collaborate if tool_name == 'collaborate' else COLLABORATION_TOOLS[tool_name]
                        result = await candidate.ainvoke(call.get('args', {}))
                    except Exception as exc:
                        result = exception_result(exc, tool_name=tool_name)
                    result = normalize_failure(result, tool_name=tool_name)
                    yield 'activity', {**activity, 'phase': 'failed' if isinstance(result, dict) and result.get('error') else 'completed'}
                    messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id,
                                                status='error' if result.get('error') else 'success'))
                    continue
                if tool_name == "checkpoint_context":
                    if len(response.tool_calls) != 1:
                        messages.append(ToolMessage(content=json.dumps(failure_result('Call checkpoint_context alone, then delegate work',
                            'invalid_arguments', 'checkpoint_must_be_alone', 'Call checkpoint_context in a separate tool round.',
                            'correct_arguments')), tool_call_id=call_id, status='error'))
                        continue
                    try:
                        reset_handoff = ContextCheckpoint.model_validate(call.get("args", {}))
                    except ValidationError as error:
                        messages.append(ToolMessage(content=json.dumps(validation_failure(error,
                            tool_name=tool_name, schema=ContextCheckpoint), ensure_ascii=False), tool_call_id=call_id, status='error'))
                        continue
                    self._leader_checkpoints[conversation_id] = LeaderCheckpoint(
                        handoff=reset_handoff,
                        history_cutoff=len(history),
                    )
                    if active.get():
                        ctx = active.get()
                        await ctx.service.handoff(ctx.project, 'leader', save=True, summary=reset_handoff.goal)
                    if self._store:
                        await self._store.save_leader_checkpoint(
                            conversation_id,
                            len(history),
                            reset_handoff.model_dump(mode="json"),
                        )
                    tool_result = ToolMessage(content="Coordination checkpoint saved. Continue from the handoff.", tool_call_id=call_id)
                    prepared = await memory.checkpoint([*messages, tool_result], reset_handoff.as_prompt())
                    messages = prepared.messages
                    await save_memory(messages)
                    yield "activity", {
                        "id": uuid4().hex,
                        "agent": "leader",
                        "tool": "checkpoint_context",
                        "description": "保存阶段交接并开启干净的协调上下文",
                        "phase": "completed",
                    }
                    continue
                if tool_name == "enable_tools":
                    from app.collaboration.tools import enable_tools
                    try:
                        content, status = json.dumps(await enable_tools.ainvoke(call.get('args', {})), ensure_ascii=False), 'success'
                    except Exception as exc:
                        content, status = json.dumps(exception_result(exc, tool_name=tool_name), ensure_ascii=False), 'error'
                    messages.append(ToolMessage(content=content, tool_call_id=call_id, status=status))
                    continue
                if tool_name == "recall_context":
                    activity = {'id': uuid4().hex, 'agent': 'leader', 'tool': tool_name, 'description': '找回已折叠的上下文'}
                    yield 'activity', {**activity, 'phase': 'running'}
                    try:
                        args = recall_context.args_schema.model_validate(call.get('args', {}))
                        content, status = await recall(self._store, scope, args.message_id, args.offset, args.limit), 'success'
                    except Exception as exc:
                        content, status = json.dumps(exception_result(exc, tool_name=tool_name), ensure_ascii=False), 'error'
                    yield 'activity', {**activity, 'phase': 'failed' if status == 'error' else 'completed'}
                    messages.append(ToolMessage(content=content, tool_call_id=call_id, status=status))
                    continue
                if tool_name not in {"delegate_task", "delegate_tasks"}:
                    messages.append(ToolMessage(content="Unknown tool call", tool_call_id=call_id))
                    continue
                try:
                    args = call.get("args", {})
                    assignments = (
                        [Delegation.model_validate(args)]
                        if tool_name == "delegate_task"
                        else DelegationBatch.model_validate(args).assignments
                    )
                except ValidationError as error:
                    messages.append(
                        ToolMessage(content=json.dumps(validation_failure(error, tool_name=tool_name,
                            schema=Delegation if tool_name == 'delegate_task' else DelegationBatch), ensure_ascii=False),
                            tool_call_id=call_id, status='error')
                    )
                    continue
                if count + sum(len(group) for _, _, group in batches) + len(assignments) > MAX_DELEGATIONS:
                    messages.append(
                        ToolMessage(content="Delegation limit reached", tool_call_id=call_id)
                    )
                    continue
                workers = {assignment.worker for assignment in assignments}
                if workers & selected_workers:
                    messages.append(
                        ToolMessage(content="A worker cannot receive concurrent assignments; assign it again after this batch", tool_call_id=call_id)
                    )
                    continue
                selected_workers.update(workers)
                batches.append((call_id, tool_name, assignments))

            if not batches:
                continue

            message_id = uuid4().hex
            jobs: list[tuple[str, str, Delegation, str]] = []
            mentions: list[dict[str, str]] = []
            for call_id, tool_name, assignments in batches:
                for assignment in assignments:
                    count += 1
                    worker = self.workers[assignment.worker]
                    event_id = uuid4().hex
                    jobs.append((call_id, tool_name, assignment, event_id))
                    mentions.append({"worker": assignment.worker, "name": worker.spec.name, "summary": assignment.summary})

            yield "chat_message", {
                "id": message_id,
                "sender": "leader",
                "content": "\n".join(f"@{item['name']} {item['summary']}" for item in mentions),
                "mentions": mentions,
            }
            for _, tool_name, assignment, event_id in jobs:
                worker = self.workers[assignment.worker]
                yield "activity", {
                    "id": event_id,
                    "agent": "leader",
                    "tool": tool_name,
                    "description": f"将工作交给{worker.spec.name}",
                    "phase": "running",
                }
                worker.status = AgentStatus.RUNNING
                yield "status", {"id": assignment.worker, "status": AgentStatus.RUNNING}

            queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
            tasks = [
                asyncio.create_task(
                    self._run_assignment(call_id, tool_name, assignment, event_id, message_id, conversation_id, queue)
                )
                for call_id, tool_name, assignment, event_id in jobs
            ]
            results: dict[tuple[str, str], str] = {}
            failures: list[Exception] = []
            remaining = len(tasks)
            try:
                while remaining:
                    event, payload = await queue.get()
                    if event == "_finished":
                        remaining -= 1
                        if payload["error"] is not None:
                            failures.append(payload["error"])
                        else:
                            results[(payload["call_id"], payload["worker"])] = payload["output"]
                    else:
                        yield event, payload
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            if failures:
                raise failures[0]
            for call_id, _, assignments in batches:
                lines = [
                    f"{assignment.worker}: {results[(call_id, assignment.worker)] or 'Specialist completed without text output'}"
                    for assignment in assignments
                ]
                messages.append(ToolMessage(content="\n\n".join(lines)[-12000:], tool_call_id=call_id))

        self.leader.status = AgentStatus.FAILED
        yield "status", {"id": "leader", "status": AgentStatus.FAILED}
        raise RuntimeError("Leader exceeded the coordination turn limit")

    async def acknowledge_leader(self, conversation_id):
        pending = self._pending_leader_receipts.pop(conversation_id, None)
        if pending and self.collaboration:
            project, context, event_ids = pending
            await self.collaboration.acknowledge(project, 'leader', context, event_ids)

    async def _run_assignment(
        self,
        call_id: str,
        tool_name: str,
        assignment: Delegation,
        event_id: str,
        message_id: str,
        conversation_id: str,
        queue: asyncio.Queue[tuple[str, dict]],
    ) -> None:
        worker = self.workers[assignment.worker]
        output: list[str] = []
        error: Exception | None = None
        context_label = "上下文准备中"
        try:
            ctx = active.get()
            if ctx:
                request = await ctx.service.create_request(ctx.project, 'leader', assignment.worker,
                    assignment.task + '\nHandoff: ' + assignment.handoff, event_id, conversation=conversation_id, summary=assignment.summary, origin_message=message_id)
                output.append(f"Durable request {request['id']} is queued. Delegation returns immediately; it is NOT completed. The coordinator will wake on messages, blockers or delivery. Handle any other ready decisions now, then give a short update and suspend; do not poll or re-create this task.")
                await queue.put(("activity", {"id": event_id, "agent": "leader", "tool": tool_name, "description": f"已异步指派给{worker.spec.name}，等待消息或交付", "phase": "completed"}))
                await queue.put(("_finished", {"call_id": call_id, "worker": assignment.worker, "output": ''.join(output), "error": None}))
                return
            await queue.put(("read_receipt", {"message_id": message_id, "reader": assignment.worker}))
            await queue.put(("chat_message", {
                "id": f"{event_id}-ack",
                "sender": assignment.worker,
                "content": WORKER_ACK[assignment.worker],
            }))
            async for event, payload in worker.stream(
                assignment.task,
                conversation_id=conversation_id,
                context_mode=assignment.context_mode,
                handoff=f"Project workspace: {workspace_root().resolve()}\n{assignment.handoff}",
            ):
                if event == "content":
                    output.append(payload["content"])
                elif event == "context":
                    context_label = "干净上下文" if payload["mode"] == "fresh" else "续接上下文"
                    await queue.put(("activity", {
                        "id": event_id,
                        "agent": "leader",
                        "tool": tool_name,
                        "description": f"将工作交给{worker.spec.name} · {context_label}",
                        "phase": "running",
                    }))
                elif event == "activity":
                    await queue.put(("activity", {"agent": assignment.worker, **payload}))
                elif event == "usage":
                    await queue.put(("usage", self.usage_snapshot()))
        except Exception as cause:
            error = cause
            try:
                await worker.discard_session(conversation_id)
            except Exception:
                # The original worker error must still reach the queue consumer.
                logger.exception("Failed to discard worker session after assignment error")
            worker.status = AgentStatus.FAILED
            await queue.put(("status", {"id": assignment.worker, "status": AgentStatus.FAILED}))
        else:
            worker.status = AgentStatus.IDLE
            await queue.put(("status", {"id": assignment.worker, "status": AgentStatus.IDLE}))
            await queue.put(("message_reaction", {
                "message_id": message_id,
                "worker": assignment.worker,
                "reaction": "done",
            }))
        await queue.put(("activity", {
            "id": event_id,
            "agent": "leader",
            "tool": tool_name,
            "description": f"将工作交给{worker.spec.name} · {context_label}",
            "phase": "failed" if error else "completed",
        }))
        await queue.put(("_finished", {
            "call_id": call_id,
            "worker": assignment.worker,
            "output": "".join(output)[-12000:],
            "error": error,
        }))


@lru_cache(maxsize=1)
def get_team() -> Team:
    return Team()
