import type { SessionUsage } from './api';
import { formatTokens } from './formatTokens';

const purposeLabels: Record<string, string> = { work: '执行任务', coordination: '协调', summary: '上下文摘要', review: '命令审核', title: '标题' };

type Group = SessionUsage['by_agent'][number];

function cache(group: Group) {
  return group.cache_percent == null ? '—' : `${group.cache_percent.toFixed(1)}%`;
}

function Rows({ title, groups, label }: { title: string; groups: Group[]; label: (key: string | null | undefined) => string }) {
  if (!groups.length) return null;
  return <table className="usage-breakdown"><caption>{title}</caption><thead><tr><th></th><th>调用</th><th>Tokens</th><th>缓存命中</th></tr></thead>
    <tbody>{groups.map(group => <tr key={String(group.key)}><td title={String(group.key ?? '')}>{label(group.key)}</td><td>{group.calls}</td><td>{formatTokens(group.total_tokens)}</td><td>{cache(group)}</td></tr>)}</tbody></table>;
}

export default function SessionUsagePanel({ usage, nameOf, allTokens }: { usage: SessionUsage | null; nameOf: (id: string) => string; allTokens: number }) {
  const totals = usage?.totals;
  return <details className="usage-total session-usage">
    <summary><span>本会话</span><strong title={`${(totals?.total_tokens ?? 0).toLocaleString()} tokens`}>{formatTokens(totals?.total_tokens ?? 0)}</strong><small>TOKENS</small>
      <small className="usage-cache" title={totals?.cache_percent == null ? '模型尚未返回缓存命中数据' : `缓存命中 ${totals.cached_tokens.toLocaleString()} / ${totals.cache_input_tokens.toLocaleString()} 输入 tokens`}>缓存命中 {totals ? cache(totals) : '—'}</small></summary>
    {usage && totals && <div className="session-usage-detail">
      <p>{totals.calls} 次模型调用 · 输入 {formatTokens(totals.input_tokens)} · 输出 {formatTokens(totals.output_tokens)}{totals.latency_ms != null ? ` · 平均 ${(totals.latency_ms / 1000).toFixed(1)} 秒` : ''}{totals.retries ? ` · 重试 ${totals.retries} 次` : ''}</p>
      <p>工具调用 {usage.tool_calls.total ?? 0} 次，失败 {usage.tool_calls.failed ?? 0} 次 · 人工确认 {usage.approvals.human ?? 0} 次，自动通过 {(usage.approvals.automatic ?? 0) + (usage.approvals.allow_all ?? 0)} 次{usage.approvals.pending ? `，${usage.approvals.pending} 次待确认` : ''}</p>
      <Rows title="按成员" groups={usage.by_agent} label={key => key === 'reviewer' ? '命令审核模型' : key === 'title' ? '标题模型' : nameOf(key || '')} />
      <Rows title="按用途" groups={usage.by_purpose} label={key => purposeLabels[key || ''] || key || '未标记'} />
      <Rows title="按任务" groups={usage.by_task.slice(0, 8)} label={key => key || '对话中的直接回复'} />
      <small>所有会话累计 {formatTokens(allTokens)} tokens</small>
    </div>}
  </details>;
}
