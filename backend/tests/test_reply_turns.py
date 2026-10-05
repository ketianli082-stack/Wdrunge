"""Messages to members with no task in progress wake them for a read-only reply turn;
files a delivery merges reach the snapshots of tasks already running."""
import asyncio
import json

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.storage import ChatStore
from app.collaboration.service import Collaboration
from app.collaboration.workspaces import IsolatedWorkspace, MERGED_COPIES


def run(coro):
    return asyncio.run(coro)


async def setup(tmp_path, *, begin=True):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store = ChatStore(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
    project = await store.create_project('Project', str(workspace))
    service = Collaboration(store)
    await service.initialize()
    conversation = (await store.create_conversation(project['id'], 'Flow'))['id']
    if begin:
        await service.begin_conversation_run(project['id'], conversation)
    return store, service, project['id'], conversation, workspace


def test_send_receipt_states_how_the_message_reaches_the_recipient(tmp_path):
    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        task = await s.create_request(p, 'leader', 'frontend', 'Build pages', 'pages', conversation=c)
        assert (await s.send(p, 'designer', 'frontend', 'Tokens ready', 'k1', c, summary='令牌已就绪'))['delivery'] == 'on_task_start'
        await s.claim('owner')
        assert (await s.send(p, 'designer', 'frontend', 'Icons ready', 'k2', c, summary='图标已就绪'))['delivery'] == 'live'
        assert (await s.send(p, 'frontend', 'leader', 'Status', 'k3', c, summary='进度'))['delivery'] == 'live'
        receipt = await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'k4', c, summary='logo 在哪')
        assert receipt['delivery'] == 'reply_turn' and 'cannot change files' in receipt['delivery_note']
        assert (await s.send(p, 'designer', 'backend', 'FYI', 'k5', c, summary='告知', wake=False))['delivery'] == 'not_woken'
        # The status is computed per call, not stored with the message.
        assert 'delivery' not in (await s.inspect_message(p, receipt['id']))
        assert task['state'] == 'queued'
        await store.close()
    run(scenario())


def test_only_members_without_open_tasks_are_woken_for_unread_messages(tmp_path):
    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        await s.create_request(p, 'leader', 'frontend', 'Build pages', 'pages', conversation=c)
        await s.send(p, 'designer', 'frontend', 'Tokens ready', 'busy', c, summary='令牌已就绪')
        await s.send(p, 'designer', 'backend', 'From a reply turn', 'quiet', c, summary='回复', wake=False)
        await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'ask', c, summary='logo 在哪')
        pending = await s.pending_replies()
        assert [(item['member'], [m['content'] for m in item['messages']]) for item in pending] == [('designer', ['Where is the logo?'])]
        # Read by any context of the member: nothing to wake for.
        await s.acknowledge(p, 'designer', 'some-task-context', [pending[0]['messages'][0]['event_id']])
        assert await s.pending_replies() == []
        await store.close()
    run(scenario())


def test_messages_from_before_a_stop_or_without_an_active_run_never_wake(tmp_path):
    async def scenario():
        store, s, p, c, _ = await setup(tmp_path, begin=False)
        await s.send(p, 'frontend', 'designer', 'Old question', 'old', c, summary='旧问题')
        assert await s.pending_replies() == []  # no run: history stays dormant
        await s.begin_conversation_run(p, c)
        assert await s.pending_replies() == []  # sent before this run began
        await s.send(p, 'frontend', 'designer', 'New question', 'new', c, summary='新问题')
        assert [m['content'] for m in (await s.pending_replies())[0]['messages']] == ['New question']
        generation = await s.stop_conversation_run(p, c)
        await s.finish_conversation_stop(p, c, generation)
        assert await s.pending_replies() == []
        await s.begin_conversation_run(p, c)
        assert await s.pending_replies() == []  # a new run after a stop starts clean
        await store.close()
    run(scenario())


def test_run_start_survives_a_restart_pause_and_resume(tmp_path):
    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        await s.create_request(p, 'leader', 'frontend', 'Build pages', 'pages', conversation=c)
        await s.send(p, 'frontend', 'designer', 'Question before restart', 'q', c, summary='问题')
        await s.pause_unfinished_on_startup()
        assert await s.pending_replies() == []  # paused
        await s.resume_conversation_run(p, c)
        assert [m['content'] for m in (await s.pending_replies())[0]['messages']] == ['Question before restart']
        await store.close()
    run(scenario())


def test_reply_turn_inbox_shows_only_messages_the_member_never_read(tmp_path):
    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        await s.send(p, 'product', 'designer', 'Requirements ready', 'req', c, summary='需求已就绪')
        read = await s.inbox(p, 'designer', 'design-task', c)
        await s.acknowledge(p, 'designer', 'design-task', [read[0]['event_id']])
        await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'ask', c, summary='logo 在哪')
        # A fresh task context would see both; a reply turn only the unread one.
        assert len(await s.inbox(p, 'designer', 'fresh-task', c)) == 2
        assert [r['content'] for r in await s.inbox(p, 'designer', 'reply', c, reply_turn=True)] == ['Where is the logo?']
        await store.close()
    run(scenario())


def _scheduler(store, s):
    from app.agents.team import Team
    from app.collaboration.runtime import Scheduler
    team = Team()
    run_team = team.configure_persistence(store, InMemorySaver())
    return team, Scheduler(s, team), run_team


def test_reply_turn_answers_read_only_and_does_not_wake_the_sender(tmp_path, monkeypatch):
    from app.agents.worker import WorkerAgent
    seen = {}

    class Model:
        def bind_tools(self, tools):
            seen.setdefault('tools', set()).update(t['function']['name'] if 'function' in t else t['name'] for t in tools)
            return self

        async def ainvoke(self, messages):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                task = next(m.content for m in messages if 'Reply turn.' in str(m.content))
                seen['task'] = task
                ask = json.loads(task.split('\n', 2)[2].split('\n\n')[0])[0]
                return AIMessage(content='', tool_calls=[
                    {'id': 'write', 'name': 'write_project_file', 'args': {'path': 'frontend/public/logo.svg', 'content': '<svg/>'}},
                    {'id': 'publish', 'name': 'collaborate', 'args': {'action': 'publish', 'payload': {'changes': []}}},
                    {'id': 'reply', 'name': 'collaboration_send', 'args': {
                        'recipient': ask['sender'], 'reply_to': ask['id'], 'key': 'logo-answer',
                        'summary': 'logo 已合并在 frontend/public/logo.svg',
                        'content': 'Merged at frontend/public/logo.svg; your snapshot predates it.'}}])
            seen['results'] = {m.tool_call_id: m.content for m in results}
            return AIMessage(content='Answered.')

    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def scenario():
        store, s, p, c, workspace = await setup(tmp_path)
        team, scheduler, configured = _scheduler(store, s)
        await configured
        done = await s.create_request(p, 'leader', 'designer', 'Design', 'design', conversation=c)
        await s.claim(scheduler.owner)
        r = await s.get_request(p, done['id'])
        await s.transition(p, r['id'], 'designer', 'review', result='Design delivered', expected_instance=r['instance_id'])
        await s.transition(p, r['id'], 'leader', 'completed', result='Accepted')
        await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'ask', c, summary='logo 在哪')
        [item] = await s.pending_replies()
        await scheduler.replies.run(item)

        assert 'Where is the logo?' in seen['task'] and f"{done['id']} [completed]" in seen['task']
        assert not seen['tools'] & {'write_project_file', 'collaboration_publish', 'submit_task', 'collaboration_request',
                                    'execute_command', 'checkpoint_context'}
        assert {'read_project_file', 'collaboration_send'} <= seen['tools']
        assert 'reply turn only reads and answers' in seen['results']['publish']
        receipt = json.loads(seen['results']['reply'])
        assert receipt['delivery'] == 'not_woken'  # the sender is idle and is not woken by the answer
        assert not (workspace / 'frontend/public/logo.svg').exists()
        answer = await s.inspect_message(p, 'designer:logo-answer')
        assert answer['reply_to'] == 'frontend:ask' and answer['no_wake']
        # Answered and read: no second wake, and the reply does not wake the idle sender.
        assert await s.pending_replies() == []
        assert not scheduler.running_roles and not scheduler.active_workers
        await store.close()
    run(scenario())


def test_reply_written_as_text_is_delivered_and_no_reply_is_respected(tmp_path, monkeypatch):
    from app.agents.worker import WorkerAgent
    answers = iter(['The logo is at frontend/public/logo.svg.', 'NO_REPLY'])

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content=next(answers))

    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        team, scheduler, configured = _scheduler(store, s)
        await configured
        await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'ask', c, summary='logo 在哪')
        await scheduler.replies.run((await s.pending_replies())[0])
        reply = await s.inspect_message(p, 'designer:reply:frontend:ask')
        assert reply['content'] == 'The logo is at frontend/public/logo.svg.' and reply['recipient'] == 'frontend'
        await s.send(p, 'backend', 'designer', 'FYI: API is up', 'fyi', c, summary='接口已上线')
        await scheduler.replies.run((await s.pending_replies())[0])
        assert await s.pending_replies() == []
        messages = await s.snapshot(p)
        assert not any(m['id'].startswith('designer:reply:backend') for m in messages.get('message', []))
        await store.close()
    run(scenario())


def test_failed_reply_turns_back_off_and_give_up(tmp_path, monkeypatch):
    from app.agents.worker import WorkerAgent

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError('provider broke')

    monkeypatch.setattr(WorkerAgent, 'model', lambda self: Model())

    async def scenario():
        store, s, p, c, _ = await setup(tmp_path)
        team, scheduler, configured = _scheduler(store, s)
        await configured
        await s.send(p, 'frontend', 'designer', 'Where is the logo?', 'ask', c, summary='logo 在哪')
        item = (await s.pending_replies())[0]
        await scheduler.replies.run(item)
        assert await s.pending_replies() == []  # backing off
        for _ in range(2):
            await s.reply_finished(p, c, 'designer', False, item['messages'][-1]['event_id'])
        wake = await s.reply_finished(p, c, 'designer', True, 0)
        assert wake['given_up_through'] == item['messages'][-1]['event_id']
        assert await s.pending_replies() == []
        await s.send(p, 'frontend', 'designer', 'Second question', 'ask2', c, summary='第二个问题')
        assert [m['content'] for m in (await s.pending_replies())[0]['messages']] == ['Second question']
        await store.close()
    run(scenario())


def test_merged_delivery_reaches_running_snapshots_without_overwriting_their_edits(tmp_path):
    async def scenario():
        store, s, p, c, workspace = await setup(tmp_path)
        (workspace / 'README.md').write_text('base\n')
        design = await s.create_request(p, 'leader', 'designer', 'Icons', 'icons', conversation=c, artifacts=['public/logo.svg'])
        front = await s.create_request(p, 'leader', 'frontend', 'Pages', 'pages', conversation=c)
        claimed = {}
        for _ in range(2):
            _, _, r = await s.claim('owner')
            claimed[r['recipient']] = r
        designer_ws = await s.prepare_workspace(p, design['id'])
        frontend_ws = await s.prepare_workspace(p, front['id'])
        (designer_ws.work / 'public').mkdir()
        (designer_ws.work / 'public/logo.svg').write_text('<svg>real</svg>')
        (designer_ws.work / 'public/favicon.svg').write_text('<svg>fav</svg>')
        # The frontend already drew its own placeholder favicon.
        (frontend_ws.work / 'public').mkdir()
        (frontend_ws.work / 'public/favicon.svg').write_text('<svg>placeholder</svg>')
        await s.publish_artifacts(p, design['id'], claimed['designer']['instance_id'])

        assert (frontend_ws.work / 'public/logo.svg').read_text() == '<svg>real</svg>'
        assert (frontend_ws.work / 'public/favicon.svg').read_text() == '<svg>placeholder</svg>'
        assert (frontend_ws.work / MERGED_COPIES / 'public/favicon.svg').read_text() == '<svg>fav</svg>'
        # The pulled file is base content, not the frontend's edit; once it drops its
        # placeholder in favour of the project's favicon, nothing is left to conflict.
        (frontend_ws.work / 'public/favicon.svg').unlink()
        assert frontend_ws.plan() == []
        [notice] = [e for e in await s.inbox(p, 'frontend', 'pages-context', c, request=front['id']) if e['type'] != 'request']
        assert notice['type'] == 'project_files_merged'
        assert notice['pulled'] == ['public/logo.svg'] and notice['kept'] == ['public/favicon.svg']
        assert MERGED_COPIES in notice['summary']
        # Only the task it concerns sees it.
        assert not [e for e in await s.inbox(p, 'frontend', 'other', c, request='another-task') if e['type'] != 'request']
        await store.close()
    run(scenario())


def test_sync_removes_files_deleted_in_the_project_unless_changed_locally(tmp_path):
    source = tmp_path / 'project'
    source.mkdir()
    (source / 'a.txt').write_text('a')
    (source / 'b.txt').write_text('b')
    workspace = IsolatedWorkspace(source, 'snap')
    workspace.prepare()
    (workspace.work / 'b.txt').write_text('mine')
    (source / 'a.txt').unlink()
    (source / 'b.txt').unlink()
    assert workspace.sync(['a.txt', 'b.txt']) == {'pulled': ['a.txt'], 'kept': ['b.txt']}
    assert not (workspace.work / 'a.txt').exists() and (workspace.work / 'b.txt').read_text() == 'mine'
