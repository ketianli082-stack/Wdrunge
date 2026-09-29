import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  getTeamStatus,
  streamChat,
  validateWorkspace,
  type Activity,
  type ChatMessage,
  type TeamChatMessage,
  type TeamMember,
  type TeamUsage,
} from "./api";
import OfficeScene from "./OfficeScene";
import "./App.css";

type VisibleMessage = TeamChatMessage & { read?: boolean; readBy?: string[]; completedBy?: string[]; time: string };
type VisibleActivity = Activity & { time: string };
type Conversation = {
  id: string;
  title: string;
  workspacePath: string;
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
const initials: Record<string, string> = {
  leader: "L", product: "产", backend: "后", designer: "美", frontend: "前", testing: "测", user: "我",
};

function newConversation(): Conversation {
  return { id: crypto.randomUUID(), title: "新对话", workspacePath: "", updatedAt: Date.now(), history: [], messages: [], activities: [] };
}
function loadConversations(): Conversation[] {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) {
      return saved.filter((item) =>
        item && typeof item.id === "string" && Array.isArray(item.history)
        && Array.isArray(item.messages) && Array.isArray(item.activities),
      ).map((item) => ({ ...item, workspacePath: typeof item.workspacePath === "string" ? item.workspacePath : "" }));
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
  const [workspaceDialog, setWorkspaceDialog] = useState<"new" | "attach" | null>(null);
  const [workspaceDraft, setWorkspaceDraft] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [workspaceSaving, setWorkspaceSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const chatFeed = useRef<HTMLDivElement | null>(null);
  const active = conversations.find((item) => item.id === activeId) || conversations[0];
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
    setWorkspaceDraft(active.workspacePath);
    setWorkspaceError("");
    setWorkspaceDialog(active.messages.length === 0 ? "attach" : "new");
  }
  async function saveWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = workspaceDraft.trim();
    if (!path || workspaceSaving || !workspaceDialog) return;
    setWorkspaceSaving(true); setWorkspaceError("");
    try {
      const validated = await validateWorkspace(path);
      if (workspaceDialog === "attach") {
        updateConversation(active.id, (item) => ({ ...item, workspacePath: validated }));
      } else {
        const created = { ...newConversation(), workspacePath: validated };
        setConversations((current) => [created, ...current]);
        setActiveId(created.id);
      }
      setWorkspaceDialog(null);
      setPanel("chat"); setInput(""); setError("");
    } catch (cause) {
      setWorkspaceError(cause instanceof Error ? cause.message : "工作区校验失败");
    } finally {
      setWorkspaceSaving(false);
    }
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
    if (!active.workspacePath) {
      setWorkspaceDraft(""); setWorkspaceError(""); setWorkspaceDialog("attach");
      return;
    }
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
      await streamChat(requestHistory, sessionId, active.workspacePath, {
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
          ...item, messages: [...item.messages, { ...message, readBy: [], completedBy: [], time: clockTime() }],
        })),
        onRead: ({ message_id, reader }) => updateConversation(sessionId, (item) => ({
          ...item,
          messages: item.messages.map((message) => message.id === message_id
            ? { ...message, read: message.mention === reader || message.read, readBy: [...new Set([...(message.readBy || []), reader])] } : message),
        })),
        onReaction: ({ message_id, worker }) => updateConversation(sessionId, (item) => ({
          ...item,
          messages: item.messages.map((message) => message.id === message_id
            ? { ...message, completedBy: [...new Set([...(message.completedBy || []), worker])] } : message),
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
        {visibleConversations.map((item) => <button key={item.id} className={`history-item ${item.id === activeId ? "selected" : ""}`} onClick={() => selectConversation(item.id)} disabled={streaming} title={`${item.title}${item.workspacePath ? ` · ${item.workspacePath}` : " · 未设置工作区"}`}><span className="history-icon">◻</span><span>{item.title}</span></button>)}
        {visibleConversations.length === 0 && <p className="history-empty">没有匹配的对话</p>}
      </nav>
      <div className="sidebar-foot"><span className="foot-mark">W</span><span>Agent 团队<small>{agents.length} 位成员已就位</small></span></div>
    </aside>

    <section className="office" aria-label="Agent 办公室">
      <OfficeScene agents={agents} />
    </section>

    <aside className="right-rail" aria-label="团队交流与工作日志">
      <div className="rail-tabs" role="tablist" aria-label="右侧内容">
        <button role="tab" aria-selected={panel === "chat"} className={panel === "chat" ? "active" : ""} onClick={() => setPanel("chat")}>团队群聊</button>
        <button role="tab" aria-selected={panel === "activity"} className={panel === "activity" ? "active" : ""} onClick={() => setPanel("activity")}>工作日志 <span>{active.activities.length}</span></button>
      </div>
      {panel === "chat" ? <section className="chat-panel" role="tabpanel" aria-label="团队群聊">
        <header className="panel-heading"><div><strong>团队群聊</strong><span>Leader · 产品 · 后端 · 美工 · 前端 · 测试</span></div><span className="member-count">6 人</span></header>
        <div className="workspace-banner"><span title={active.workspacePath}>{active.workspacePath ? `工作区 · ${active.workspacePath}` : "尚未设置工作区"}</span>{(!active.workspacePath || active.history.length === 0) && <button type="button" onClick={() => { setWorkspaceDraft(active.workspacePath); setWorkspaceError(""); setWorkspaceDialog("attach"); }}>{active.workspacePath ? "更改" : "设置工作区"}</button>}</div>
        <div className="chat-feed" ref={chatFeed}>
          {active.messages.length === 0 && <div className="chat-welcome"><span className="welcome-icon">W</span><strong>开始一段对话</strong><p>向 Leader 发送消息。需要协作时，成员会在这里回应。</p></div>}
          {active.messages.map((message) => <article className={`chat-message ${message.sender === "user" ? "mine" : ""}`} key={message.id}>
            <span className={`avatar avatar-${message.sender}`}>{initials[message.sender] || "·"}</span>
            <div className="message-body"><div className="message-meta"><strong>{nameOf(message.sender)}</strong><time>{message.time}</time></div><div className="bubble">
              {message.mentions?.length ? message.mentions.map((mention) => <p className="mention-line" key={mention.worker}>
                <span className="mention-token">@{mention.name}<span className={`read-indicator ${(message.readBy || []).includes(mention.worker) ? "read" : "unread"}`} title={`${mention.name}${(message.readBy || []).includes(mention.worker) ? "已读" : "未读"}`} aria-label={`${mention.name}${(message.readBy || []).includes(mention.worker) ? "已读" : "未读"}`} /></span>{" "}{mention.summary}
              </p>) : message.mention && message.content.startsWith(`@${nameOf(message.mention)}`) ? <p>
                <span className="mention-token">@{nameOf(message.mention)}<span className={`read-indicator ${message.read ? "read" : "unread"}`} title={message.read ? "已读" : "未读"} aria-label={`${nameOf(message.mention)}${message.read ? "已读" : "未读"}`} /></span>{message.content.slice(nameOf(message.mention).length + 1)}
              </p> : <p>{message.content}</p>}
            </div>{!!message.completedBy?.length && <div className="message-reactions" aria-label="任务完成状态">{message.completedBy.map((worker) => <span className="done-reaction" key={worker} title={`${nameOf(worker)}已完成`}><span aria-hidden="true">✓</span> Done · {nameOf(worker)}</span>)}</div>}</div>
          </article>)}
          {streaming && <div className="processing"><i /><i /><i /><span>团队正在处理</span></div>}
        </div>
        <div className="composer-area">{error && <p className="error" role="alert">{error}</p>}<form className="composer" onSubmit={send}><textarea aria-label="发送消息" placeholder={active.workspacePath ? "给团队发消息…" : "请先设置工作区…"} value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} rows={2} /><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span>{streaming ? <button type="button" onClick={() => controller.current?.abort()} aria-label="停止生成">■</button> : <button type="submit" disabled={!input.trim() || !active.workspacePath} aria-label="发送">↑</button>}</div></form></div>
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
    {workspaceDialog && <div className="workspace-dialog-backdrop" role="presentation"><form className="workspace-dialog" onSubmit={saveWorkspace} role="dialog" aria-modal="true" aria-label="设置工作区"><h2>{workspaceDialog === "new" ? "新建对话" : "设置工作区"}</h2><p>输入本机项目文件夹的绝对路径。团队会在这个目录中读取和修改文件。</p><label htmlFor="workspace-path">工作区目录</label><input id="workspace-path" autoFocus placeholder="/Users/you/Codes/project" value={workspaceDraft} onChange={(event) => setWorkspaceDraft(event.target.value)} />{workspaceError && <p className="workspace-dialog-error" role="alert">{workspaceError}</p>}<div className="workspace-dialog-actions"><button type="button" onClick={() => setWorkspaceDialog(null)} disabled={workspaceSaving}>取消</button><button type="submit" disabled={!workspaceDraft.trim() || workspaceSaving}>{workspaceSaving ? "检查中…" : "使用此工作区"}</button></div></form></div>}
  </main>;
}

export default App;
