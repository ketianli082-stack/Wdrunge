import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import './DeliveryFiles.css';

const apiBase = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:17420';
export type DeliveryEntry = { path: string; name: string; kind: 'file' | 'folder'; size: number; modified: number };
export type DeliveryState = { entries: DeliveryEntry[]; revision: string; truncated: boolean; status: 'connecting' | 'live' | 'reconnecting' };
type Preview = { path: string; kind: 'markdown' | 'text' | 'image' | 'pdf' | 'unsupported'; content: string; size: number; truncated: boolean };
export const deliveryUrl = (project: string, endpoint: string) => `${apiBase}/api/projects/${encodeURIComponent(project)}/delivery/${endpoint}`;

export function useDeliveryFiles(project: string | undefined): DeliveryState {
  const [state, setState] = useState<DeliveryState>({ entries: [], revision: '', truncated: false, status: 'connecting' });
  useEffect(() => {
    let disposed = false;
    let stream: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    setState({ entries: [], revision: '', truncated: false, status: 'connecting' });
    if (!project) return;
    function connect() {
      if (disposed) return;
      stream = new EventSource(deliveryUrl(project!, 'events'));
      stream.addEventListener('files', event => {
        if (disposed) return;
        try {
          const data = JSON.parse((event as MessageEvent).data);
          setState({ ...data, status: 'live' });
        } catch { /* A malformed event must not erase the last valid directory. */ }
      });
      stream.onerror = () => {
        stream?.close();
        if (!disposed) {
          setState(current => ({ ...current, status: 'reconnecting' }));
          retry = setTimeout(connect, 1500);
        }
      };
    }
    connect();
    return () => { disposed = true; stream?.close(); clearTimeout(retry); };
  }, [project]);
  return state;
}

export function DeliveryTree({ files, selected, onOpen }: { files: DeliveryState; selected: string | null; onOpen: (path: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['docs']));
  const [query, setQuery] = useState('');
  const children = new Map<string, DeliveryEntry[]>();
  const matches = query.trim().toLocaleLowerCase();
  const shown = new Set<string>();
  for (const entry of files.entries) {
    if (!matches || entry.path.toLocaleLowerCase().includes(matches)) {
      let path = entry.path;
      shown.add(path);
      while (path.includes('/')) { path = path.slice(0, path.lastIndexOf('/')); shown.add(path); }
    }
    const parent = entry.path.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/')) : '';
    children.set(parent, [...(children.get(parent) || []), entry]);
  }
  function branch(parent: string, depth: number) {
    return (children.get(parent) || []).filter(entry => shown.has(entry.path)).map(entry => {
      const open = expanded.has(entry.path) || !!matches;
      return <li key={entry.path}>
        <button className={`delivery-entry ${selected === entry.path ? 'selected' : ''}`} style={{ paddingLeft: 10 + depth * 16 }}
          aria-expanded={entry.kind === 'folder' ? open : undefined} aria-current={selected === entry.path ? 'true' : undefined}
          title={entry.path} onClick={() => {
            if (entry.kind === 'file') onOpen(entry.path);
            else setExpanded(current => { const next = new Set(current); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next; });
          }}><span className="delivery-chevron" aria-hidden="true">{entry.kind === 'folder' ? (open ? '⌄' : '›') : ''}</span><span className={`delivery-file-icon ${entry.kind}`} aria-hidden="true">{entry.kind === 'folder' ? '▱' : '≡'}</span><span className="delivery-name">{entry.name}</span>{entry.kind === 'file' && <small>{formatSize(entry.size)}</small>}</button>
        {entry.kind === 'folder' && open && <ul>{branch(entry.path, depth + 1)}</ul>}
      </li>;
    });
  }
  return <div className="delivery-browser">
    <label className="delivery-search"><span aria-hidden="true">⌕</span><input aria-label="筛选交付文件" placeholder="查找文件或文件夹…" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className="delivery-tree-heading"><span>项目文件</span><span className={`delivery-connection ${files.status}`} role="status"><i />{files.status === 'live' ? '实时同步' : files.status === 'connecting' ? '正在加载' : '正在重新连接'}</span></div>
    <nav aria-label="交付文件目录"><ul>{branch('', 0)}</ul></nav>
    {files.status === 'live' && !files.entries.length && <div className="delivery-empty">还没有交付文件<small>团队创建文件后会自动出现在这里。</small></div>}
    {!!files.entries.length && !shown.size && <p className="delivery-empty">没有匹配的文件或文件夹</p>}
    {files.truncated && <p className="delivery-note">目录超过 10,000 项，当前仅显示前 10,000 项。</p>}
  </div>;
}

function formatSize(size: number) {
  return size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function DeliveryPreview({ project, path, revision, status, onClose, onOpen }: { project: string; path: string; revision: string; status: DeliveryState['status']; onClose: () => void; onOpen: (path: string) => void }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(true);
  const [resourceVersion, setResourceVersion] = useState('');
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeButton.current?.focus(); }, [path]);
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setRefreshing(true);
    void fetch(`${deliveryUrl(project, 'preview')}?path=${encodeURIComponent(path)}`, { signal: controller.signal, cache: 'no-store' }).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '文件暂时无法读取');
      if (current) { setPreview(data); setError(''); setResourceVersion(revision); }
    }).catch(cause => {
      if (current) { setPreview(null); setError(cause instanceof Error ? cause.message : '文件暂时无法读取'); }
    }).finally(() => { if (current) setRefreshing(false); });
    return () => { current = false; controller.abort(); };
  }, [project, path, revision]);
  const resource = `${deliveryUrl(project, 'file')}?path=${encodeURIComponent(path)}&revision=${encodeURIComponent(resourceVersion)}`;
  function localPath(href: string | undefined) {
    if (!href || /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(href)) return null;
    try {
      const base = new URL(path.split('/').map(encodeURIComponent).join('/'), 'https://workspace.invalid/');
      const resolved = new URL(href, base);
      return decodeURIComponent(resolved.pathname.slice(1));
    } catch { return null; }
  }
  return <section className="delivery-overlay" role="dialog" aria-label={`文件预览：${path}`} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
    <header className="delivery-preview-heading"><div><span className="delivery-preview-eyebrow">交付文档</span><strong title={path}>{path.split('/').pop()}</strong><small title={path}>{path}</small></div><button ref={closeButton} onClick={onClose} aria-label="关闭文件预览" title="关闭 · Esc">×</button></header>
    <div className="delivery-preview-toolbar"><span role="status">{refreshing ? '正在更新…' : status === 'live' ? '已同步最新文件' : '连接恢复后自动更新'}{preview && ` · ${formatSize(preview.size)}`}</span><a href={`${resource}&download=true`}>下载文件 ↗</a></div>
    <div className="delivery-preview-body">
      {error && <div className="delivery-preview-empty" role="alert"><strong>{error}</strong><p>目录会继续同步，文件重新出现后会自动打开。</p></div>}
      {!preview && !error && <div className="delivery-preview-empty">正在打开文件…</div>}
      {preview?.kind === 'markdown' && <div className="markdown-body delivery-markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} skipHtml components={{
        a: ({ href, children }) => { const local = localPath(href); return local ? <a href="#" onClick={event => { event.preventDefault(); onOpen(local); }}>{children}</a> : <a href={href} target={href?.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a>; },
        img: ({ src, alt }) => { const local = localPath(typeof src === 'string' ? src : undefined); return <img alt={alt || ''} src={local ? `${deliveryUrl(project, 'file')}?path=${encodeURIComponent(local)}&revision=${encodeURIComponent(resourceVersion)}` : src} loading="lazy" />; },
      }}>{preview.content}</ReactMarkdown></div>}
      {preview?.kind === 'text' && <pre className="delivery-source"><code>{preview.content}</code></pre>}
      {preview?.kind === 'image' && <div className="delivery-image"><img src={resource} alt={path} /></div>}
      {preview?.kind === 'pdf' && <iframe className="delivery-pdf" title={path} src={resource} />}
      {preview?.kind === 'unsupported' && <div className="delivery-preview-empty"><strong>此文件暂不支持在线预览</strong><p>可以下载后使用对应应用打开。</p><a href={`${resource}&download=true`}>下载 {path.split('/').pop()}</a></div>}
      {preview?.truncated && <p className="delivery-note">文件较大，当前显示前 2 MB 内容。下载可查看完整文件。</p>}
    </div>
  </section>;
}
