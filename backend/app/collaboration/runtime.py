"""Bounded background execution; blocked jobs release their execution slots."""
import asyncio
import logging
import time
from uuid import uuid4
from app.storage import StorageConflictError
from app.tools._workspace import source_workspace
from pathlib import Path

from app.collaboration.tools import Context, active
from app.tools._workspace import active_workspace

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

    async def start(self):
        await self.service.initialize()
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

    async def run(self):
        while True:
            try:
                await self.coordinator.tick()
                if time.monotonic()-self.last_maintenance > 10:
                    await self.service.renew_leases(self.owner, set(self.executions.values()))
                    await self.service.scheduling_tick()
                    self.last_maintenance = time.monotonic()
                for task, (project, identifier) in list(self.executions.items()):
                    request = await self.service.get_request(project, identifier)
                    if request['state'] == 'cancelled' or request.get('lease_owner') != self.owner:
                        task.cancel()
                while len(self.jobs) < self.concurrency:
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
        workspace_token = active_workspace.set(Path(workspace))
        source_token = source_workspace.set(Path(workspace))
        output = []
        try:
            await worker.configure_persistence(self.team._store, self.team._checkpointer)
            isolated = await self.service.prepare_workspace(project, request['id'])
            active_workspace.set(isolated.work)
            active.get().instance_id = request['instance_id']
            if request.get('origin_message') and request['conversation']:
                await self.team._store.add_receipt(request['conversation'], {'message_id': request['origin_message'], 'reader': role})
            # A fresh execution gets durable task state and current project facts;
            # it does not depend on a cancelled model's tool-call transcript.
            async for event, data in worker.stream(request['goal'], conversation_id=f"collaboration:{project}:{request['id']}", context_mode='fresh', handoff=f"Durable request {request['id']}. Requirements: {request['based_on']}. Expected artifacts: {request['artifacts']}. Prior result: {request['result']}. Dependencies: {request['dependencies']}. Use collaborate to inspect dependencies, deliver, or wait."):
                if event == 'content':
                    output.append(data['content'])
                elif event == 'activity' and request['conversation']:
                    await self.team._store.upsert_activity(request['conversation'], {**data, 'agent': role})
            current = await self.service.get_request(project, request['id'])
            if current['state'] == 'running':
                await self.service.publish_artifacts(project, request['id'], request['instance_id'])
                await self.service.transition(project, request['id'], request['recipient'], 'review', result=''.join(output)[-4000:] or 'Execution finished; inspect artifacts before accepting.', artifacts=request['artifacts'])
        except asyncio.CancelledError:
            raise
        except StorageConflictError as exc:
            await self.service.mark_conflict(project, request['id'], str(exc), request['instance_id'])
        except Exception as exc:
            logger.exception('Collaboration request failed')
            current = await self.service.get_request(project, request['id'])
            if current['state'] == 'running':
                await self.service.transition(project, request['id'], request['recipient'], 'failed', result=str(exc)[:1000])
        finally:
            self.team.workers[role].total_tokens += worker.total_tokens
            self.team.workers[role].context_tokens = worker.context_tokens
            self.active_workers.pop(id(worker), None)
            self.running_roles[role] -= 1
            if not self.running_roles[role]:
                del self.running_roles[role]
            active.reset(token)
            active_workspace.reset(workspace_token)
            source_workspace.reset(source_token)
