"""Event-driven Leader turns, independent of foreground HTTP streams."""
import asyncio
import json
import logging
import time
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import text

logger = logging.getLogger(__name__)


class LeaderCoordinator:
    def __init__(self, service, team):
        self.service, self.team = service, team
        self.job = None

    async def start(self):
        # Resume existing project conversations after an API restart. Fresh test
        # or integration requests without any human conversation remain detached.
        async with self.service.transaction() as c:
            rows = (await c.execute(text("SELECT DISTINCT c.id,c.project_id FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE m.kind='user'"))).mappings().all()
            for row in rows:
                record = await self.service._get(c, 'leader_session', row['project_id'], row['id'], False)
                if record is None:
                    record = dict(id=row['id'], state='waiting', last_wake=time.time(), last_progress=0, retry_after=0, failures=0)
                if record['state'] == 'running':
                    record['state'] = 'waiting'
                await self.service._put(c, 'leader_session', row['project_id'], row['id'], record)

    async def close(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
            self.job = None

    async def tick(self, now=None):
        now = time.time() if now is None else now
        async with self.service.reading() as c:
            rows = (await c.execute(text("SELECT r.project,r.data,p.workspace_path FROM collaboration_records r JOIN projects p ON p.id=r.project JOIN conversations v ON v.id=r.id WHERE r.kind='leader_session' ORDER BY r.updated"))).mappings().all()
        for row in rows:
            session = json.loads(row['data'])
            if session['state'] in {'running', 'failed'} or session.get('retry_after', 0) > now:
                continue
            project, conversation = row['project'], session['id']
            async with self.service.reading() as c:
                settings = await self.service._get(c, 'settings', project, 'project', False)
            if settings and settings.get('paused'):
                continue
            if not self.job or self.job.done():
                if await self.service.inbox(project, 'leader', f'leader:{conversation}', conversation):
                    self.job = asyncio.create_task(self.run(project, conversation, row['workspace_path']))
                    # Let a normal event wake take precedence over a timed check.
                    return

    async def run(self, project, conversation, workspace):
        store = self.service.store
        reply_id, reply = uuid4().hex, ''
        try:
            history = [HumanMessage(content=m['content']) if m['role']=='user' else AIMessage(content=m['content']) for m in await store.model_history(conversation)]
            async for event, data in self.team.stream(history, conversation_id=conversation, workspace_path=Path(workspace), wake_only=True):
                if event == 'activity':
                    await store.upsert_activity(conversation, data)
                elif event == 'chat_message':
                    await store.add_chat_message(conversation, data)
                elif event == 'read_receipt':
                    await store.add_receipt(conversation, data)
                elif event == 'message_reaction':
                    await store.add_reaction(conversation, data)
                elif event == 'delta':
                    if not reply:
                        await store.start_assistant_message(conversation, reply_id, data['content'])
                    reply += data.get('content', '')
                    await store.update_assistant_message(reply_id, reply, 'completed')
                    await self.team.acknowledge_leader(conversation)
                    await store.add_event(conversation, 'assistant_completed', {'id': reply_id, 'content_length': len(reply)})
            if reply:
                await self.service.leader_session(project, conversation, failures=0, retry_after=0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Background Leader coordination failed')
            async with self.service.transaction() as c:
                session = await self.service._get(c, 'leader_session', project, conversation)
                failures = session.get('failures', 0) + 1
                session.update(failures=failures, state='failed' if failures >= 3 else 'waiting', retry_after=time.time()+min(60, 5*2**failures))
                await self.service._put(c, 'leader_session', project, conversation, session)
            await store.upsert_activity(conversation, dict(id=uuid4().hex, agent='leader', tool='coordination', description='自动协调失败，稍后重试。' if failures < 3 else '自动协调连续失败，请检查后继续对话。', phase='failed'))

