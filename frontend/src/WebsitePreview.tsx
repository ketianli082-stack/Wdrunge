import { useEffect, useRef, useState } from 'react';
import { localPreviewUrl, parseContext, visualRequest, type VisualContext } from './visualFeedback';
import './WebsitePreview.css';

type Mode = 'browse' | 'point';
export default function WebsitePreview({ onRequest, busy, onClose }: {
  onRequest: (content: string) => Promise<void>; busy: boolean; onClose: () => void;
}) {
  const [address, setAddress] = useState('http://127.0.0.1:8101');
  const [url, setUrl] = useState('');
  const [revision, setRevision] = useState(0);
  const [ready, setReady] = useState(false);
  const [mode, setMode] = useState<Mode>('browse');
  const [context, setContext] = useState<VisualContext | null>(null);
  const [requirement, setRequirement] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [sending, setSending] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const token = useRef(crypto.randomUUID());
  const pageId = useRef('');
  const awaiting = useRef<{ id: string; resolve: (value: VisualContext) => void; reject: (error: Error) => void } | null>(null);
  const lastReady = useRef(0);
  const lastPageUrl = useRef('');
  const origin = url ? new URL(url).origin : '';
  function command(type: string, extra = {}) {
    if (origin) frame.current?.contentWindow?.postMessage({ type, token: token.current, pageId: pageId.current, ...extra }, origin);
  }
  function discard(reason: string) {
    setContext(null); setNotice(reason);
    awaiting.current?.reject(new Error(reason)); awaiting.current = null;
  }
  useEffect(() => {
    if (!url) return;
    token.current = crypto.randomUUID(); pageId.current = ''; lastReady.current = 0;
    setReady(false); setMode('browse'); setContext(null);
    function receive(event: MessageEvent) {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin || event.data?.token !== token.current) return;
      const data = event.data;
      if (typeof data.pageId !== 'string') return;
      if (data.type === 'wdrunge:ready') {
        try { if (new URL(localPreviewUrl(data.url)).origin !== origin) return; } catch { return; }
        if (pageId.current && pageId.current !== data.pageId) discard('页面已重新加载，请重新选择。');
        pageId.current = data.pageId; lastReady.current = Date.now(); setReady(true);
        if (lastPageUrl.current !== data.url) { lastPageUrl.current = data.url; setAddress(data.url); }
        return;
      }
      if (data.pageId !== pageId.current) return;
      if (data.type === 'wdrunge:page') {
        discard('页面路由已变化，旧选择已清除，请重新选择。');
        if (typeof data.url === 'string') { lastPageUrl.current = data.url; setAddress(data.url); }
      }
      if (data.type === 'wdrunge:selection') {
        const item = parseContext(data.context);
        if (!item || new URL(item.url).origin !== origin) return;
        setContext(item); setNotice(item.elements.length || item.region ? '' : '选择已清除；如果页面替换了元素，请重新点选。');
        if (['browse','point'].includes(data.mode)) setMode(data.mode);
        if (awaiting.current && data.requestId === awaiting.current.id) { awaiting.current.resolve(item); awaiting.current = null; }
      }
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      setMode('browse'); command('wdrunge:mode', { mode: 'browse' }); command('wdrunge:clear');
    }
    window.addEventListener('message', receive); window.addEventListener('keydown', escape);
    const timer = setInterval(() => {
      command('wdrunge:connect');
      if (lastReady.current && Date.now() - lastReady.current > 3000) {
        lastReady.current = 0; pageId.current = ''; setReady(false); setMode('browse');
        discard('预览连接已断开，请检查页面地址和选择脚本。');
      }
    }, 800);
    command('wdrunge:connect');
    return () => {
      window.removeEventListener('message', receive); window.removeEventListener('keydown', escape); clearInterval(timer);
      awaiting.current?.reject(new Error('预览已关闭或切换')); awaiting.current = null;
    };
  }, [url, revision, origin]);
  function freshContext(): Promise<VisualContext> {
    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => {
        if (awaiting.current?.id === id) { awaiting.current = null; reject(new Error('无法获取最新页面信息，请刷新后重新选择。')); }
      }, 2500);
      awaiting.current = { id, resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } };
      command('wdrunge:snapshot', { requestId: id });
    });
  }
  async function submit(content?: string) {
    if (busy || sending) return;
    setSending(true); setError('');
    try {
      if (!content) {
        const current = await freshContext();
        if (!current.elements.length && !current.region) throw new Error('所选元素已消失或页面已跳转，请重新选择。');
        content = visualRequest(requirement, current);
      }
      await onRequest(content); setRequirement('');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '发送失败'); }
    finally { setSending(false); }
  }
  const scriptUrl = new URL(`${import.meta.env.BASE_URL}visual-inspector.js`, window.location.href).href;
  const snippet = `<script src="${scriptUrl}" defer></script>`;
  const elements = context?.elements || [];
  const hasSelection = !!(elements.length || context?.region);
  function choose(next: Mode) { setMode(next); command('wdrunge:mode', { mode: next }); }
  return <section className="website-preview" aria-label="网站预览">
    <header><strong>网站预览</strong><button onClick={onClose}>返回办公室</button></header>
    <form className="website-address" onSubmit={event => {
      event.preventDefault();
      try { const next = localPreviewUrl(address); setUrl(next); setRevision(value => value + 1); setError(''); setNotice(''); }
      catch (cause) { setError((cause as Error).message); }
    }}><input aria-label="预览网站地址" value={address} onChange={event => setAddress(event.target.value)} /><button>打开</button>
      <button type="button" disabled={!url} onClick={() => { try { setUrl(localPreviewUrl(address)); setRevision(value => value + 1); } catch { setRevision(value => value + 1); } }}>刷新</button></form>
    <div className="website-tools">
      <button disabled={!ready} aria-pressed={mode === 'browse'} onClick={() => choose('browse')}>浏览网页</button>
      <button disabled={!ready} aria-pressed={mode === 'point'} onClick={() => choose('point')}>选择修改位置</button>
      <span role="status">{!ready ? '等待网页接入选择脚本' : mode === 'point' ? '点击单选 · 拖动框选 · 再点取消 · Shift 追加 · Esc 退出' : '已连接，可点选元素'}</span>
    </div>
    {notice && <p role="status" className="website-notice">{notice}</p>}
    {error && <p role="alert" className="website-error">{error}</p>}
    {url && !ready && <details className="website-setup"><summary>网页可以浏览，但还不能点选？接入选择脚本</summary>
      <p>开发页面需加入以下脚本，并刷新预览。若网页无法显示，请先启动服务并确认允许 iframe 嵌入。</p><code>{snippet}</code>
      <button disabled={busy || sending} onClick={() => void submit(`请为当前项目的本地开发网页接入 Wdrunge 元素选择脚本。预览地址是 ${url}。在开发入口 HTML 或框架的开发环境入口加入 ${snippet}，仅限开发环境，避免生产页面加载本机脚本。先检查现有框架和入口，修改后检查服务并告知我刷新预览。`)}>让 Agent 接入</button>
    </details>}
    <div className="website-viewport">{url ? <iframe key={`${url}:${revision}`} ref={frame} title="项目网站" src={url}
      sandbox="allow-scripts allow-same-origin allow-forms" referrerPolicy="no-referrer" onLoad={() => {
        token.current = crypto.randomUUID(); pageId.current = ''; lastReady.current = 0;
        setReady(false); setContext(null); setMode('browse'); discard('页面已加载，请选择要修改的位置。'); command('wdrunge:connect');
      }} /> : <div className="website-empty">启动项目网站后，在上方输入本机地址并打开。<br />点选元素、Shift 多选或框选区域，告诉团队你想如何修改。</div>}</div>
    {hasSelection && <form className="website-annotation" onSubmit={event => { event.preventDefault(); if (requirement.trim()) void submit(); }}>
      <div><strong>{context?.region ? `已框选区域 · ${elements.length} 个元素` : elements.length === 1 ? `已选中 ${elements[0].tag}` : `已选中 ${elements.length} 个元素`}</strong>
        <button type="button" disabled={elements.length !== 1 || !elements[0]?.ancestors.length} onClick={() => command('wdrunge:parent')}>选择父级区域</button>
        <button type="button" onClick={() => command('wdrunge:clear')}>清空选择</button></div>
      <div className="website-selection-list">{elements.map((element, index) => <div key={`${element.selector}:${index}`}><span title={element.selector}>{element.tag} · {element.text || element.selector}</span><button type="button" aria-label={`移除第 ${index + 1} 个元素`} onClick={() => command('wdrunge:remove', { index })}>×</button></div>)}</div>
      {context?.truncated && <small role="status">选择过多，最多保留 20 个元素；请缩小范围或分批修改。</small>}
      {context?.region && !elements.length && <small>区域未完整包含 DOM 元素，将提供区域坐标。建议扩大框选或点选附近元素，以便定位源码。</small>}
      <textarea aria-label="网页修改要求" disabled={sending} placeholder="例如：这两个按钮统一加粗；这块区域间距缩小；点击这里保存文章" value={requirement} onChange={event => setRequirement(event.target.value)} maxLength={4000} />
      <button disabled={!requirement.trim() || !ready || busy || sending}>{sending ? '团队正在处理…' : '发送修改要求'}</button>
      <small>发送前自动重新采集最新元素；修改后通常热更新，也可刷新查看。要操作网页，请切换“浏览网页”。</small>
    </form>}
  </section>;
}
