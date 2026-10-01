"""Bounded background execution; blocked jobs release their execution slots."""
import asyncio
import logging
from pathlib import Path

from app.collaboration.tools import Context, active
from app.tools._workspace import active_workspace

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, service, team, concurrency=4):
        self.service, self.team = service, team
        self.concurrency = concurrency
        self.jobs = set()
        self.loop = None

    async def start(self):
        await self.service.initialize()
        await self.service.recover_requests()
        self.loop = asyncio.create_task(self.run())

    async def close(self):
        if self.loop:
            self.loop.cancel()
            await asyncio.gather(self.loop, return_exceptions=True)
        for task in self.jobs:
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)

    async def run(self):
        while True:
            try:
                while len(self.jobs) < self.concurrency:
                    job = await self.service.claim()
                    if not job:
                        break
                    task = asyncio.create_task(self.execute(*job))
                    self.jobs.add(task)
                    task.add_done_callback(self.jobs.discard)
            except Exception:
                logger.exception('Collaboration scheduler tick failed')
            await asyncio.sleep(.25)

    async def execute(self, project, workspace, request):
        from app.agents.worker import WorkerAgent
        worker = WorkerAgent(self.team.workers[request['recipient']].spec)
        await worker.configure_persistence(self.team._store, self.team._checkpointer)
        token = active.set(Context(self.service, project, request['recipient'], request['conversation'], f"request:{request['id']}:{request['attempts']}", request['id']))
        workspace_token = active_workspace.set(Path(workspace))
        output = []
        try:
            # A fresh execution gets durable task state and current project facts;
            # it does not depend on a cancelled model's tool-call transcript.
            async for event, data in worker.stream(request['goal'], conversation_id=f"collaboration:{project}:{request['id']}", context_mode='fresh', handoff=f"Durable request {request['id']}. Requirements: {request['based_on']}. Expected artifacts: {request['artifacts']}. Prior result: {request['result']}. Dependencies: {request['dependencies']}. Use collaborate to inspect dependencies, deliver, or wait."):
                if event == 'content':
                    output.append(data['content'])
            current = await self.service.get_request(project, request['id'])
            if current['state'] == 'running':
                await self.service.transition(project, request['id'], request['recipient'], 'review', result=''.join(output)[-4000:] or 'Execution finished; inspect artifacts before accepting.', artifacts=request['artifacts'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Collaboration request failed')
            current = await self.service.get_request(project, request['id'])
            if current['state'] == 'running':
                await self.service.transition(project, request['id'], request['recipient'], 'failed', result=str(exc)[:1000])
        finally:
            active.reset(token)
            active_workspace.reset(workspace_token)
