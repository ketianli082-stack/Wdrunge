import { useEffect, useState } from 'react';
import MessageContent from './MessageContent';
import './CollaborationPanel.css';

const base = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';
type Doc = { id: string; title: string; path: string; version: number };
type Job = { id: string; sender: string; recipient: string; goal: string; state: string; result: string; dependencies: string[]; delivered: string[] };
type Snapshot = { document: Doc[]; request: Job[]; message: { id: string; sender: string; recipient: string; content: string }[] };
type Heading = { id: string; title: string; level: number; characters: number };

export default function CollaborationPanel({ project }: { project: string }) {
  const [data, setData] = useState<Snapshot>({ document: [], request: [], message: [] });
  const [error, setError] = useState('');
  const [doc, setDoc] = useState<Doc | null>(null);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState(false);
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
    setDoc(null); setHeadings([]); setContent(''); setError('');
    const load = () => void refresh(controller.signal).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    load(); const timer = setInterval(load, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [project]);
  async function act(fn: () => Promise<void>) {
    setBusy(true); setError('');
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  const labels: Record<string, string> = { queued: '排队', running: '执行中', blocked: '等待依赖', review: '待验收', completed: '完成', cancelled: '取消', rejected: '拒绝', failed: '失败' };
  return <section className="collaboration-panel" role="tabpanel" aria-label="项目协作">
    <header><strong>项目协作</strong><span>跨会话保存</span></header>
    {error && <p role="alert">{error}</p>}
    <div className="collaboration-controls">
      <button disabled={busy} onClick={() => void act(async () => { const r = await fetch(`${url}/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"paused":true}' }); if (!r.ok) throw new Error('暂停失败'); })}>暂停新任务</button>
      <button disabled={busy} onClick={() => void act(async () => { const r = await fetch(`${url}/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"paused":false}' }); if (!r.ok) throw new Error('恢复失败'); })}>恢复调度</button>
    </div>
    <h3>共享文档</h3>
    {!data.document.length && <p className="collaboration-empty">团队尚未登记共享文档</p>}
    {data.document.map(d => <button className="collaboration-document" key={d.id} disabled={busy} onClick={() => void act(async () => { const r = await command('read', { document_id: d.id, version: d.version }); setDoc(d); setHeadings(r.outline); setContent(''); })}>{d.title}<small>v{d.version} · {d.path}</small></button>)}
    {doc && <div className="collaboration-reader"><strong>{doc.title} · v{doc.version}</strong>{headings.map(h => <button key={h.id} style={{ paddingLeft: `${8 + h.level * 10}px` }} onClick={() => void act(async () => { const r = await command('read', { document_id: doc.id, version: doc.version, section_id: h.id }); setContent(r.content + (r.next_offset ? '\n\n（内容未完，可通过文档读取工具继续分页）' : '')); })}>{h.title} <small>{h.characters} 字符</small></button>)}<MessageContent message={{ id: "preview", sender: "leader", content }} /></div>}
    <h3>协作请求</h3>
    {!data.request.length && <p className="collaboration-empty">暂无请求</p>}
    {[...data.request].reverse().map(r => <article className="collaboration-request" key={r.id} id={`request-${r.id}`}><div><strong>{r.sender} → {r.recipient}</strong><span>{labels[r.state] || r.state}</span></div><p>{r.goal}</p>{r.dependencies.length > 0 && <small>依赖：{r.dependencies.join('、')}</small>}{r.result && <MessageContent message={{ id: "preview", sender: r.recipient, content: r.result }} />}{r.delivered.map(path => <code key={path}>{path}</code>)}<footer>{r.state === 'review' && <><button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'completed', result: '用户确认验收通过' }); })}>验收通过</button><button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'queued', result: '请检查交付要求并重新处理' }); })}>重新处理</button></>}{!['completed', 'cancelled', 'rejected', 'failed'].includes(r.state) && <button disabled={busy} onClick={() => void act(async () => { await command('transition', { identifier: r.id, state: 'cancelled', result: '用户取消' }); })}>取消</button>}</footer></article>)}
    <h3>定向消息</h3>
    {data.message.map(m => <article className="collaboration-request" key={m.id}><strong>{m.sender} → {m.recipient}</strong><MessageContent message={{ id: "preview", sender: m.sender, content: m.content }} /></article>)}
  </section>;
}
