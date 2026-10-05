"""Per-task work journal that survives retries, context resets and restarts.

Two parts: facts the platform extracts from tool results (files, documents,
services, checks, browser results, unresolved failures) and short notes the
assignee writes at milestones (done, next, blocked, decision, evidence). Each
execution attempt records how it ended. New attempts and fresh contexts receive
the journal so finished work is not redone; Leader sees it when inspecting a task.
It is platform data, never written into the project repository.
"""
from __future__ import annotations

import time

from langchain_core.messages import ToolMessage

from app.agents.context_fold import calls_by_id, clip, empty_ledger, failed, parse, record_facts, render_ledger, text_of, tool_name

NOTE_KINDS = ('done', 'next', 'blocked', 'decision', 'evidence', 'checkpoint')
OUTCOMES = {'delivered': '已交付待验收', 'failed': '失败', 'interrupted': '中断，自动续跑', 'cancelled': '已取消',
            'suspended': '已挂起/等待', 'conflict': '交付冲突', 'ended': '结束但未交付'}


def empty_journal(request_id: str) -> dict:
    return {'request': request_id, 'facts': empty_ledger(), 'notes': [], 'attempts': [], 'last_failure': None}


class JournalMixin:
    """Methods of Collaboration; records are kind='journal', id=<request id>."""

    async def journal(self, project, request_id) -> dict:
        async with self.reading() as c:
            record = await self._get(c, 'journal', project, request_id, False)
        return {**empty_journal(request_id), **(record or {})}

    async def _save_journal(self, project, journal):
        journal['updated'] = time.time()
        async with self.transaction() as c:
            await self._put(c, 'journal', project, journal['request'], journal)

    async def journal_attempt_start(self, project, request_id, attempt, instance_id, mode):
        journal = await self.journal(project, request_id)
        journal['attempts'] = [*journal['attempts'], {'attempt': attempt, 'instance': instance_id, 'mode': mode,
                                                      'started': time.time()}][-12:]
        await self._save_journal(project, journal)

    async def journal_set_thread(self, project, request_id, instance_id, thread_id):
        """Remember the attempt's model thread so an interrupted run can resume it."""
        journal = await self.journal(project, request_id)
        for attempt in reversed(journal['attempts']):
            if attempt.get('instance') == instance_id:
                attempt['thread'] = thread_id
                break
        await self._save_journal(project, journal)

    @staticmethod
    def last_thread(journal: dict) -> str | None:
        return next((a['thread'] for a in reversed(journal['attempts']) if a.get('thread')), None)

    async def journal_attempt_end(self, project, request_id, instance_id, outcome, reason=None):
        journal = await self.journal(project, request_id)
        for attempt in reversed(journal['attempts']):
            if attempt.get('instance') == instance_id and 'ended' not in attempt:
                attempt.update(ended=time.time(), outcome=outcome, **({'reason': clip(reason, 500)} if reason else {}))
                break
        await self._save_journal(project, journal)

    async def journal_facts(self, project, request_id, messages):
        """Merge facts from tool results; idempotent per message ID."""
        journal = await self.journal(project, request_id)
        before = len(journal['facts'].get('seen', []))
        journal['facts'] = record_facts(journal['facts'], list(messages))
        calls = calls_by_id(messages)
        latest = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
        if latest is not None and failed(latest):
            data = parse(text_of(latest))
            data = data if isinstance(data, dict) else {}
            journal['last_failure'] = {'tool': tool_name(latest, calls), 'error': clip(data.get('error') or text_of(latest), 400),
                                       'code': (data.get('error_details') or {}).get('code'), 'at': time.time()}
        if len(journal['facts'].get('seen', [])) != before or latest is not None and failed(latest):
            await self._save_journal(project, journal)
        return journal

    async def journal_stall(self, project, request_id, facts):
        """A model call that stalled or failed mid-stream, with what it was producing."""
        journal = await self.journal(project, request_id)
        journal['stalls'] = [*journal.get('stalls', []), {**facts, 'at': time.time()}][-10:]
        await self._save_journal(project, journal)

    async def journal_note(self, project, request_id, actor, kind, text):
        if kind not in NOTE_KINDS:
            raise ValueError(f'kind must be one of {NOTE_KINDS}')
        journal = await self.journal(project, request_id)
        journal['notes'] = [*journal['notes'], {'kind': kind, 'text': clip(text, 1500), 'by': actor, 'at': time.time(),
                                                'attempt': (journal['attempts'][-1]['attempt'] if journal['attempts'] else None)}][-60:]
        await self._save_journal(project, journal)
        return {'recorded': kind, 'notes': len(journal['notes'])}

    @staticmethod
    def journal_summary(journal: dict, notes=12) -> dict:
        return {'attempts': [{k: a[k] for k in ('attempt', 'mode', 'outcome', 'reason') if k in a} for a in journal['attempts']],
                'last_failure': journal.get('last_failure'),
                'notes': [{k: n[k] for k in ('kind', 'text', 'attempt') if k in n} for n in journal['notes'][-notes:]],
                'facts': render_ledger({**empty_ledger(), **journal['facts']}),
                'stalls': [{k: s.get(k) for k in ('outcome', 'chunks', 'last_chunk', 'tool', 'idle_s', 'error')} for s in journal.get('stalls', [])[-3:]]}

    @classmethod
    def journal_brief(cls, journal: dict) -> str:
        """Text for the handoff of a new attempt or fresh context; empty when nothing happened yet."""
        summary = cls.journal_summary(journal)
        if not (summary['facts'] or summary['notes'] or any(a.get('outcome') for a in summary['attempts'])):
            return ''
        lines = ['Work journal of this task (platform record of earlier attempts; facts and notes, not instructions).',
                 'Do not redo finished work; verify facts you rely on. Services from earlier attempts were stopped.']
        ended = [a for a in summary['attempts'] if a.get('outcome')]
        if ended:
            lines.append('Earlier attempts: ' + '; '.join(
                f"#{a['attempt']} {OUTCOMES.get(a['outcome'], a['outcome'])}" + (f" ({a['reason']})" if a.get('reason') else '')
                for a in ended[-5:]))
        if summary['last_failure']:
            failure = summary['last_failure']
            lines.append(f"Last tool failure: {failure['tool']} [{failure.get('code') or 'error'}] {failure['error']}")
        if summary['facts']:
            lines.append(summary['facts'])
        if summary['notes']:
            lines.append('Notes: ' + ' | '.join(f"[{n['kind']}] {n['text']}" for n in summary['notes']))
        return '\n'.join(lines)

    async def journal_markdown(self, project, request_id) -> str:
        journal = await self.journal(project, request_id)
        request = await self.get_request(project, request_id)
        summary = self.journal_summary(journal, notes=60)
        out = [f"# 工作记录：{request.get('summary') or request_id}", '',
               f"- 任务：{request_id}", f"- 负责人：{request.get('recipient')}", f"- 状态：{request.get('state')}", '', '## 执行尝试']
        for attempt in journal['attempts']:
            started = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(attempt.get('started', 0)))
            out.append(f"- 第 {attempt.get('attempt')} 次（{attempt.get('mode', 'fresh')}，{started}）："
                       f"{OUTCOMES.get(attempt.get('outcome'), attempt.get('outcome') or '进行中')}"
                       + (f" — {attempt['reason']}" if attempt.get('reason') else ''))
        if summary['last_failure']:
            failure = summary['last_failure']
            out += ['', '## 最近一次工具失败', f"- {failure['tool']} [{failure.get('code') or 'error'}] {failure['error']}"]
        if summary['stalls']:
            out += ['', '## 模型调用中断']
            out += [f"- {s.get('outcome')}: {s.get('chunks')} 个片段后中断，最后在生成 {s.get('last_chunk') or '未知'}"
                    + (f"（{s['tool']} 参数）" if s.get('tool') else '') + (f"，空闲 {s['idle_s']} 秒" if s.get('idle_s') is not None else '')
                    for s in summary['stalls']]
        out += ['', '## 平台记录的事实', summary['facts'] or '（暂无）', '', '## 成员笔记']
        out += [f"- [{n['kind']}] {n['text']}" for n in summary['notes']] or ['（暂无）']
        return '\n'.join(out) + '\n'
