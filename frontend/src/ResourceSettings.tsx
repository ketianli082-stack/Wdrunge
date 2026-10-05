import { useEffect, useMemo, useState } from 'react';
import { invoke, isTauri } from '@tauri-apps/api/core';
import type { ProjectSummary } from './api';
import { setControlToken, useControlToken } from './controlToken';
import './ResourceSettings.css';

const base = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:17420').replace(/\/$/, '');

type Extra = { key: string; value: string; secret: boolean; has_value?: boolean };
type Connection = {
  id: string; name: string; label: string; type: string; type_label: string; host: string; port: number | null;
  username: string; database: string; scheme: string; env_prefix: string; provision: boolean; can_provision: boolean;
  projects: string[] | null; extra: Extra[]; note: string; has_password: boolean; source: { kind: string; container?: string; image?: string };
};
type TypeInfo = { type: string; label: string; port: number | null; can_provision: boolean };
type Container = { container_id: string; container: string; image: string; type: string; type_label: string; port: number | null; username: string; has_password: boolean; imported: boolean; known_type: boolean };
type Test = { ok: boolean; detail: string };
type Backup = { name: string; engine: string; size: number; created: number };
type ProjectStatus = { project_id: string; data_dir: string; database: string; backups: Backup[];
  resources: { id: string; name: string; label: string; type: string; state: string; error?: string; own_database: boolean; database?: string; user?: string }[] };
type Draft = {
  id?: string; type: string; customType: string; name: string; label: string; host: string; port: string; username: string;
  password: string; clearPassword: boolean; hasPassword: boolean; database: string; scheme: string; envPrefix: string;
  provision: boolean; allProjects: boolean; projects: string[]; extra: Extra[]; note: string;
};

const RELATIONAL = new Set(['mysql', 'mariadb', 'postgresql']);
const CUSTOM = '__custom__';

function emptyDraft(type = 'mysql', types: TypeInfo[] = []): Draft {
  return { type, customType: '', name: type, label: '', host: '127.0.0.1', port: String(types.find(t => t.type === type)?.port ?? ''),
    username: type === 'mysql' || type === 'mariadb' ? 'root' : '', password: '', clearPassword: false, hasPassword: false,
    database: '', scheme: '', envPrefix: '', provision: true, allProjects: true, projects: [], extra: [], note: '' };
}

function fromConnection(connection: Connection, types: TypeInfo[]): Draft {
  const known = types.some(t => t.type === connection.type);
  return { id: connection.id, type: known ? connection.type : CUSTOM, customType: known ? '' : connection.type, name: connection.name,
    label: connection.label, host: connection.host, port: connection.port ? String(connection.port) : '', username: connection.username,
    password: '', clearPassword: false, hasPassword: connection.has_password, database: connection.database, scheme: connection.scheme,
    envPrefix: connection.env_prefix === defaultPrefix(connection.name) ? '' : connection.env_prefix, provision: connection.provision,
    allProjects: connection.projects === null, projects: connection.projects || [], extra: connection.extra.map(e => ({ ...e })), note: connection.note };
}

function defaultPrefix(name: string) {
  const prefix = name.toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return prefix && /^[A-Z]/.test(prefix) ? prefix : `SVC_${prefix}`;
}

function draftType(draft: Draft) { return draft.type === CUSTOM ? draft.customType.trim().toLowerCase() : draft.type; }

function payload(draft: Draft) {
  return { name: draft.name.trim().toLowerCase(), label: draft.label.trim(), type: draftType(draft), host: draft.host.trim(),
    port: draft.port ? Number(draft.port) : null, username: draft.username, password: draft.password ? draft.password : null,
    clear_password: draft.clearPassword, database: draft.database, scheme: draft.scheme, env_prefix: draft.envPrefix,
    provision: draft.provision, projects: draft.allProjects ? null : draft.projects, note: draft.note,
    extra: draft.extra.filter(e => e.key.trim()).map(e => ({ key: e.key.trim().toUpperCase(), value: e.secret && !e.value ? null : e.value, secret: e.secret })) };
}

function errorText(data: any, status: number) {
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map((item: any) => String(item.msg || '').replace(/^Value error, /, '')).join('；');
  return status === 401 ? '授权连接已失效，请重新连接。' : `请求失败（${status}）`;
}

export default function ResourceSettings({ projects, activeProject, onClose }: {
  projects: ProjectSummary[]; activeProject?: ProjectSummary; onClose: () => void;
}) {
  const token = useControlToken();
  const [draftToken, setDraftToken] = useState('');
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [types, setTypes] = useState<TypeInfo[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [formTest, setFormTest] = useState<Test | null>(null);
  const [tests, setTests] = useState<Record<string, Test | 'running'>>({});
  const [docker, setDocker] = useState<{ containers: Container[]; unavailable?: string } | null>(null);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmRestore, setConfirmRestore] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function call<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${base}/api/resources${path}`, {
        method: init?.method || 'GET',
        headers: { Authorization: `Bearer ${token}`, ...(init?.body ? { 'Content-Type': 'application/json' } : {}) },
        body: init?.body ? JSON.stringify(init.body) : undefined,
      });
    } catch {
      // WebKit reports any network or CORS failure only as "Load failed".
      throw new Error('无法连接 Wdrunge 后端：请确认后端正在运行（更新后需要重启后端）。');
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && isTauri()) {
      try { const fresh = await invoke<string>('execution_control_token'); if (fresh && fresh !== token) setControlToken(fresh); } catch { /* shown below */ }
    }
    if (!response.ok) throw new Error(errorText(data, response.status));
    return data as T;
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : '操作失败'); } finally { setBusy(false); }
  }

  async function load() {
    const data = await call<{ connections: Connection[]; types: TypeInfo[] }>('');
    setConnections(data.connections); setTypes(data.types);
    if (activeProject) setStatus(await call<ProjectStatus>(`/projects/${encodeURIComponent(activeProject.id)}`).catch(() => null));
  }

  useEffect(() => {
    if (!token && isTauri()) invoke<string>('execution_control_token').then(setControlToken).catch(() => {});
  }, [token]);
  useEffect(() => { if (token) void run(load); }, [token, activeProject?.id]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape' && !draft) onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [draft, onClose]);

  const projectName = useMemo(() => new Map(projects.map(p => [p.id, p.name])), [projects]);
  const draftInfo = draft ? types.find(t => t.type === draftType(draft)) : undefined;
  const envPreview = draft ? (() => {
    const prefix = draft.envPrefix.trim().toUpperCase() || defaultPrefix(draft.name || 'service');
    const names = ['HOST', 'PORT', 'USER', 'PASSWORD', 'DATABASE', 'URL'].map(s => `${prefix}_${s}`);
    const extra = draft.extra.filter(e => e.key.trim()).map(e => e.key.trim().toUpperCase());
    return [...names, ...extra, ...(RELATIONAL.has(draftType(draft)) ? ['DATABASE_URL', 'DB_*'] : [])];
  })() : [];

  function update(patch: Partial<Draft>) { setDraft(current => current ? { ...current, ...patch } : current); setFormTest(null); }
  function chooseType(type: string) {
    if (!draft) return;
    const info = types.find(t => t.type === type);
    const renamed = !draft.id && (draft.name === draftType(draft) || !draft.name);
    update({ type, port: info?.port ? String(info.port) : draft.port, ...(renamed && type !== CUSTOM ? { name: type } : {}),
      ...(!draft.id && !draft.username && (type === 'mysql' || type === 'mariadb') ? { username: 'root' } : {}) });
  }

  async function save() {
    if (!draft) return;
    await run(async () => {
      await call(draft.id ? `/${draft.id}` : '', { method: draft.id ? 'PUT' : 'POST', body: payload(draft) });
      setDraft(null); setFormTest(null);
      await load();
    });
  }

  async function testDraft() {
    if (!draft) return;
    await run(async () => { setFormTest(await call<Test>('/test', { method: 'POST', body: { id: draft.id ?? null, connection: payload(draft) } })); });
  }

  async function testSaved(id: string) {
    setTests(current => ({ ...current, [id]: 'running' }));
    try { const result = await call<Test>('/test', { method: 'POST', body: { id } }); setTests(current => ({ ...current, [id]: result })); }
    catch (cause) { setTests(current => ({ ...current, [id]: { ok: false, detail: cause instanceof Error ? cause.message : '测试失败' } })); }
  }

  async function openDocker() {
    await run(async () => { setDocker(await call<{ containers: Container[]; unavailable?: string }>('/docker')); });
  }

  async function importContainer(container: Container) {
    await run(async () => {
      const saved = await call<Connection & { test?: Test }>('/docker/import', { method: 'POST', body: { container: container.container } });
      if (saved.test) setTests(current => ({ ...current, [saved.id]: saved.test! }));
      setDocker(current => current ? { ...current, containers: current.containers.map(c => c.container === container.container ? { ...c, imported: true } : c) } : current);
      await load();
    });
  }

  async function remove(id: string) {
    await run(async () => { await call(`/${id}`, { method: 'DELETE' }); setConfirmDelete(null); await load(); });
  }

  async function backupNow() {
    if (!activeProject) return;
    await run(async () => {
      const made = await call<Backup>(`/projects/${encodeURIComponent(activeProject.id)}/backups`, { method: 'POST' });
      setNotice(`已备份：${made.name}`);
      await load();
    });
  }

  async function restore(name: string) {
    if (!activeProject) return;
    await run(async () => {
      await call(`/projects/${encodeURIComponent(activeProject.id)}/restore`, { method: 'POST', body: { name } });
      setConfirmRestore(null); setNotice(`已恢复到备份 ${name}`);
      await load();
    });
  }

  async function prepare() {
    if (!activeProject) return;
    await run(async () => { setStatus(await call<ProjectStatus>(`/projects/${encodeURIComponent(activeProject.id)}/prepare`, { method: 'POST' })); });
  }

  return <div className="resource-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget && !draft) onClose(); }}>
    <section className="resource-page" role="dialog" aria-modal="true" aria-labelledby="resource-title">
      <header className="resource-header">
        <div><h2 id="resource-title">服务连接</h2><p>统一配置数据库、缓存等服务的地址和账号，所有项目共用。Agent 只拿到连接变量，看不到密码。</p></div>
        <button className="resource-close" onClick={onClose} aria-label="关闭">×</button>
      </header>

      {!token && <form className="resource-token" onSubmit={event => { event.preventDefault(); setControlToken(draftToken.trim()); }}>
        <p>需要本机授权才能查看和修改连接。{isTauri() ? '正在连接…' : '请从 ~/Library/Application Support/Wdrunge/execution-token 复制授权码。'}</p>
        {!isTauri() && <><input type="password" aria-label="本机授权码" value={draftToken} onChange={e => setDraftToken(e.target.value)} /><button type="submit" disabled={!draftToken.trim()}>连接</button></>}
      </form>}

      {token && !draft && <>
        {activeProject && status && <section className="resource-project" aria-label="当前项目">
          <h3>当前项目：{activeProject.name}</h3>
          {status.database === 'sqlite' && <p>数据库：内置 SQLite（<code>{status.data_dir}/app.db</code>），无需配置。连接 MySQL 后会自动改用 MySQL。</p>}
          {status.resources.map(item => <div key={item.id} className={`resource-state ${item.state}`}>
            <strong>{item.label}</strong>
            {item.state === 'ready' ? <span>可用{item.own_database && item.database ? ` · 项目专用数据库 ${item.database}（账号 ${item.user}）` : ''}</span>
              : <span>未就绪：{item.error || '正在准备'}</span>}
          </div>)}
          <div className="resource-actions">
            {status.resources.some(item => item.state === 'error') && <button disabled={busy} onClick={() => void prepare()}>重试</button>}
            <button disabled={busy} onClick={() => void backupNow()}>立即备份数据库</button>
          </div>
          {!!status.backups?.length && <details className="resource-backups">
            <summary>数据库备份（{status.backups.length}）</summary>
            <small>平台在执行数据库迁移前会自动备份，保留最近 5 份。恢复会用备份替换项目数据库的全部表。</small>
            {status.backups.map(item => <div className="resource-backup-row" key={item.name}>
              <span>{new Date(item.created * 1000).toLocaleString()} · {item.engine === 'sqlite' ? 'SQLite' : 'MySQL'} · {(item.size / 1024).toFixed(1)} KB</span>
              {confirmRestore === item.name
                ? <span><button className="danger" disabled={busy} onClick={() => void restore(item.name)}>确认恢复</button> <button onClick={() => setConfirmRestore(null)}>取消</button></span>
                : <button disabled={busy} onClick={() => setConfirmRestore(item.name)}>恢复</button>}
            </div>)}
          </details>}
          {notice && <p className="resource-test ok" role="status">{notice}</p>}
        </section>}

        <div className="resource-toolbar">
          <button className="primary" disabled={busy} onClick={() => { setFormTest(null); setDraft(emptyDraft('mysql', types)); }}>＋ 新建连接</button>
          <button disabled={busy} onClick={() => void openDocker()}>从 Docker 导入</button>
        </div>

        {docker && <section className="resource-docker" aria-label="Docker 容器">
          <div className="resource-docker-head"><h3>本机 Docker 中运行的服务</h3><button onClick={() => setDocker(null)}>收起</button></div>
          {docker.unavailable && <p>{docker.unavailable}</p>}
          {!docker.unavailable && !docker.containers.length && <p>没有正在运行的容器。</p>}
          {docker.containers.map(container => <div className="resource-docker-row" key={container.container_id}>
            <span><strong>{container.type_label}</strong> · {container.container}<small>{container.image} · {container.port ? `本机端口 ${container.port}` : '没有映射端口'}{container.has_password ? ' · 已找到密码' : ''}</small></span>
            {container.imported ? <em>已导入</em> : <button disabled={busy || !container.port} onClick={() => void importContainer(container)}>导入</button>}
          </div>)}
          <small>导入会读取容器启动时设置的账号和密码，只保存在本机应用数据中。</small>
        </section>}

        {connections && !connections.length && <p className="resource-empty">还没有连接。项目需要数据库时会使用内置 SQLite；要用已有的 MySQL、Redis 等服务，点“新建连接”或“从 Docker 导入”。</p>}
        <div className="resource-list">
          {connections?.map(connection => {
            const test = tests[connection.id];
            return <article className="resource-card" key={connection.id}>
              <div className="resource-card-head"><strong>{connection.label}</strong><span className="resource-type">{connection.type_label}</span></div>
              <p>{connection.host}:{connection.port ?? '—'}{connection.username ? ` · 账号 ${connection.username}` : ''} · 密码{connection.has_password ? '已保存' : '未设置'}
                {connection.source.kind === 'docker' ? ` · 来自 Docker（${connection.source.container}）` : ''}</p>
              <p>{connection.projects === null ? '所有项目可用' : `仅用于：${connection.projects.map(id => projectName.get(id) || '已删除的项目').join('、') || '无'}`}
                {connection.provision ? ' · 每个项目自动创建独立数据库和账号' : ''}</p>
              <p className="resource-vars">名称 <code>{connection.name}</code> · 变量 <code>{connection.env_prefix}_*</code>{connection.extra.map(e => <code key={e.key}>{e.key}</code>)}</p>
              {test && <p className={`resource-test ${test === 'running' ? '' : test.ok ? 'ok' : 'fail'}`} role="status">{test === 'running' ? '正在测试…' : test.detail}</p>}
              <div className="resource-actions">
                <button disabled={busy || test === 'running'} onClick={() => void testSaved(connection.id)}>测试连接</button>
                <button disabled={busy} onClick={() => { setFormTest(null); setDraft(fromConnection(connection, types)); }}>编辑</button>
                {confirmDelete === connection.id
                  ? <><button className="danger" disabled={busy} onClick={() => void remove(connection.id)}>确认删除</button><button onClick={() => setConfirmDelete(null)}>取消</button></>
                  : <button disabled={busy} onClick={() => setConfirmDelete(connection.id)}>删除</button>}
              </div>
              {confirmDelete === connection.id && <small>删除后项目不再拿到这个连接；已经为项目创建的数据库会保留在服务里。</small>}
            </article>;
          })}
        </div>
      </>}

      {token && draft && <form className="resource-form" onSubmit={event => { event.preventDefault(); void save(); }}>
        <h3>{draft.id ? '编辑连接' : '新建连接'}</h3>
        <div className="resource-grid">
          <label>类型<select value={draft.type} onChange={e => chooseType(e.target.value)}>
            {types.map(t => <option key={t.type} value={t.type}>{t.label}</option>)}
            <option value={CUSTOM}>自定义…</option>
          </select></label>
          {draft.type === CUSTOM && <label>自定义类型<input required placeholder="例如 meilisearch" value={draft.customType} onChange={e => update({ customType: e.target.value, ...(!draft.id ? { name: e.target.value.toLowerCase() } : {}) })} /></label>}
          <label>名称<input required placeholder="英文标识，例如 mysql" value={draft.name} onChange={e => update({ name: e.target.value })} /><small>任务和变量名使用它</small></label>
          <label>显示名称<input placeholder={draftInfo?.label || '可选'} value={draft.label} onChange={e => update({ label: e.target.value })} /></label>
          <label>地址<input required value={draft.host} onChange={e => update({ host: e.target.value })} /></label>
          <label>端口<input inputMode="numeric" placeholder={draftInfo?.port ? String(draftInfo.port) : ''} value={draft.port} onChange={e => update({ port: e.target.value.replace(/\D/g, '') })} /></label>
          <label>账号<input autoComplete="off" value={draft.username} onChange={e => update({ username: e.target.value })} /></label>
          <label>密码<input type="password" autoComplete="new-password" placeholder={draft.hasPassword ? '已保存，留空则不修改' : ''} value={draft.password} disabled={draft.clearPassword} onChange={e => update({ password: e.target.value })} />
            {draft.hasPassword && <span className="resource-inline"><input type="checkbox" checked={draft.clearPassword} onChange={e => update({ clearPassword: e.target.checked, password: '' })} />清除已保存的密码</span>}</label>
          {(!draftInfo?.can_provision || !draft.provision) && <label>数据库 / 默认库<input placeholder="可选" value={draft.database} onChange={e => update({ database: e.target.value })} /></label>}
        </div>
        {draftInfo?.can_provision && <label className="resource-check"><input type="checkbox" checked={draft.provision} onChange={e => update({ provision: e.target.checked })} />
          <span>为每个项目自动创建独立的数据库和账号（推荐）<small>平台用上面的账号建库授权，项目只拿到自己数据库的账号；上面的账号需要有建库权限，例如 root。</small></span></label>}

        <fieldset className="resource-projects">
          <legend>适用项目</legend>
          <label className="resource-inline"><input type="radio" checked={draft.allProjects} onChange={() => update({ allProjects: true })} />所有项目（包括以后新建的）</label>
          <label className="resource-inline"><input type="radio" checked={!draft.allProjects} onChange={() => update({ allProjects: false })} />只用于指定项目</label>
          {!draft.allProjects && <div className="resource-project-picks">{projects.map(project => <label className="resource-inline" key={project.id}>
            <input type="checkbox" checked={draft.projects.includes(project.id)} onChange={e => update({ projects: e.target.checked ? [...draft.projects, project.id] : draft.projects.filter(id => id !== project.id) })} />{project.name}</label>)}
            {!projects.length && <small>还没有项目</small>}</div>}
        </fieldset>

        <fieldset className="resource-extra">
          <legend>自定义字段</legend>
          <small>需要额外的配置（如访问密钥、区域）时添加，原样作为环境变量提供给项目。</small>
          {draft.extra.map((item, index) => <div className="resource-extra-row" key={index}>
            <input aria-label="变量名" placeholder="变量名，如 S3_REGION" value={item.key} onChange={e => update({ extra: draft.extra.map((x, i) => i === index ? { ...x, key: e.target.value.toUpperCase() } : x) })} />
            <input aria-label="值" type={item.secret ? 'password' : 'text'} placeholder={item.secret && item.has_value ? '已保存，留空则不修改' : '值'} value={item.value} onChange={e => update({ extra: draft.extra.map((x, i) => i === index ? { ...x, value: e.target.value } : x) })} />
            <label className="resource-inline"><input type="checkbox" checked={item.secret} onChange={e => update({ extra: draft.extra.map((x, i) => i === index ? { ...x, secret: e.target.checked } : x) })} />保密</label>
            <button type="button" aria-label="删除字段" onClick={() => update({ extra: draft.extra.filter((_, i) => i !== index) })}>×</button>
          </div>)}
          <button type="button" onClick={() => update({ extra: [...draft.extra, { key: '', value: '', secret: false }] })}>＋ 添加字段</button>
        </fieldset>

        <details className="resource-advanced">
          <summary>高级选项</summary>
          <div className="resource-grid">
            <label>变量前缀<input placeholder={defaultPrefix(draft.name || 'service')} value={draft.envPrefix} onChange={e => update({ envPrefix: e.target.value.toUpperCase() })} /></label>
            <label>连接协议<input placeholder="自定义类型生成 URL 时使用，如 http" value={draft.scheme} onChange={e => update({ scheme: e.target.value })} /></label>
            <label className="wide">备注<input value={draft.note} onChange={e => update({ note: e.target.value })} /></label>
          </div>
        </details>
        <p className="resource-vars">项目会拿到这些变量：{envPreview.map(name => <code key={name}>{name}</code>)}</p>

        {formTest && <p className={`resource-test ${formTest.ok ? 'ok' : 'fail'}`} role="status">{formTest.detail}</p>}
        <div className="resource-actions">
          <button type="button" disabled={busy} onClick={() => void testDraft()}>测试连接</button>
          <span />
          <button type="button" disabled={busy} onClick={() => { setDraft(null); setFormTest(null); setError(''); }}>取消</button>
          <button type="submit" className="primary" disabled={busy || !draft.name.trim() || !draftType(draft)}>保存</button>
        </div>
      </form>}

      {error && <p className="resource-error" role="alert">{error}</p>}
    </section>
  </div>;
}
