"""Event-driven Leader turns and context-free, three-minute progress checks."""
import asyncio
from contextlib import aclosing
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
        # Reconstruct session records; startup execution fences keep historical
        # conversations dormant until the user explicitly continues them.
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
            if await self.service.conversation_stopped(project, conversation):
                continue
            async with self.service.reading() as c:
                settings = await self.service._get(c, 'settings', project, 'project', False)
            if settings and settings.get('paused'):
                continue
            if not self.job or self.job.done():
                if await self.service.inbox(project, 'leader', f'leader:{conversation}', conversation):
                    self.job = asyncio.create_task(self.run(project, conversation, row['workspace_path']))
                    # Let a normal event wake take precedence over a timed check.
                    return
            if now - max(session.get('last_wake', now), session.get('last_progress', 0)) >= 180:
                await self.progress(project, conversation, now)

    async def run(self, project, conversation, workspace):
        store = self.service.store
        reply_id, reply = uuid4().hex, ''
        try:
            history = [HumanMessage(content=m['content']) if m['role']=='user' else AIMessage(content=m['content']) for m in await store.model_history(conversation)]
            # aclosing: a cancelled wake closes the Leader stream in this task and context;
            # otherwise its cleanup would run later from the garbage collector.
            async with aclosing(self.team.stream(history, conversation_id=conversation, workspace_path=Path(workspace),
                                                 wake_only=True)) as events:
                async for event, data in events:
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

    async def progress(self, project, conversation, now):
        """UI-only exchanges derived from actual runtime evidence. No LLM call,
        agent checkpoint, model history, handoff or collaboration inbox entry."""
        from app.agents.identity import member_name
        async with self.service.transaction() as c:
            session = await self.service._get(c, 'leader_session', project, conversation)
            if session['state'] in {'running', 'failed'} or now-max(session.get('last_wake', now), session.get('last_progress', 0)) < 180:
                return
            requests = [r for r in await self.service._list(c, 'request', project) if not r.get('paused_on_restart') and not r.get('goal_id') and r['conversation']==conversation and r['state'] in {'queued','running','blocked','review'}]
            if not requests:
                return
            session['last_progress'] = now
            await self.service._put(c, 'leader_session', project, conversation, session)
            roles = sorted({r['recipient'] for r in requests})
            batch = uuid4().hex
            question = dict(id=batch, sender='leader', kind='progress', content='请简要同步当前进度；有阻塞或待验收交付请正式 @我。', mentions=[{'worker': role, 'name': member_name(role), 'summary': '请同步当前工作进度。'} for role in roles])
            await self.service.store.add_chat_message_in_transaction(c, conversation, question)
            states = {'queued':'排队中', 'running':'执行中', 'blocked':'等待依赖', 'review':'待验收'}
            for role in roles:
                own = [r for r in requests if r['recipient']==role]
                counts = {state: sum(r['state']==state for r in own) for state in states}
                summary = '进度快照：' + '，'.join(f'{count} 项{states[state]}' for state,count in counts.items() if count)
                activity = (await c.execute(text('SELECT description,phase FROM activities WHERE conversation_id=:c AND agent_id=:r ORDER BY updated_at DESC LIMIT 1'), {'c':conversation,'r':role})).mappings().first()
                if activity:
                    summary += f"；最近活动：{activity['description'][:70]}（{activity['phase']}）"
                await self.service.store.add_receipt_in_transaction(c, conversation, {'message_id': batch, 'reader': role})
                response = dict(id=f'{batch}:{role}', sender=role, kind='progress', content=summary, mentions=[{'worker':'leader', 'name':member_name('leader'), 'summary':summary}])
                await self.service.store.add_chat_message_in_transaction(c, conversation, response)
                await self.service.store.add_receipt_in_transaction(c, conversation, {'message_id': response['id'], 'reader':'leader'})
                # Progress chatter remains ephemeral to models; an actionable
                # condition gets a separate durable notification with task IDs.
                attention = [r['id'] for r in own if r['state']=='review' or r.get('blocked_reason')]
                if attention:
                    await self.service._event(c, project, 'leader', {'type':'progress_attention', 'requests':attention, 'summary':'任务需要验收或解除阻塞，请检查状态。'}, conversation)
