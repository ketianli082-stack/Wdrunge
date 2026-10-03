"""Bounded background execution; blocked jobs release their execution slots."""
import asyncio
import logging
import time
from uuid import uuid4
from app.storage import StorageConflictError
from app.agents.tools._workspace import source_workspace
from pathlib import Path

from app.collaboration.tools import Context, active
from app.agents.resilience import is_transient
from app.agents.tools._workspace import active_workspace

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, service, team, concurrency=4):
        self.service, self.team = service, team
        self.concurrency = concurrency
        self.jobs = set()
        self.executions = {}
        self.loop = None
        self.owner = uuid4().hex
        self.running_roles = {}
        self.active_workers = {}
        self.team.scheduler = self
        self.last_maintenance = 0
        from app.collaboration.coordinator import LeaderCoordinator
        self.coordinator = LeaderCoordinator(service, team)

    async def start(self, *, pause_existing=False):
        await self.service.initialize()
        if pause_existing:
            await self.service.pause_unfinished_on_startup()
        await self.service.recover_requests()
        await self.coordinator.start()
        self.loop = asyncio.create_task(self.run())

    async def close(self):
        if self.loop:
            self.loop.cancel()
            await asyncio.gather(self.loop, return_exceptions=True)
        await self.coordinator.close()
        for task in self.jobs:
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)

    async def cancel_conversation(self, project, conversation):
        tasks = []
        for task, (p, identifier) in list(self.executions.items()):
            if p == project and (await self.service.get_request(p, identifier))['conversation'] == conversation:
                if not task.cancelling():
                    task.cancel()
                tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        while True:
            try:
                await self.coordinator.tick()
                if time.monotonic()-self.last_maintenance > 10:
                    await self.service.renew_leases(self.owner, set(self.executions.values()))
                    await self.service.scheduling_tick()
                    self.last_maintenance = time.monotonic()
                cancelled = []
                for task, (project, identifier) in list(self.executions.items()):
                    request = await self.service.get_request(project, identifier)
                    if request['state'] == 'cancelled' or request.get('lease_owner') != self.owner:
                        if not task.cancelling():
                            task.cancel()
                        cancelled.append(task)
                if cancelled:
                    # Finish old workspace/process cleanup before a resumed task reuses it.
                    await asyncio.gather(*cancelled, return_exceptions=True)
                from app.agents.resilience import provider_cooldown
                # A burst of provider failures pauses new work instead of starting tasks
                # that would only burn their retries.
                while len(self.jobs) < self.concurrency and not provider_cooldown():
                    job = await self.service.claim(self.owner)
                    if not job:
                        break
                    task = asyncio.create_task(self.execute(*job))
                    self.jobs.add(task)
                    self.executions[task] = (job[0], job[2]['id'])
                    task.add_done_callback(self.jobs.discard)
                    task.add_done_callback(lambda t: self.executions.pop(t, None))
            except Exception:
                logger.exception('Collaboration scheduler tick failed')
            await asyncio.sleep(.25)

    async def execute(self, project, workspace, request):
        from app.agents.worker import WorkerAgent
        role = request['recipient']
        self.running_roles[role] = self.running_roles.get(role, 0)+1
        worker = WorkerAgent(self.team.workers[role].spec)
        self.active_workers[id(worker)] = worker
        token = active.set(Context(self.service, project, request['recipient'], request['conversation'], f"request:{request['id']}:{request['attempts']}", request['id']))
        from app.agents.usage import usage_scope
        scope_token = usage_scope.set({'project_id': project, 'conversation_id': request['conversation'] or None,
                                       'request_id': request['id'], 'attempt': request['attempts']})
        workspace_token = active_workspace.set(Path(workspace))
        source_token = source_workspace.set(Path(workspace))
        output = []
        outcome, reason = 'ended', None
        identifier, instance = request['id'], request['instance_id']
        conversation_id = f"collaboration:{project}:{identifier}"
        try:
            await worker.configure_persistence(self.team._store, self.team._checkpointer)
            isolated = await self.service.prepare_workspace(project, identifier)
            active_workspace.set(isolated.work)
            active.get().instance_id = instance
            if request.get('origin_message') and request['conversation']:
                await self.team._store.add_receipt(request['conversation'], {'message_id': request['origin_message'], 'reader': role})
            # An interrupted execution continues its saved context; any other attempt
            # starts fresh from durable task state, the work journal and project facts.
            journal = await self.service.journal(project, identifier)
            thread = self.service.last_thread(journal) if request.get('resume') else None
            if thread and await worker.has_thread_state(thread):
                worker._threads[conversation_id] = thread
            resume = bool(thread) and worker._threads.get(conversation_id) == thread
            mode = 'continue' if resume else 'fresh'
            brief = self.service.journal_brief(journal)
            if not resume and request.get('documents'):
                try:
                    await self.service.create_document_drafts(project, request)
                except Exception:
                    logger.warning('Could not create document drafts', exc_info=True)
            await self.service.journal_attempt_start(project, identifier, request['attempts'], instance, mode)
            if resume and request.get('rework'):
                task = (f"Rework requested by {request['sender']} after review: {request['rework']}\n"
                        'Your earlier context for this task is above and your delivery is merged into this fresh snapshot; '
                        'address the feedback, keep what was accepted and deliver again.')
                handoff = brief or 'No newer facts were recorded.'
            elif resume:
                task = (f"Resume your assigned task after an interruption ({request.get('interrupted_reason') or 'execution interrupted'}). "
                        'Your earlier context for this task is above; continue from where you stopped and do not redo finished work.')
                handoff = brief or 'No newer facts were recorded.'
            else:
                task = request['goal']
                handoff = (f"Durable request {identifier}. Requirements: {request['based_on']}. Expected artifacts: {request['artifacts']}. "
                           f"Prior result: {request['result']}. Dependencies: {request['dependencies']}. Use collaboration_inspect_request, "
                           'collaboration_transition or collaboration_wait to inspect dependencies, deliver, or wait.'
                           + (f'\n{plan}' if (plan := await self.service.task_brief(project, request)) else '')
                           + (f'\n{drafts}' if (drafts := await self.service.draft_brief(project, request)) else '')
                           + (f'\n\n{brief}' if brief else ''))
            async for event, data in worker.stream(task, conversation_id=conversation_id, context_mode=mode, handoff=handoff):
                if event == 'context':
                    await self.service.journal_set_thread(project, identifier, instance, worker._threads[conversation_id])
                elif event == 'content':
                    output.append(data['content'])
                elif event == 'activity' and request['conversation']:
                    await self.team._store.upsert_activity(request['conversation'], {**data, 'agent': role})
            current = await self.service.get_request(project, identifier)
            if current['state'] == 'running' and current.get('instance_id') == instance:
                from app.collaboration.delivery_checks import delivery_findings
                try:
                    await self.service.set_findings(project, identifier, await delivery_findings(self.service, project, current, isolated.work))
                except Exception:
                    logger.warning('Delivery checks failed', exc_info=True)
                await self.service.publish_artifacts(project, identifier, instance)
                await self.service.transition(project, identifier, request['recipient'], 'review', result=''.join(output)[-4000:] or 'Execution finished; inspect artifacts before accepting.', artifacts=request['artifacts'], expected_instance=instance)
                outcome = 'delivered'
            else:
                outcome = {'review': 'delivered', 'completed': 'delivered', 'cancelled': 'cancelled', 'failed': 'failed',
                           'blocked': 'suspended', 'queued': 'suspended'}.get(current['state'], 'ended')
        except asyncio.CancelledError:
            outcome, reason = 'cancelled', 'execution stopped'
            raise
        except StorageConflictError as exc:
            outcome, reason = 'conflict', str(exc)
            await self.service.mark_conflict(project, identifier, str(exc), instance)
        except Exception as exc:
            logger.exception('Collaboration request failed')
            reason = f'{type(exc).__name__}: {exc}'
            transient = is_transient(exc)
            if transient and await self.service.requeue_interrupted(project, identifier, instance, reason):
                outcome = 'interrupted'
            else:
                outcome = 'failed'
                current = await self.service.get_request(project, identifier)
                if current['state'] == 'running' and current.get('instance_id') == instance:
                    await self.service.transition(project, identifier, request['recipient'], 'failed', result=str(exc)[:1000], expected_instance=instance)
                    if transient:
                        await self.service.mark_resumable(project, identifier)
        finally:
            try:
                # Awaited in place (not shielded) so no write outlives the execution; a
                # second cancellation only loses this record, never the cleanup below.
                await self.service.journal_attempt_end(project, identifier, instance, outcome, reason)
            except BaseException:
                logger.warning('Could not record the attempt outcome in the task journal', exc_info=True)
            from app.execution.processes import processes
            await processes.close(workspace=active_workspace.get())
            self.team.workers[role].total_tokens += worker.total_tokens
            self.team.workers[role].cache_input_tokens += worker.cache_input_tokens
            self.team.workers[role].cached_tokens += worker.cached_tokens
            self.team.workers[role].context_tokens = worker.context_tokens
            self.active_workers.pop(id(worker), None)
            self.running_roles[role] -= 1
            if not self.running_roles[role]:
                del self.running_roles[role]
            active.reset(token)
            usage_scope.reset(scope_token)
            active_workspace.reset(workspace_token)
            source_workspace.reset(source_token)
