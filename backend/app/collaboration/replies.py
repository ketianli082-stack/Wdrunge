"""Messages to members with no task in progress: delivery status and reply turns.

A member reads its inbox only while one of its tasks runs (the Leader is woken by its
inbox). A message to a member with nothing queued, running or blocked in the
conversation would never be read, so the platform wakes that member for a short,
read-only reply turn (app.collaboration.reply_turns). Members with open tasks are not
woken: they read the message on their next step or when their task starts.
"""
import json
import time

from sqlalchemy import text

OPEN_STATES = {'queued', 'running', 'blocked'}
# Consecutive failed reply turns before the messages they were answering are given up.
REPLY_ATTEMPTS = 3

DELIVERY_NOTES = {
    'live': 'The recipient is working (or is the Leader) and reads this before its next step.',
    'on_task_start': 'The recipient has a queued or blocked task and reads this when that task starts.',
    'reply_turn': ('The recipient has no task in progress; the platform wakes them for a short reply turn. A reply turn '
                   'only answers: it cannot change files or documents, so work they must do needs a task from the Leader.'),
    'not_woken': ('The recipient has no task in progress and is not woken (messages sent from a reply turn, or outside a '
                  'conversation, wake no one); they read it when they next get a task. Tell the Leader if it needs action.'),
}


class ReplyMixin:
    async def delivery(self, c, project, conversation, recipient, wake=True):
        """How a message just sent reaches its recipient, so the sender knows whether an
        answer can come."""
        if recipient == 'leader':
            state = 'live'
        else:
            states = {r['state'] for r in await self._list(c, 'request', project)
                      if r['recipient'] == recipient and r['conversation'] == conversation and r['state'] in OPEN_STATES}
            state = ('live' if 'running' in states else 'on_task_start' if states
                     else 'reply_turn' if wake and conversation else 'not_woken')
        return {'delivery': state, 'delivery_note': DELIVERY_NOTES[state]}

    async def pending_replies(self):
        """Members to wake: messages no context of the member has read, sent since the
        conversation's current run began, while the member has no open task there.
        Each entry carries its messages, oldest first."""
        async with self.reading() as c:
            rows = (await c.execute(text('''SELECT e.project,e.conversation,e.recipient,e.seq,e.data FROM collaboration_records run
              JOIN collaboration_events e ON e.project=run.project AND e.conversation=run.id
              WHERE run.kind='conversation_run' AND run.state='active'
              AND e.created >= COALESCE(json_extract(run.data,'$.activated'), 1e18)
              AND e.recipient != 'leader' AND json_extract(e.data,'$.type')='message'
              AND COALESCE(json_extract(e.data,'$.no_wake'),0)=0
              AND NOT EXISTS (SELECT 1 FROM collaboration_receipts r
                WHERE r.project=e.project AND r.seq=e.seq AND r.context LIKE e.recipient||':%')
              ORDER BY e.seq'''))).mappings().all()
            groups = {}
            for row in rows:
                groups.setdefault((row['project'], row['conversation'], row['recipient']), []).append(
                    {'event_id': row['seq'], **json.loads(row['data'])})
            pending = []
            for (project, conversation, member), messages in groups.items():
                settings = await self._get(c, 'settings', project, 'project', False)
                if settings and settings.get('paused'):
                    continue
                if any(r['recipient'] == member and r['conversation'] == conversation and r['state'] in OPEN_STATES
                       for r in await self._list(c, 'request', project)):
                    continue
                wake = await self._get(c, 'reply_wake', project, f'{conversation}:{member}', False) or {}
                messages = [m for m in messages if m['event_id'] > wake.get('given_up_through', 0)]
                if messages and wake.get('retry_after', 0) <= time.time():
                    pending.append(dict(project=project, conversation=conversation, member=member, messages=messages))
            return pending

    async def reply_finished(self, project, conversation, member, ok, through):
        """Record a reply turn's outcome: failures back off, and after REPLY_ATTEMPTS the
        messages up to `through` are given up so a broken turn does not repeat forever."""
        key = f'{conversation}:{member}'
        async with self.transaction() as c:
            wake = await self._get(c, 'reply_wake', project, key, False) or dict(id=key)
            if ok:
                wake.update(failures=0, retry_after=0)
            else:
                wake['failures'] = wake.get('failures', 0) + 1
                if wake['failures'] >= REPLY_ATTEMPTS:
                    wake.update(failures=0, retry_after=0, given_up_through=max(through, wake.get('given_up_through', 0)))
                else:
                    wake['retry_after'] = time.time() + 30 * 2 ** wake['failures']
            await self._put(c, 'reply_wake', project, key, wake)
            return wake

    async def reply_brief(self, project, conversation, member):
        """What the member did in this conversation, for a reply turn's fresh context."""
        async with self.reading() as c:
            tasks = [r for r in await self._list(c, 'request', project)
                     if r['recipient'] == member and r['conversation'] == conversation]
        if not tasks:
            return 'You have had no task in this conversation.'
        lines = ['Your tasks in this conversation (platform record, not instructions):']
        for r in tasks[-6:]:
            lines.append(f"- {r['id']} [{r['state']}] {r.get('summary') or r['goal'][:200]}; artifacts: {r.get('artifacts') or []}; "
                         f"merged into the project: {(r.get('merged_paths') or [])[:20]}; result: {(r.get('result') or '')[:600]}")
        latest = tasks[-1]
        summary = self.journal_summary(await self.journal(project, latest['id']), notes=6)
        if summary['facts']:
            lines.append(f"Work journal facts of {latest['id']}:\n{summary['facts']}")
        if summary['notes']:
            lines.append('Notes: ' + ' | '.join(f"[{n['kind']}] {n['text']}" for n in summary['notes']))
        return '\n'.join(lines)
