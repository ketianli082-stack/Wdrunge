import { useEffect, useState } from 'react';

type Service = {
  name: string; kind: string; port?: number; host?: string; base_url?: string; status?: string; source?: string;
  claimed?: boolean; owner?: string; note?: string; command?: { argv: string[]; cwd: string }; runtime?: Runtime;
};
type Runtime = { state: 'running' | 'listening' | 'stopped'; process_id?: string; kept?: boolean; started_by?: string; stale?: string[]; note?: string };
type ProjectCommand = { name: string; argv: string[]; cwd: string; status: string; purpose?: string };
type Environment = { host: { os: string; userland: string }; reserved_ports: number[]; services: Service[]; commands: ProjectCommand[] };

const statusLabel: Record<string, string> = { verified: '已验证', declared: '已登记' };
const sourceLabel: Record<string, string> = { user: '用户', agent: '成员', manifest: '项目配置', observed: '运行观察' };

function runtimeLabel(runtime?: Runtime) {
  if (!runtime || runtime.state === 'stopped') return '未运行';
  if (runtime.state === 'listening') return '端口有响应（非团队启动）';
  return `运行中${runtime.started_by ? `（${runtime.started_by} 启动${runtime.kept ? '，任务结束后保留' : ''}）` : ''}`
    + (runtime.stale?.length ? ` · 代码已更新，需重启：${runtime.stale.slice(0, 3).join('、')}` : '');
}

export default function EnvironmentPanel({ busy, command, stopService, act }: {
  busy: boolean;
  command: (action: string, payload: object) => Promise<any>;
  stopService: (name: string) => Promise<any>;
  act: (fn: () => Promise<void>) => Promise<void>;
}) {
  const [data, setData] = useState<Environment | null>(null);
  const [name, setName] = useState('');
  const [port, setPort] = useState('');
  const [start, setStart] = useState('');
  const [cwd, setCwd] = useState('.');
  const load = async () => {
    const result = await command('environment', {});
    setData(result && result.host && Array.isArray(result.services) && Array.isArray(result.commands) ? result : null);
  };
  useEffect(() => { void load().catch(() => setData(null)); }, []);
  const save = () => void act(async () => {
    await command('register_service', {
      name: name.trim(), ...(port.trim() ? { port: Number(port) } : {}),
      ...(start.trim() ? { command: start.trim().split(/\s+/), cwd: cwd.trim() || '.' } : {}),
    });
    setName(''); setPort(''); setStart(''); setCwd('.');
    await load();
  });
  return <section aria-label="项目环境" className="environment-panel">
    <h3>项目环境</h3>
    <p className="collaboration-empty">成员按此登记使用端口、地址与启动命令；已登记的端口视为本项目服务，命令审核不再为其询问。</p>
    {data && <>
      <p className="collaboration-empty">主机：{data.host.os}（{data.host.userland}）· 平台保留端口：{data.reserved_ports.join(', ') || '尚未检测'}</p>
      {!data.services.length && <p className="collaboration-empty">尚未登记服务。</p>}
      {data.services.map(s => <article className="collaboration-request" key={s.name}>
        <div><strong>{s.name}</strong><span>{runtimeLabel(s.runtime)} · {statusLabel[s.status || ''] || s.status}{s.claimed ? '' : ' · 端口未确认归属'}</span></div>
        <p>{s.base_url || (s.port ? `${s.kind === 'http' ? 'http://' : ''}${s.host || '127.0.0.1'}:${s.port}` : '未分配端口')}
          <small> · 来源：{sourceLabel[s.source || ''] || s.source}{s.owner ? ` · ${s.owner}` : ''}</small></p>
        {s.command && <p><code>{s.command.argv.join(' ')}</code><small> （目录 {s.command.cwd}）</small></p>}
        {s.note && <small>{s.note}</small>}
        <div className="collaboration-controls">
          {s.runtime?.state === 'running' && <button disabled={busy} onClick={() => void act(async () => {
            await stopService(s.name); await load();
          })}>停止</button>}
          <button disabled={busy} onClick={() => void act(async () => {
            await command('register_service', { name: s.name, remove: true }); await load();
          })}>移除</button></div>
      </article>)}
      {data.commands.length > 0 && <><h3>项目命令</h3>{data.commands.map(c => <p key={c.name}>
        <strong>{c.name}</strong> <code>{c.argv.join(' ')}</code><small> （目录 {c.cwd} · {statusLabel[c.status] || c.status}）</small></p>)}</>}
    </>}
    <details><summary>登记或修改服务</summary>
      <form className="goal-evidence" onSubmit={e => { e.preventDefault(); save(); }}>
        <label>服务名称（如 api、web、db）<input required pattern="[a-z0-9][a-z0-9_.\-]{0,39}" value={name} onChange={e => setName(e.target.value)} /></label>
        <label>端口（留空自动分配空闲端口）<input inputMode="numeric" pattern="\d{1,5}" value={port} onChange={e => setPort(e.target.value)} /></label>
        <label>启动命令（可选）<input value={start} onChange={e => setStart(e.target.value)} placeholder="python3 backend/app.py" /></label>
        <label>启动目录<input value={cwd} onChange={e => setCwd(e.target.value)} /></label>
        <button disabled={busy || !name.trim()}>保存</button>
      </form>
    </details>
  </section>;
}
