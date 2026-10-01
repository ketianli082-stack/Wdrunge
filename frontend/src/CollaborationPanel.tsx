import { useEffect, useState } from 'react';
import MessageContent from './MessageContent';
import { DeliveryTree, type DeliveryState } from './DeliveryFiles';
import './CollaborationPanel.css';

const base = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';
type Doc = { id: string; title: string; path: string; version: number };
type Job = { id: string; sender: string; recipient: string; goal: string; state: string; result: string; dependencies: string[]; delivered: string[]; priority?: number; deadline?: number; escalated_at?: number; blocked_reason?: string; instance_id?: string; workspace_id?: string; needs_review?: boolean };
type Decision = { id: string; title: string; rationale: string; state: string; version: number };
type Snapshot = { settings?: {paused:boolean}[]; decision?: Decision[]; handoff?: {id: string; created: number; summary: string}[]; document: Doc[]; request: Job[]; message: { id: string; sender: string; recipient: string; content: string }[] };

type Heading = { id: string; title: string; level: number; characters: number };

export default function CollaborationPanel({ project, files, selected, onOpen }: { project: string; files: DeliveryState; selected: string | null; onOpen: (path: string) => void }) {
  const [data, setData] = useState<Snapshot>({ document: [], request: [], message: [] });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [findings, setFindings] = useState<{kind: string; document_id?: string; decision_id?: string; fact?: string}[]>([]);
  const [checked, setChecked] = useState(false);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [content, setContent] = useState('');
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<{document_id: string; version: number; section_id: string; title: string; excerpt: string}[]>([]);
  const [section, setSection] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [outlineOffset, setOutlineOffset] = useState<number | null>(null);
  const [revisions, setRevisions] = useState<{version:number;summary:string;author:string;created:number}[]>([]);
  const [historyOffset, setHistoryOffset] = useState<number|null>(null);
  const url = `${base}/api/projects/${encodeURIComponent(project)}/collaboration`;
  async function command(action: string, payload: object) {
    const response = await fetch(`${url}/commands`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, payload }) });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '操作失败');
    return body;
  }
  async function refresh(signal?: AbortSignal) {
    const response = await fetch(url, { signal });
    if (!response.ok) throw new Error('无法读取协作记录');
    setData(await response.json());
  }
  useEffect(() => {
    const controller = new AbortController();
    const load = () => void refresh(controller.signal).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    load(); const timer = setInterval(load, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [project]);
  async function act(fn: () => Promise<void>) {
    setBusy(true); setError('');
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  const labels: Record<string, string> = { queued: '排队', running: '执行中', blocked: '等待依赖', review: '待验收', completed: '完成', cancelled: '取消', rejected: '拒绝', failed: '失败' };
  return <section className="collaboration-panel" role="tabpanel" aria-label="交付文档">
    <header><strong>交付文档</strong><span>点击文件，在场景中预览</span></header>
    <DeliveryTree files={files} selected={selected} onOpen={onOpen} />
    <details className="delivery-management"><summary>协作记录与管理</summary>
    {error && <p role="alert">{error}</p>}
    <div className="collaboration-controls">
      <button disabled={busy} onClick={() => void act(async () => { const r = await fetch(`${url}/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"paused":true}' }); if (!r.ok) throw new Error('暂停失败'); })}>暂停新任务</button>
      <button disabled={busy} onClick={() => void act(async () => { const r = await fetch(`${url}/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"paused":false}' }); if (!r.ok) throw new Error('恢复失败'); })}>恢复调度</button>
    </div>
    {data.settings?.some(s => s.paused) && <p className="collaboration-empty">调度已暂停</p>}
    <h3>已登记文档与修订历史</h3>
    <form className="collaboration-controls" onSubmit={e => { e.preventDefault(); void act(async () => { const r = await command('search', { query }); setMatches(r.matches); }); }}><input aria-label="搜索文档" placeholder="搜索文档内容" value={query} onChange={e => setQuery(e.target.value)} /><button disabled={busy || !query.trim()}>搜索</button></form>
    {matches.map((m, i) => <button className="collaboration-document" key={i} onClick={() => void act(async () => { const d = data.document.find(d => d.id === m.document_id); if (d) { setDoc({...d, version: m.version}); const r = await command('read', { document_id: d.id, version: m.version, section_id: m.section_id }); setSection(m.section_id); setContent(r.content); setNextOffset(r.next_offset); setHeadings([]); setOutlineOffset(null); } })}>{m.title}<small>{m.excerpt}</small></button>)}
    {!data.document.length && <p className="collaboration-empty">团队尚未登记共享文档</p>}
    {data.document.map(d => <button className="collaboration-document" key={d.id} disabled={busy} onClick={() => void act(async () => { const r = await command('read', { document_id: d.id, version: d.version }); setDoc(d); setRevisions([]); setHistoryOffset(null); setHeadings(r.outline); setOutlineOffset(r.next_offset); setContent(''); setSection(null); setNextOffset(null); })}>{d.title}<small>v{d.version} · {d.path}</small></button>)}
    {doc && <div className="collaboration-reader"><strong>{doc.title} · v{doc.version}</strong><button disabled={busy} onClick={() => void act(async () => { const r = await command('history', { document_id: doc.id }); setRevisions(r.revisions); setHistoryOffset(r.next_offset); })}>修订历史</button>{revisions.map(v => <button key={v.version} onClick={() => void act(async () => { const r = await command('read', { document_id: doc.id, version: v.version }); setDoc({...doc,version:v.version}); setHeadings(r.outline); setOutlineOffset(r.next_offset); setContent(''); setSection(null); setNextOffset(null); })}>v{v.version} · {v.author} · {new Date(v.created*1000).toLocaleDateString()}<small>{v.summary}</small></button>)}{historyOffset !== null && <button disabled={busy} onClick={() => void act(async () => { const r = await command('history', { document_id: doc.id, offset: historyOffset }); setRevisions([...revisions, ...r.revisions]); setHistoryOffset(r.next_offset); })}>更早修订</button>}{headings.map(h => <button key={h.id} style={{ paddingLeft: `${8 + h.level * 10}px` }} onClick={() => void act(async () => { const r = await command('read', { document_id: doc.id, version: doc.version, section_id: h.id }); setContent(r.content); setSection(h.id); setNextOffset(r.next_offset); })}>{h.title} <small>{h.characters} 字符</small></button>)}{outlineOffset !== null && <button disabled={busy} onClick={() => void act(async () => { const r = await command('read', { document_id: doc.id, version: doc.version, offset: outlineOffset }); setHeadings([...headings, ...r.outline]); setOutlineOffset(r.next_offset); })}>更多章节</button>}<MessageContent message={{ id: "preview", sender: "leader", content }} />{nextOffset !== null && section && <button disabled={busy} onClick={() => void act(async () => { const r = await command('read', { document_id: doc.id, version: doc.version, section_id: section, offset: nextOffset }); setContent(content + r.content); setNextOffset(r.next_offset); })}>继续读取</button>}{section && doc.version > 1 && <button disabled={busy} onClick={() => void act(async () => { const r = await command('diff', { document_id: doc.id, from_version: doc.version-1, to_version: doc.version, section_id: section }); setContent('```diff\n' + r.diff + '\n```'); setNextOffset(null); })}>查看本节上次变更</button>}</div>}
    <h3>协作请求</h3>
    {!data.request.length && <p className="collaboration-empty">暂无请求</p>}
    {[...data.request].reverse().map(r => <article className="collaboration-request" key={r.id} id={`request-${r.id}`}><div><strong>{r.sender} → {r.recipient}</strong><span>{labels[r.state] || r.state}</span></div><p>{r.goal}</p><div className="collaboration-controls"><label>优先级 <select aria-label={`请求 ${r.id} 优先级`} value={r.priority ?? 1} disabled={busy || ['completed','cancelled','rejected','failed'].includes(r.state)} onChange={e => void act(async () => { await command('schedule', { identifier: r.id, priority: Number(e.target.value) }); })}><option value={0}>低</option><option value={1}>普通</option><option value={2}>高</option><option value={3}>紧急</option></select></label>{r.deadline && <small>截止 {new Date(r.deadline * 1000).toLocaleString()}{r.escalated_at ? ' · 已超时升级' : ''}</small>}</div>{r.instance_id && <small>执行实例 {r.instance_id.slice(0,8)} · 独立工作区</small>}{r.needs_review && <p role="status">任务依据已更新，需要重新核对。</p>}{r.blocked_reason && <p role="status">阻塞原因：{{basis_changed:'任务依据已变化，需要重新核对',merge_conflict:'修改冲突，已保留独立工作区',dependency_failed:'依赖任务未成功'}[r.blocked_reason] || r.blocked_reason}</p>}{r.dependencies.length > 0 && <div className="collaboration-dependencies">依赖：{r.dependencies.map(id => <a href={`#request-${id}`} key={id}>{id} · {labels[data.request.find(job => job.id === id)?.state || ''] || '未知'}</a>)}</div>}{r.result && <MessageContent message={{ id: "preview", sender: r.recipient, content: r.result }} />}{r.delivered.map(path => <button className="collaboration-document" key={path} onClick={() => onOpen(path)}>{path} ↗</button>)}<footer>{r.state === 'blocked' && <button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'queued', result: '请重新核对任务依据和冲突后继续，保留已有工作。' }); })}>重新核对并排队</button>}{r.state === 'review' && <><button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'completed', result: '用户确认验收通过' }); })}>验收通过</button><button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'queued', result: '请检查交付要求并重新处理' }); })}>重新处理</button></>}{!['completed', 'cancelled', 'rejected', 'failed'].includes(r.state) && <button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'cancelled', result: '用户取消' }); })}>取消</button>}</footer></article>)}
    <h3>文档维护与决策</h3>
    <div className="collaboration-controls"><button disabled={busy} onClick={() => void act(async () => { const r = await command('maintenance', {}); setFindings(r.findings); setChecked(true); })}>检查文档</button><button disabled={busy} onClick={() => void act(async () => { await command('handoff', { save: true, summary: '用户保存项目阶段交接' }); })}>保存阶段交接</button></div>
    {checked && <p className="collaboration-empty">{findings.length ? `${findings.length} 项需要核对` : '已检查版本、更新时间、重复正文和结构化决策，未发现问题。'}</p>}
    {findings.map((f, i) => <p key={i}>{{stale_dependency: '文档依据已变化', review_due: '文档较久未更新', duplicate_section: '发现重复章节', decision_evidence_changed: '决策依据已变化', contradictory_decisions: '决策约定冲突'}[f.kind] || f.kind} · {data.document.find(d => d.id === f.document_id)?.title || f.fact || f.decision_id || '请核对相关章节'}</p>)}
    {data.decision?.map(d => <article className="collaboration-request" key={d.id}><strong>{d.title}</strong><small> · v{d.version} · {{proposed:'提议',accepted:'已采纳',rejected:'已拒绝',superseded:'已替代'}[d.state] || d.state}</small><MessageContent message={{id:d.id,sender:'leader',content:d.rationale}} /></article>)}
    {data.handoff?.slice(-3).map(h => <p className="collaboration-empty" key={h.id}>交接记录 · {new Date(h.created*1000).toLocaleString()} · {h.summary || '项目状态快照'}</p>)}
    <h3>定向消息</h3>
    {data.message.map(m => <article className="collaboration-request" key={m.id}><strong>{m.sender} → {m.recipient}</strong><MessageContent message={{ id: "preview", sender: m.sender, content: m.content }} /></article>)}
    </details>
  </section>;
}
