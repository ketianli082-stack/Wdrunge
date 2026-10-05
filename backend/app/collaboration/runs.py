"""Conversation-scoped execution status and durable stop fences."""
import time
from uuid import uuid4
from sqlalchemy import text
from app.storage import StorageConflictError


def activated(old):
    """When the conversation's current run began: kept while it runs or is paused by a
    restart, new after a stop or idle period. Only messages sent since then wake idle
    members for a reply, never ones from before a stop."""
    if old and old.get('state') in {'active', 'paused'} and old.get('activated'):
        return old['activated']
    return time.time()


class ConversationRuns:
    async def conversation_stopped(self, project, conversation, generation=None):
        async with self.reading() as c:
            control = await self._get(c, 'conversation_run', project, conversation, False)
            return bool(control and (control['state'] != 'active' or generation is not None and control['generation'] != generation))

    async def begin_conversation_run(self, project, conversation, *, message_id=None, content=None):
        async with self.transaction() as c:
            await self._conversation(c, project, conversation)
            old = await self._get(c, 'conversation_run', project, conversation, False)
            if old and old['state'] == 'stopping':
                raise StorageConflictError('当前对话正在停止，请稍后重试')
            if message_id is not None:
                await self.store.add_user_message_in_transaction(c, conversation, content, message_id)
            generation = uuid4().hex
            await self._put(c, 'conversation_run', project, conversation, dict(state='active', generation=generation, pending=True, activated=activated(old)))
            return generation

    async def end_conversation_run(self, project, conversation, generation):
        async with self.transaction() as c:
            control = await self._get(c, 'conversation_run', project, conversation, False)
            if control and control['generation'] == generation and control.get('pending'):
                control['pending'] = False
                await self._put(c, 'conversation_run', project, conversation, control)

    async def stop_conversation_run(self, project, conversation):
        """Fence dispatch before cancelling async work; preserve files and accepted results."""
        async with self.transaction() as c:
            await self._conversation(c, project, conversation)
            generation = uuid4().hex
            await self._put(c, 'conversation_run', project, conversation, dict(state='stopping', generation=generation))
            for r in await self._list(c, 'request', project):
                if r['conversation'] == conversation and r['state'] in {'queued','running','blocked','review'}:
                    r.update(state='cancelled', lease_owner=None, lease_until=0, instance_id=None, result='用户停止了当前对话的执行。')
                    await self._put(c, 'request', project, r['id'], r)
            for g in await self._list(c, 'goal', project):
                if g['conversation'] == conversation and g['state'] not in {'completed','cancelled'}:
                    g.update(state='cancelled', reason='用户停止了当前对话的执行。', reminder_epoch=g.get('reminder_epoch',0)+1)
                    await self._put(c, 'goal', project, g['id'], g)
            session = await self._get(c, 'leader_session', project, conversation, False)
            if session:
                session.update(state='idle', retry_after=0)
                await self._put(c, 'leader_session', project, conversation, session)
            return generation

    async def finish_conversation_stop(self, project, conversation, generation):
        async with self.transaction() as c:
            control = await self._get(c, 'conversation_run', project, conversation)
            if control['generation'] != generation:
                return
            control['state'] = 'stopped'
            await self._put(c, 'conversation_run', project, conversation, control)
            await c.execute(text("UPDATE activities SET phase='failed', description=description || '（用户已停止）' WHERE conversation_id=:v AND phase='running'"), {'v':conversation})
        await self.store.add_event(conversation, 'execution_stopped', {'running':False})

    async def conversation_execution(self, project, conversation):
        async with self.reading() as c:
            await self._conversation(c, project, conversation)
            control = await self._get(c, 'conversation_run', project, conversation, False)
            stopped = control and control['state'] != 'active'
            requests = [r for r in await self._list(c, 'request', project) if r['conversation'] == conversation and r['state'] in {'queued','running','blocked','review'}]
            paused = bool(control and control['state']=='paused') or any(r.get('paused_on_restart') for r in requests)
            requests = [r for r in requests if not r.get('paused_on_restart')]
            all_goals = [g for g in await self._list(c, 'goal', project) if g['conversation'] == conversation]
            paused = paused or any(g.get('paused_on_restart') and g['state']=='paused' for g in all_goals)
            goals = [g for g in all_goals if g['state'] == 'active']
            session = await self._get(c, 'leader_session', project, conversation, False)
            leader = bool(session and session['state'] == 'running' or control and control.get('pending'))
            return dict(running=bool(not stopped and (requests or goals or leader)), stopping=bool(control and control['state']=='stopping'),
                        paused=paused, pending_tasks=0 if stopped else len(requests), active_goals=0 if stopped else len(goals))

    async def pause_unfinished_on_startup(self):
        """Loading history must never implicitly restart unfinished work."""
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT kind,project,id,data FROM collaboration_records WHERE kind IN ('request','goal')"))).mappings().all()
            import json
            conversations = set()
            for row in rows:
                value = json.loads(row['data'])
                is_task = row['kind'] == 'request'
                if is_task and value['state'] in {'queued','running','blocked','review'}:
                    value['paused_on_restart'] = True
                    if value['state'] == 'running':
                        value.update(state='queued', lease_owner=None, lease_until=0, instance_id=None,
                                     resume=True, interrupted_reason='application restarted')
                    await self._put(c, 'request', row['project'], row['id'], value)
                elif not is_task and value['state'] == 'active':
                    value.update(state='paused', paused_on_restart=True, reason='应用重启，等待用户手动继续。', reminder_epoch=value.get('reminder_epoch',0)+1)
                    await self._put(c, 'goal', row['project'], row['id'], value)
                else:
                    continue
                if value.get('conversation'):
                    conversations.add((row['project'],value['conversation']))
            pending = (await c.execute(text("SELECT project,id FROM collaboration_records WHERE kind='conversation_run' AND state='active' AND json_extract(data,'$.pending')=1"))).all()
            conversations.update((row[0],row[1]) for row in pending)
            sessions = (await c.execute(text("SELECT project,id,state FROM collaboration_records WHERE kind='leader_session'"))).all()
            conversations.update((row[0],row[1]) for row in sessions if row[2]=='running')
            # Older databases may have human history/inbox events but no saved
            # leader session yet. Fence those too before coordinator.start()
            # reconstructs their sessions.
            history = (await c.execute(text("SELECT project_id,id FROM conversations"))).all()
            for project, conversation in history:
                if (project,conversation) not in conversations:
                    control = await self._get(c,'conversation_run',project,conversation,False)
                    if not control or control['state']=='active':
                        await self._put(c,'conversation_run',project,conversation,dict(state='idle',generation=uuid4().hex))
            for project, conversation in conversations:
                control = await self._get(c,'conversation_run',project,conversation,False)
                if control and control['state'] in {'stopped','stopping'}:
                    continue
                await self._put(c,'conversation_run',project,conversation,dict(state='paused',generation=uuid4().hex,
                                                                               **({'activated': control['activated']} if control and control.get('activated') else {})))
                session = await self._get(c,'leader_session',project,conversation,False)
                if session:
                    session.update(state='idle',retry_after=0)
                    await self._put(c,'leader_session',project,conversation,session)
                await c.execute(text("UPDATE activities SET phase='failed', description=description || '（重启后已挂起）' WHERE conversation_id=:v AND phase='running'"), {'v':conversation})

        return sorted(conversations)

    async def resume_conversation_run(self, project, conversation):
        async with self.transaction() as c:
            await self._conversation(c,project,conversation)
            control = await self._get(c,'conversation_run',project,conversation,False)
            if control and control['state'] in {'stopping','stopped'}:
                raise StorageConflictError('已停止的任务不会重新启动；请发送新消息创建任务')
            await self._put(c,'conversation_run',project,conversation,dict(state='active',generation=uuid4().hex,activated=activated(control)))
            for kind in ('request','goal'):
                for value in await self._list(c,kind,project):
                    if value.get('conversation') != conversation or not value.get('paused_on_restart'):
                        continue
                    value.pop('paused_on_restart',None)
                    if kind == 'goal' and value['state']=='paused':
                        value.update(state='active',reason='用户手动继续任务。',next_reminder_at=time.time(),stagnant_reminders=0,fingerprint='')
                    await self._put(c,kind,project,value['id'],value)
            session=await self._get(c,'leader_session',project,conversation,False) or dict(id=conversation,last_wake=time.time(),last_progress=0)
            session.update(state='waiting',failures=0,retry_after=0)
            await self._put(c,'leader_session',project,conversation,session)
            await self._event(c,project,'leader',{'type':'conversation_resumed','summary':'用户手动继续本对话的未完成任务，请检查目标和任务状态。'},conversation)
