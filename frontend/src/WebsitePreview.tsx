import { useEffect, useImperativeHandle, useRef, useState, type Ref } from 'react';
import { localPreviewUrl, parseContext, type VisualContext, type VisualAnnotation } from './visualFeedback';
import './WebsitePreview.css';

type Mode = 'browse' | 'point';
export type WebsitePreviewHandle = { captureSelection: () => Promise<VisualContext>; clearSelection: () => void };
export default function WebsitePreview({ onRequest, onAnnotation, onSelection, annotations = [], busy, onClose, ref }: {
  onRequest: (content: string) => Promise<void>; busy: boolean; onClose: () => void;
  onAnnotation: (item: VisualAnnotation) => void; onSelection?: (context: VisualContext | null) => void;
  annotations?: VisualAnnotation[]; ref?: Ref<WebsitePreviewHandle>;
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
  const [refreshing, setRefreshing] = useState(false);
  const refreshPhase = useRef<'idle' | 'connecting' | 'fetching' | 'reloading'>('idle');
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canReload = useRef(false);
  const [popupOpen, setPopupOpen] = useState(true);
  const [bounds, setBounds] = useState({ width: 0, height: 0 });
  const [popupHeight, setPopupHeight] = useState(260);
  const viewport = useRef<HTMLDivElement>(null);
  const popover = useRef<HTMLFormElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const token = useRef(crypto.randomUUID());
  const pageId = useRef('');
  const awaiting = useRef<{ id: string; resolve: (value: VisualContext) => void; reject: (error: Error) => void } | null>(null);
  const lastReady = useRef(0);
  const lastPageUrl = useRef('');
  const origin = url ? new URL(url).origin : '';
  function finishRefresh(message?: string) {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    refreshTimer.current = null; refreshPhase.current = 'idle'; setRefreshing(false);
    if (message) setError(message);
  }
  function refreshPreview() {
    try {
      const next = localPreviewUrl(address);
      setError(''); setNotice('正在重新获取网页和资源…'); setRefreshing(true);
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => finishRefresh('无法强制刷新：请确认网站已接入最新选择脚本且服务正常。'), 20000);
      if (ready && canReload.current && next === lastPageUrl.current) {
        refreshPhase.current = 'fetching'; command('wdrunge:reload');
      } else {
        // Older bridges need one reload to pick up the new refresh command.
        refreshPhase.current = 'connecting'; setUrl(next); setRevision(value => value + 1);
      }
    } catch (cause) { setError((cause as Error).message); }
  }
  useEffect(() => () => { if (refreshTimer.current) clearTimeout(refreshTimer.current); }, []);
  const elements = context?.elements || [];
  const hasSelection = !!(elements.length || context?.region);
  const selectionKey = context ? JSON.stringify([context.url, context.elements.map(item => item.selector), context.region]) : '';
  useEffect(() => { setRequirement(''); setPopupOpen(true); }, [selectionKey]);
  useEffect(() => { onSelection?.(hasSelection ? context : null); }, [context, hasSelection, onSelection]);
  useEffect(() => {
    const observer = new ResizeObserver(entries => {
      const rect = entries[0].contentRect; setBounds({ width: rect.width, height: rect.height });
    });
    if (viewport.current) observer.observe(viewport.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => { if (ready) command('wdrunge:annotations', { annotations }); }, [annotations, ready, url, revision]);
  useEffect(() => {
    const observer = new ResizeObserver(entries => setPopupHeight(entries[0].borderBoxSize?.[0]?.blockSize || entries[0].contentRect.height));
    if (popover.current) observer.observe(popover.current);
    return () => observer.disconnect();
  }, [hasSelection, popupOpen]);
  useImperativeHandle(ref, () => ({ captureSelection: freshContext, clearSelection: () => command('wdrunge:clear') }));
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
        canReload.current = Array.isArray(data.capabilities) && data.capabilities.includes('reload');
        if (lastPageUrl.current !== data.url) { lastPageUrl.current = data.url; setAddress(data.url); }
        if (refreshPhase.current === 'connecting') {
          refreshPhase.current = 'fetching'; command('wdrunge:reload');
        }
        return;
      }
      if (data.pageId !== pageId.current) return;
      if (data.type === 'wdrunge:reloading' && refreshPhase.current === 'fetching') refreshPhase.current = 'reloading';
      if (data.type === 'wdrunge:reload-error' && refreshPhase.current !== 'idle') {
        finishRefresh(typeof data.message === 'string' ? data.message.slice(0, 300) : '资源刷新失败，请重试。');
        return;
      }
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
    if (awaiting.current) return Promise.reject(new Error('正在获取选区，请稍后再试。'));
    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => {
        if (awaiting.current?.id === id) { awaiting.current = null; reject(new Error('无法获取最新页面信息，请刷新后重新选择。')); }
      }, 2500);
      awaiting.current = { id, resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } };
      command('wdrunge:snapshot', { requestId: id });
    });
  }
  async function submit(content: string) {
    if (busy || sending) return;
    setSending(true); setError('');
    try {
      await onRequest(content); setRequirement('');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '发送失败'); }
    finally { setSending(false); }
  }
  async function addAnnotation() {
    if (!requirement.trim() || sending || busy) return;
    const originalKey = selectionKey;
    setSending(true); setError('');
    try {
      const current = await freshContext();
      const key = JSON.stringify([current.url, current.elements.map(item => item.selector), current.region]);
      if ((!current.elements.length && !current.region) || key !== originalKey) throw new Error('选区已变化，请重新选择并填写批注。');
      if (annotations.length >= 10) throw new Error('一次最多添加 10 条批注，请先在群聊中发送或删除部分批注。');
      onAnnotation({ id: crypto.randomUUID(), comment: requirement.trim(), context: current });
      command('wdrunge:clear'); setRequirement('');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '添加批注失败'); }
    finally { setSending(false); }
  }
  const scriptUrl = new URL(`${import.meta.env.BASE_URL}visual-inspector.js`, window.location.href).href;
  const snippet = `<script src="${scriptUrl}" defer></script>`;
  const rects = elements.map(element => element.rect);
  const anchor = context?.region ? { ...context.region, x: context.region.x - context.viewport.scrollX, y: context.region.y - context.viewport.scrollY }
    : rects.length ? { x: Math.min(...rects.map(r => r.x)), y: Math.min(...rects.map(r => r.y)), width: 0, height: Math.max(...rects.map(r => r.y + r.height)) - Math.min(...rects.map(r => r.y)) } : null;
  const popupStyle = { left: Math.max(8, Math.min(anchor?.x || 8, bounds.width - 348)),
    top: Math.max(8, Math.min(anchor && anchor.y + anchor.height + popupHeight + 16 < bounds.height ? anchor.y + anchor.height + 8 : (anchor?.y || popupHeight + 16) - popupHeight - 8, bounds.height - popupHeight - 8)) };
  function choose(next: Mode) { setMode(next); command('wdrunge:mode', { mode: next }); }
  return <section className="website-preview" aria-label="网站预览">
    <header><strong>网站预览</strong><button onClick={onClose}>返回办公室</button></header>
    <form className="website-address" onSubmit={event => {
      event.preventDefault();
      try { const next = localPreviewUrl(address); finishRefresh(); setUrl(next); setRevision(value => value + 1); setError(''); setNotice(''); }
      catch (cause) { setError((cause as Error).message); }
    }}><input aria-label="预览网站地址" value={address} onChange={event => setAddress(event.target.value)} /><button>打开</button>
      <button type="button" disabled={!url || refreshing} onClick={refreshPreview}>{refreshing ? '刷新中…' : '刷新'}</button></form>
    <div className="website-tools">
      <button disabled={!ready || refreshing} aria-pressed={mode === 'browse'} onClick={() => choose('browse')}>浏览网页</button>
      <button disabled={!ready || refreshing} aria-pressed={mode === 'point'} onClick={() => choose('point')}>选择修改位置</button>
      {hasSelection && <><button onClick={() => setPopupOpen(value => !value)}>批注选区</button>
        <button disabled={elements.length !== 1 || !elements[0]?.ancestors.length} onClick={() => command('wdrunge:parent')}>选择父级区域</button>
        <button onClick={() => command('wdrunge:clear')}>清空选择</button></>}
      <span role="status">{refreshing ? '正在刷新网页资源…' : !ready ? '等待网页接入选择脚本' : mode === 'point' ? '点击单选 · 拖动框选 · 再点取消 · Shift 追加 · Esc 退出' : '已连接，可点选元素'}</span>
    </div>
    {notice && <p role="status" className="website-notice">{notice}</p>}
    {error && <p role="alert" className="website-error">{error}</p>}
    {url && !ready && <details className="website-setup"><summary>网页可以浏览，但还不能点选？接入选择脚本</summary>
      <p>开发页面需加入以下脚本，并刷新预览。若网页无法显示，请先启动服务并确认允许 iframe 嵌入。</p><code>{snippet}</code>
      <button disabled={busy || sending} onClick={() => void submit(`请为当前项目的本地开发网页接入 Wdrunge 元素选择脚本。预览地址是 ${url}。在开发入口 HTML 或框架的开发环境入口加入 ${snippet}，仅限开发环境，避免生产页面加载本机脚本。先检查现有框架和入口，修改后检查服务并告知我刷新预览。`)}>让 Agent 接入</button>
    </details>}
    <div className="website-viewport" ref={viewport}>{url ? <iframe key={`${url}:${revision}`} ref={frame} title="项目网站" src={url}
      sandbox="allow-scripts allow-same-origin allow-forms" referrerPolicy="no-referrer" onLoad={() => {
        if (refreshPhase.current === 'reloading') finishRefresh();
        token.current = crypto.randomUUID(); pageId.current = ''; lastReady.current = 0;
        setReady(false); setContext(null); setMode('browse'); discard('页面已加载，请选择要修改的位置。'); command('wdrunge:connect');
      }} /> : <div className="website-empty">启动项目网站后，在上方输入本机地址并打开。<br />点选元素、Shift 多选或框选区域，告诉团队你想如何修改。</div>}
    {hasSelection && popupOpen && <form ref={popover} className="website-comment-popover" aria-label="选区批注" style={popupStyle}
      onSubmit={event => { event.preventDefault(); void addAnnotation(); }}>
      <div className="website-comment-heading"><strong>{context?.region ? `已框选区域 · ${elements.length} 个元素` : elements.length === 1 ? `已选中 ${elements[0].tag}` : `已选中 ${elements.length} 个元素`}</strong>
        <button type="button" aria-label="关闭批注框" onClick={() => setPopupOpen(false)}>×</button></div>
      <div className="website-selection-list">{elements.map((element, index) => <div key={`${element.selector}:${index}`}><span title={element.selector}>{element.tag} · {element.text || element.selector}</span><button type="button" aria-label={`移除第 ${index + 1} 个元素`} onClick={() => command('wdrunge:remove', { index })}>×</button></div>)}</div>
      {context?.truncated && <small role="status">最多保留 20 个元素，请缩小范围或分批修改。</small>}
      {context?.region && !elements.length && <small>区域只包含坐标，建议选中附近元素帮助定位。</small>}
      <textarea aria-label="选区批注意见" disabled={sending || busy} placeholder="这个位置要怎样修改？" value={requirement} onChange={event => setRequirement(event.target.value)} maxLength={2000}
        onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (requirement.trim()) void addAnnotation(); } }} />
      <div className="website-comment-footer"><small>也可直接在右侧群聊提要求</small><button disabled={!requirement.trim() || !ready || busy || sending}>{sending ? '正在添加…' : '添加批注'}</button></div>
    </form>}
    </div>
  </section>;
}
