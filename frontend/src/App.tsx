import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  getTeamStatus,
  streamChat,
  type Activity,
  type ChatMessage,
  type TeamChatMessage,
  type TeamMember,
  type TeamUsage,
} from "./api";
import "./App.css";

type VisibleMessage = TeamChatMessage & { read?: boolean; time: string };
type VisibleActivity = Activity & { time: string };
type Conversation = {
  id: string;
  title: string;
  updatedAt: number;
  history: ChatMessage[];
  messages: VisibleMessage[];
  activities: VisibleActivity[];
};
type Panel = "chat" | "activity";

const STORAGE_KEY = "wdrunge.conversations.v1";
const ACTIVE_KEY = "wdrunge.activeConversation.v1";
const initialAgents: TeamMember[] = [
  { id: "leader", name: "Leader", status: "idle", tools: [], skills: [] },
  { id: "product", name: "产品", status: "idle", tools: [], skills: [] },
  { id: "backend", name: "后端开发", status: "idle", tools: [], skills: [] },
  { id: "designer", name: "美工", status: "idle", tools: [], skills: [] },
  { id: "frontend", name: "前端开发", status: "idle", tools: [], skills: [] },
  { id: "testing", name: "测试", status: "idle", tools: [], skills: [] },
];
const roleNotes: Record<string, string> = {
  leader: "协调团队与汇总进展",
  product: "需求与产品设计",
  backend: "服务与接口实现",
  designer: "视觉与交互设计",
  frontend: "页面与前端接入",
  testing: "验证与质量检查",
};
const initials: Record<string, string> = {
  leader: "L", product: "产", backend: "后", designer: "美", frontend: "前", testing: "测", user: "我",
};

function newConversation(): Conversation {
  return { id: crypto.randomUUID(), title: "新对话", updatedAt: Date.now(), history: [], messages: [], activities: [] };
}
function loadConversations(): Conversation[] {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) {
      return saved.filter((item) =>
        item && typeof item.id === "string" && Array.isArray(item.history)
        && Array.isArray(item.messages) && Array.isArray(item.activities),
      );
    }
  } catch { /* A damaged local draft starts as a fresh conversation. */ }
  return [];
}
const savedConversations = loadConversations();
const initialConversations = savedConversations.length ? savedConversations : [newConversation()];
const savedActiveId = (() => {
  try { return localStorage.getItem(ACTIVE_KEY); } catch { return null; }
})();
const initialActiveId = initialConversations.some((item) => item.id === savedActiveId)
  ? savedActiveId as string : initialConversations[0].id;

function clockTime() {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date());
}
function titleFor(content: string) {
  const oneLine = content.replace(/\s+/g, " ").trim();
  return oneLine.length > 22 ? `${oneLine.slice(0, 22)}…` : oneLine;
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>(initialConversations);
  const [activeId, setActiveId] = useState(initialActiveId);
  const [agents, setAgents] = useState<TeamMember[]>(initialAgents);
  const [usage, setUsage] = useState<TeamUsage>({
    total_tokens: 0,
    agents: initialAgents.map((agent) => ({ id: agent.id, total_tokens: 0, context_tokens: 0, context_window: 131072, context_percent: 0 })),
  });
  const [panel, setPanel] = useState<Panel>("chat");
  const [search, setSearch] = useState("");
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const chatFeed = useRef<HTMLDivElement | null>(null);
  const active = conversations.find((item) => item.id === activeId) || conversations[0];
  const runningCount = agents.filter((agent) => agent.status === "running").length;
  const nameOf = (id: string) => id === "user" ? "你" : agents.find((agent) => agent.id === id)?.name || id;

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
      localStorage.setItem(ACTIVE_KEY, activeId);
    } catch { /* Storage can be unavailable or full; the active session still works. */ }
  }, [conversations, activeId]);
  useEffect(() => {
    const abort = new AbortController();
    getTeamStatus(abort.signal).then((team) => {
      setAgents(team.agents);
      if (team.usage) setUsage(team.usage);
    }).catch(() => {});
    return () => abort.abort();
  }, []);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (panel === "chat" && chatFeed.current) {
      chatFeed.current.scrollTop = chatFeed.current.scrollHeight;
    }
  }, [activeId, active.messages, panel]);

  function updateConversation(id: string, update: (item: Conversation) => Conversation) {
    setConversations((current) => current.map((item) => item.id === id ? update(item) : item));
  }
  function startConversation() {
    if (streaming) return;
    if (active.messages.length === 0) { setPanel("chat"); return; }
    const created = newConversation();
    setConversations((current) => [created, ...current]);
    setActiveId(created.id);
    setPanel("chat"); setInput(""); setError("");
  }
  function selectConversation(id: string) {
    if (streaming) return;
    setActiveId(id);
    setPanel("chat"); setInput(""); setError("");
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = input.trim();
    if (!content || streaming) return;
    const sessionId = active.id;
    const requestHistory: ChatMessage[] = [...active.history, { role: "user", content }];
    const replyId = crypto.randomUUID();
    let reply = "";
    updateConversation(sessionId, (item) => ({
      ...item,
      title: item.history.length ? item.title : titleFor(content),
      updatedAt: Date.now(),
      history: requestHistory,
      messages: [...item.messages, { id: crypto.randomUUID(), sender: "user", content, time: clockTime() }],
    }));
    setInput(""); setError(""); setStreaming(true); setPanel("chat");
    const abort = new AbortController();
    controller.current = abort;
    try {
      await streamChat(requestHistory, sessionId, {
        onDelta: (delta) => {
          reply += delta;
          updateConversation(sessionId, (item) => ({
            ...item,
            messages: item.messages.some((message) => message.id === replyId)
              ? item.messages.map((message) => message.id === replyId ? { ...message, content: message.content + delta } : message)
              : [...item.messages, { id: replyId, sender: "leader", content: delta, time: clockTime() }],
          }));
        },
        onStatus: ({ id, status }) => setAgents((current) => current.map((agent) => agent.id === id ? { ...agent, status } : agent)),
        onMessage: (message) => updateConversation(sessionId, (item) => ({
          ...item, messages: [...item.messages, { ...message, read: false, time: clockTime() }],
        })),
        onRead: ({ message_id, reader }) => updateConversation(sessionId, (item) => ({
          ...item,
          messages: item.messages.map((message) => message.id === message_id && message.mention === reader
            ? { ...message, read: true } : message),
        })),
        onActivity: (activity) => updateConversation(sessionId, (item) => ({
          ...item,
          activities: item.activities.some((entry) => entry.id === activity.id)
            ? item.activities.map((entry) => entry.id === activity.id ? { ...entry, ...activity } : entry)
            : [...item.activities, { ...activity, time: clockTime() }],
        })),
        onUsage: setUsage,
      }, abort.signal);
    } catch (cause) {
      if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "请求失败");
    } finally {
      if (reply) updateConversation(sessionId, (item) => ({
        ...item, history: [...requestHistory, { role: "assistant", content: reply }],
      }));
      controller.current = null;
      setStreaming(false);
      getTeamStatus().then((team) => {
        setAgents(team.agents);
        if (team.usage) setUsage(team.usage);
      }).catch(() => {});
    }
  }

  const visibleConversations = [...conversations]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .filter((item) => item.title.toLowerCase().includes(search.toLowerCase()));

  return <main className="workspace">
    <aside className="sidebar" aria-label="聊天历史">
      <div className="sidebar-head">
        <div className="brand"><span className="brand-mark">W</span><strong>Wdrunge</strong></div>
        <button className="new-chat" onClick={startConversation} disabled={streaming}><span>＋</span> 新建对话</button>
        <label className="history-search"><span>⌕</span><input aria-label="搜索聊天历史" placeholder="搜索对话" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <div className="sidebar-label">最近的对话</div>
      </div>
      <nav className="history-list" aria-label="最近的对话">
        {visibleConversations.map((item) => <button key={item.id} className={`history-item ${item.id === activeId ? "selected" : ""}`} onClick={() => selectConversation(item.id)} disabled={streaming} title={item.title}><span className="history-icon">◻</span><span>{item.title}</span></button>)}
        {visibleConversations.length === 0 && <p className="history-empty">没有匹配的对话</p>}
      </nav>
      <div className="sidebar-foot"><span className="foot-mark">W</span><span>Agent 团队<small>{agents.length} 位成员已就位</small></span></div>
    </aside>

    <section className="office" aria-label="Agent 办公室">
      <header className="office-header"><span>Agent 办公室</span><span className="header-status"><i /> 团队在线</span></header>
      <div className="office-content">
        <div className="page-title"><div><span className="eyebrow">WORKSPACE / OFFICE</span><h1>团队办公室</h1><p>所有成员都在这里。Leader 会根据你的消息安排工作。</p></div><div className="online-count"><strong>{runningCount}</strong><span>正在工作</span></div></div>
        <div className="section-heading"><span>团队工位</span><span>6 位成员</span></div>
        <div className="desks">{agents.map((agent, index) => <AgentCard key={agent.id} agent={agent} note={roleNotes[agent.id]} index={index + 1} />)}</div>
        <div className="office-footer"><span className="footer-icon">☷</span><div><strong>实时工作状态</strong><p>群聊查看团队交流；工作日志查看每次工具调用。</p></div></div>
      </div>
    </section>

    <aside className="right-rail" aria-label="团队交流与工作日志">
      <div className="rail-tabs" role="tablist" aria-label="右侧内容">
        <button role="tab" aria-selected={panel === "chat"} className={panel === "chat" ? "active" : ""} onClick={() => setPanel("chat")}>团队群聊</button>
        <button role="tab" aria-selected={panel === "activity"} className={panel === "activity" ? "active" : ""} onClick={() => setPanel("activity")}>工作日志 <span>{active.activities.length}</span></button>
      </div>
      {panel === "chat" ? <section className="chat-panel" role="tabpanel" aria-label="团队群聊">
        <header className="panel-heading"><div><strong>团队群聊</strong><span>Leader · 产品 · 后端 · 美工 · 前端 · 测试</span></div><span className="member-count">6 人</span></header>
        <div className="chat-feed" ref={chatFeed}>
          {active.messages.length === 0 && <div className="chat-welcome"><span className="welcome-icon">W</span><strong>开始一段对话</strong><p>向 Leader 发送消息。需要协作时，成员会在这里回应。</p></div>}
          {active.messages.map((message) => <article className={`chat-message ${message.sender === "user" ? "mine" : ""}`} key={message.id}>
            <span className={`avatar avatar-${message.sender}`}>{initials[message.sender] || "·"}</span>
            <div className="message-body"><div className="message-meta"><strong>{nameOf(message.sender)}</strong><time>{message.time}</time></div><div className={`bubble ${message.mention ? "mentioned" : ""}`}>
              {message.mention && <span className={`read-indicator ${message.read ? "read" : "unread"}`} title={message.read ? `${nameOf(message.mention)}已读` : `等待${nameOf(message.mention)}阅读`} aria-label={message.read ? `${nameOf(message.mention)}已读` : `${nameOf(message.mention)}未读`} />}
              <p>{message.content}</p>
            </div>{message.mention && <span className="read-caption">{message.read ? "已读" : "未读"}</span>}</div>
          </article>)}
          {streaming && <div className="processing"><i /><i /><i /><span>团队正在处理</span></div>}
        </div>
        <div className="composer-area">{error && <p className="error" role="alert">{error}</p>}<form className="composer" onSubmit={send}><textarea aria-label="发送消息" placeholder="给团队发消息…" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} rows={2} /><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span>{streaming ? <button type="button" onClick={() => controller.current?.abort()} aria-label="停止生成">■</button> : <button type="submit" disabled={!input.trim()} aria-label="发送">↑</button>}</div></form></div>
      </section> : <section className="log-panel" role="tabpanel" aria-label="工作日志">
        <div className="usage-dashboard">
          <div className="usage-total"><span>累计使用</span><strong>{usage.total_tokens.toLocaleString()}</strong><small>TOKENS</small></div>
          <div className="context-usage"><div className="context-heading">上下文窗口占用</div>{agents.map((agent) => {
            const item = usage.agents.find((member) => member.id === agent.id);
            const percent = item?.context_percent ?? 0;
            return <div className="context-row" key={agent.id} title={`${item?.context_tokens ?? 0} / ${item?.context_window ?? 131072} tokens`}><span>{agent.name}</span><div className="context-track"><i style={{ width: `${Math.min(percent, 100)}%` }} /></div><strong>{percent.toFixed(1)}%</strong></div>;
          })}</div>
        </div>
        <header className="panel-heading"><div><strong>工具调用时间线</strong><span>按时间记录 Agent 的工具调用</span></div><span className="member-count">{active.activities.length} 条</span></header>
        <div className="activity-feed">{active.activities.length === 0 ? <div className="activity-empty">暂无工具调用记录</div> : <ol className="timeline">{[...active.activities].reverse().map((item) => <li key={item.id}><span className={`timeline-dot ${item.phase}`} /><div className="timeline-top"><strong>{nameOf(item.agent)}</strong><time>{item.time}</time></div><p>{item.description}</p><div className="timeline-foot"><code>{item.tool}</code><span className={`activity-phase ${item.phase}`}>{item.phase === "running" ? "进行中" : item.phase === "completed" ? "已完成" : "失败"}</span></div></li>)}</ol>}</div>
      </section>}
    </aside>
  </main>;
}

function AgentCard({ agent, note, index }: { agent: TeamMember; note: string; index: number }) {
  return <article className={`agent-card ${agent.status}`}>
    <div className="card-top"><span className="desk-number">DESK / {String(index).padStart(2, "0")}</span><span className={`card-status ${agent.status}`}><i />{agent.status}</span></div>
    <div className="desk-scene" aria-hidden="true"><span className="scene-glow" /><span className="desk-monitor"><i /><i /><i /></span><span className="desk-stand" /><span className="desk-surface"><i className="desk-keyboard" /><i className="desk-mug" /></span></div>
    <div className="card-center"><span className={`card-avatar avatar-${agent.id}`}>{initials[agent.id] || "·"}</span><div><h3>{agent.name}</h3><p>{note}</p></div></div>
  </article>;
}

export default App;
