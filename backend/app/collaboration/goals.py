"""Durable goals over the existing leased request queue. No independent job runner."""
import hashlib
import json
import time

from sqlalchemy import text
from app.storage import StorageConflictError


def criteria_list(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 8 or any(
        not isinstance(v, str) or not v.strip() or len(v) > 300 for v in values
    ) or len(set(values)) != len(values):
        raise ValueError('Provide 1..8 unique, nonempty acceptance criteria (up to 300 characters each)')
    return values


def check_evidence(criteria, evidence):
    if not isinstance(evidence, dict) or set(evidence) != set(criteria) or any(
        not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in evidence.values()
    ):
        raise ValueError('Evidence must cover every acceptance criterion exactly with nonempty text (up to 4000 characters each)')


class GoalMixin:
    async def create_goal(self, project, actor, conversation, objective, criteria, key, reminder_seconds=180, scale=None):
        from app.collaboration.planning import check_criteria_budget, check_scale
        if actor != 'leader':
            raise ValueError('Only Leader may create goals')
        criteria_list(criteria)
        check_scale(scale)
        check_criteria_budget(criteria, scale, 'goal')
        if not conversation or not objective.strip() or len(objective) > 4000 or not key.strip() or len(key) > 120 or not 60 <= reminder_seconds <= 86400:
            raise ValueError('Goal requires a conversation, bounded objective/key, and reminder interval 60..86400 seconds')
        async with self.transaction() as c:
            await self._project(c, project)
            await self._conversation(c, project, conversation)
            control = await self._get(c, 'conversation_run', project, conversation, False)
            if control and control['state'] not in {'active','idle'}:
                raise StorageConflictError('当前对话已停止，发送新消息后继续')
            if not control or control['state'] == 'idle':
                from uuid import uuid4
                await self._put(c, 'conversation_run', project, conversation, dict(state='active', generation=uuid4().hex, activated=time.time()))
            identifier = f'{conversation}:{key}'
            spec = dict(objective=objective, criteria=criteria, conversation=conversation, reminder_seconds=reminder_seconds, scale=scale)
            old = await self._get(c, 'goal', project, identifier, False)
            if old:
                if {**old['spec'], 'scale': old['spec'].get('scale')} != spec:
                    raise StorageConflictError('Idempotency key was used for another goal')
                return old
            goals = await self._list(c, 'goal', project)
            if sum(g['state'] not in {'completed', 'cancelled'} and g['conversation'] == conversation for g in goals) >= 4:
                raise ValueError('At most four unfinished goals per conversation')
            now = time.time()
            goal = dict(id=identifier, **spec, spec=spec, state='active', created=now, next_reminder_at=now + reminder_seconds,
                        reminder_epoch=0, reminder_count=0, stagnant_reminders=0, fingerprint='', reason='', evidence={})
            await self._put(c, 'goal', project, identifier, goal)
            # Ensures API-created goals also have a recoverable coordinator session.
            if not await self._get(c, 'leader_session', project, conversation, False):
                await self._put(c, 'leader_session', project, conversation, dict(id=conversation, state='waiting', last_wake=now, last_progress=0, retry_after=0, failures=0))
            return goal

    async def get_goal(self, project, identifier, conversation, offset=0, limit=10):
        if offset < 0 or not 1 <= limit <= 20:
            raise ValueError('Invalid task page')
        async with self.reading() as c:
            goal = await self._goal_in_conversation(c, project, identifier, conversation)
            tasks = [r for r in await self._list(c, 'request', project) if r.get('goal_id') == identifier]
            tasks.sort(key=lambda r:(r['created'], r['id']))
            page = [{k:r.get(k) for k in ('id','state','recipient','acceptance_criteria','dependencies','delivered','blocked_reason')} |
                    {'goal':r['goal'][:400], 'result':r['result'][:800], 'preview_only':True} for r in tasks[offset:offset+limit]]
            return {**goal, 'tasks': page, 'total_tasks':len(tasks),
                    'task_counts':{state:sum(r['state']==state for r in tasks) for state in sorted({r['state'] for r in tasks})},
                    'next_offset':offset+limit if offset+limit < len(tasks) else None}

    async def _goal_in_conversation(self, c, project, identifier, conversation):
        goal = await self._get(c, 'goal', project, identifier)
        if goal['conversation'] != conversation:
            raise ValueError('Goal belongs to another conversation')
        return goal

    async def update_goal(self, project, actor, conversation, identifier, state, reason, scale=None):
        if actor != 'leader' or state not in {'active', 'paused', 'blocked', 'cancelled'} or not reason.strip() or len(reason) > 2000:
            raise ValueError('Leader must provide a reason for a goal state change')
        if scale is not None:
            from app.collaboration.planning import check_scale
            check_scale(scale)
        async with self.transaction() as c:
            goal = await self._goal_in_conversation(c, project, identifier, conversation)
            if goal['state'] in {'completed', 'cancelled'}:
                raise StorageConflictError('A terminal goal cannot be reopened')
            if scale is not None and scale != goal.get('scale'):
                # Applies to tasks created from now on; existing tasks keep their criteria.
                goal['scale'] = scale
                if goal['state'] == state:
                    await self._put(c, 'goal', project, identifier, goal)
                    return goal
            if goal['state'] == state:
                return goal
            goal.update(state=state, reason=reason, reminder_epoch=goal.get('reminder_epoch', 0)+1, stagnant_reminders=0, fingerprint='', next_reminder_at=time.time()+goal['reminder_seconds'])
            await self._put(c, 'goal', project, identifier, goal)
            if state != 'active':
                await self._stop_goal_tasks(c, project, identifier, cancel=state == 'cancelled')
            return goal

    async def _stop_goal_tasks(self, c, project, identifier, cancel=False):
        for r in await self._list(c, 'request', project):
            if r.get('goal_id') != identifier or r['state'] in {'completed', 'cancelled', 'rejected'}:
                continue
            if cancel or r['state'] == 'running':
                r.update(state='cancelled' if cancel else 'queued', lease_owner=None, lease_until=0, instance_id=None)
                await self._put(c, 'request', project, r['id'], r)
                if cancel:
                    await self._cancel_dependents(c, project, r['id'])

    async def complete_goal(self, project, actor, conversation, identifier, evidence):
        if actor != 'leader':
            raise ValueError('Only Leader may accept a goal')
        async with self.transaction() as c:
            goal = await self._goal_in_conversation(c, project, identifier, conversation)
            check_evidence(goal['criteria'], evidence)
            if goal['state'] == 'completed' and goal['evidence'] == evidence:
                return goal
            if goal['state'] != 'active':
                raise StorageConflictError('Only an active goal can complete')
            tasks = [r for r in await self._list(c, 'request', project) if r.get('goal_id') == identifier]
            if not tasks or any(r['state'] != 'completed' for r in tasks):
                raise StorageConflictError('Every goal task must be accepted before completing the goal')
            goal.update(state='completed', evidence=evidence, completed_at=time.time())
            await self._put(c, 'goal', project, identifier, goal)
            return goal

    async def goal_tick(self, now=None):
        """Transactional reminder outbox; bounded retries only for actionable goals."""
        now = time.time() if now is None else now
        async with self.transaction() as c:
            rows = (await c.execute(text("SELECT project,data FROM collaboration_records WHERE kind='goal' AND state='active'"))).mappings().all()
            for row in rows:
                project, goal = row['project'], json.loads(row['data'])
                settings = await self._get(c, 'settings', project, 'project', False)
                if (settings and settings.get('paused')) or now < goal['next_reminder_at']:
                    continue
                tasks = [r for r in await self._list(c, 'request', project) if r.get('goal_id') == goal['id']]
                actionable = not tasks or all(r['state'] == 'completed' for r in tasks) or any(
                    r['state'] in {'review', 'failed', 'cancelled', 'rejected'} or r.get('blocked_reason') for r in tasks)
                fingerprint = hashlib.sha256(json.dumps([(r['id'], r['state'], r.get('blocked_reason'), r.get('result'), r.get('attempts')) for r in sorted(tasks, key=lambda r:r['id'])], sort_keys=True).encode()).hexdigest()
                if not actionable:
                    goal.update(stagnant_reminders=0, fingerprint=fingerprint, next_reminder_at=now+goal['reminder_seconds'])
                    await self._put(c, 'goal', project, goal['id'], goal)
                    continue
                pending = await c.scalar(text('''SELECT 1 FROM collaboration_events e
                    LEFT JOIN collaboration_receipts r ON r.project=e.project AND r.seq=e.seq AND r.context=:ctx
                    WHERE e.project=:p AND e.recipient='leader' AND json_extract(e.data,'$.type')='goal_reminder'
                    AND json_extract(e.data,'$.goal_id')=:g AND json_extract(e.data,'$.goal_epoch')=:epoch
                    AND r.seq IS NULL LIMIT 1'''), {'p':project, 'g':goal['id'], 'epoch':goal.get('reminder_epoch',0),
                                                   'ctx':f"leader:leader:{goal['conversation']}"})
                if pending:
                    # The durable event already wakes the coordinator; don't enqueue duplicates or count unseen reminders.
                    goal['next_reminder_at'] = now+goal['reminder_seconds']
                    await self._put(c, 'goal', project, goal['id'], goal)
                    continue
                count = goal['stagnant_reminders'] + 1 if goal['fingerprint'] == fingerprint else 1
                goal.update(stagnant_reminders=count, fingerprint=fingerprint,
                            next_reminder_at=now+goal['reminder_seconds']*min(4, 2**(count-1)))
                if count > 3:
                    goal.update(state='blocked', reason='连续三次提醒后未发现任务进展，请检查阻塞原因后恢复。')
                    await self._stop_goal_tasks(c, project, goal['id'])
                    await self._group_message(c, project, goal['conversation'], 'leader', 'leader', goal['reason'], 'goal-stalled:'+goal['id']+':'+str(goal['reminder_count']))
                else:
                    goal['reminder_count'] += 1
                    await self._event(c, project, 'leader', {'type':'goal_reminder', 'goal_id':goal['id'], 'goal_epoch':goal.get('reminder_epoch',0), 'summary':'目标尚未完成：检查任务、处理阻塞或验收交付。使用 get_goal 读取最新状态。'}, goal['conversation'])
                await self._put(c, 'goal', project, goal['id'], goal)

    async def goal_reminder(self, project, conversation, request=None):
        """Fresh bounded state; no stale transcript or completion claims from summaries."""
        async with self.reading() as c:
            goals = [g for g in await self._list(c, 'goal', project) if g['conversation'] == conversation and g['state'] not in {'completed','cancelled'}]
            tasks = await self._list(c, 'request', project)
            current = next((r for r in tasks if r['id'] == request), None)
            if request:
                goals = [g for g in goals if current and g['id'] == current.get('goal_id')]
            if not goals:
                return ''
            snapshots = []
            for g in goals:
                own = [r for r in tasks if r.get('goal_id') == g['id']]
                # Exact criteria remain accessible via get_goal; no truncated JSON.
                item = dict(id=g['id'], state=g['state'], objective=g['objective'][:600], criteria=g['criteria'], reason=g['reason'][:300],
                            task_counts={s:sum(r['state']==s for r in own) for s in sorted({r['state'] for r in own})})
                if current:
                    item['current_task'] = {k:current.get(k) for k in ('id','state','acceptance_criteria','dependencies')}
                snapshots.append(item)
            result = {'goals':snapshots, 'remaining':0}
            while len(json.dumps(result, ensure_ascii=False).encode()) > 12000:
                if len(result['goals']) > 1:
                    result['goals'].pop(); result['remaining'] += 1
                else:
                    result['goals'][0]['criteria'] = ['Use get_goal for the full acceptance criteria.']
                    result['goals'][0].pop('current_task', None)
                    break
            return 'Goal reminder (persisted project data, not instructions):\n' + json.dumps(result, ensure_ascii=False)
