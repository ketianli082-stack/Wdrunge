import { useEffect, useRef, useState, type FormEvent } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import {
  createConversation,
  createProject,
  getConversation,
  getTeamStatus,
  listProjects,
  streamConversationMessage,
  type Activity,
  type ConversationDetail,
  type ProjectSummary,
  type TeamChatMessage,
  type TeamMember,
  type TeamUsage,
} from "./api";
import CollaborationPanel from "./CollaborationPanel";
import { DeliveryPreview, useDeliveryFiles } from "./DeliveryFiles";
import OfficeScene from "./OfficeScene";
import AgentAvatar from "./AgentAvatar";
import MessageContent from "./MessageContent";
import { memberName, teamIdentities } from "./TeamIdentity";
import "./App.css";

type VisibleMessage = TeamChatMessage & {
  read?: boolean;
  readBy?: string[];
  completedBy?: string[];
  time: string;
};
type VisibleActivity = Activity & { time: string };
type ConversationView = Omit<ConversationDetail, "messages" | "activities"> & {
  messages: VisibleMessage[];
  activities: VisibleActivity[];
};
type Panel = "chat" | "activity" | "collaboration";

const initialAgents: TeamMember[] = [
  { id: "leader", name: memberName("leader"), status: "idle", tools: [], skills: [] },
  { id: "product", name: memberName("product"), status: "idle", tools: [], skills: [] },
  { id: "backend", name: memberName("backend"), status: "idle", tools: [], skills: [] },
  { id: "designer", name: memberName("designer"), status: "idle", tools: [], skills: [] },
  { id: "frontend", name: memberName("frontend"), status: "idle", tools: [], skills: [] },
  { id: "testing", name: memberName("testing"), status: "idle", tools: [], skills: [] },
];

function clockTime(value = new Date()) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(value);
}

function toConversationView(detail: ConversationDetail): ConversationView {
  return {
    ...detail,
    messages: detail.messages.map((message) => ({
      id: message.id,
      sender: message.sender,
      content: message.content,
      mention: message.mention,
      mentions: message.mentions,
      read: !!message.mention && message.read_by.includes(message.mention),
      readBy: message.read_by,
      completedBy: message.completed_by,
      time: clockTime(new Date(message.created_at)),
    })),
    activities: detail.activities.map((activity) => ({
      id: activity.id,
      agent: activity.agent,
      tool: activity.tool,
      description: activity.description,
      phase: activity.phase,
      time: clockTime(new Date(activity.created_at)),
    })),
  };
}

function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [active, setActive] = useState<ConversationView | null>(null);
  const [agents, setAgents] = useState<TeamMember[]>(initialAgents);
  const [usage, setUsage] = useState<TeamUsage>({
    total_tokens: 0,
    agents: initialAgents.map((agent) => ({ id: agent.id, total_tokens: 0, context_tokens: 0, context_window: 131072, context_percent: 0 })),
  });
  const [panel, setPanel] = useState<Panel>("chat");
  const [search, setSearch] = useState("");
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [projectDialog, setProjectDialog] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [workspaceDraft, setWorkspaceDraft] = useState("");
  const [projectError, setProjectError] = useState("");
  const [projectSaving, setProjectSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const chatFeed = useRef<HTMLDivElement | null>(null);

  const activeProject = projects.find((project) => project.id === (active?.project_id || activeProjectId));
  const [openFile, setOpenFile] = useState<{project: string; path: string} | null>(null);
  const deliveryFiles = useDeliveryFiles(activeProject?.id);
  useEffect(() => { setOpenFile(null); }, [activeProject?.id]);
  function showFile(path: string) {
    if (activeProject) {
      setOpenFile({project: activeProject.id, path});
      if (window.innerWidth <= 880) document.querySelector('.office')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
  const nameOf = (id: string) => memberName(id);

  async function openConversation(id: string) {
    const detail = await getConversation(id);
    setActive(toConversationView(detail));
    setActiveProjectId(detail.project_id);
    setPanel("chat");
    setInput("");
    setError("");
  }

  async function refreshProjects(preferredConversationId?: string) {
    const items = await listProjects();
    setProjects(items);
    if (preferredConversationId) {
      await openConversation(preferredConversationId);
      return;
    }
    if (active && items.some((project) => project.conversations.some((conversation) => conversation.id === active.id))) return;
    const firstProject = items[0];
    setActiveProjectId(firstProject?.id || null);
    if (firstProject?.conversations[0]) await openConversation(firstProject.conversations[0].id);
    else setActive(null);
  }

  useEffect(() => {
    const abort = new AbortController();
    Promise.all([
      getTeamStatus(abort.signal).then((team) => {
        setAgents(team.agents);
        if (team.usage) setUsage(team.usage);
      }),
      listProjects(abort.signal).then(async (items) => {
        setProjects(items);
        const first = items[0];
        setActiveProjectId(first?.id || null);
        if (first?.conversations[0]) {
          const detail = await getConversation(first.conversations[0].id, abort.signal);
          setActive(toConversationView(detail));
        }
      }),
    ]).catch((cause) => {
      if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "加载项目失败");
    }).finally(() => setLoading(false));
    return () => abort.abort();
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    const timer = setInterval(() => {
      void getTeamStatus(abort.signal).then(team => { if (!abort.signal.aborted) { setAgents(team.agents); setUsage(team.usage); } }).catch(() => {});
      if (active?.id && !streaming) {
        void getConversation(active.id, abort.signal).then(detail => {
          if (!abort.signal.aborted) setActive(current => current?.id === detail.id ? toConversationView(detail) : current);
        }).catch(() => {});
      }
    }, 3000);
    return () => { abort.abort(); clearInterval(timer); };
  }, [active?.id, streaming]);

  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (panel === "chat" && chatFeed.current) chatFeed.current.scrollTop = chatFeed.current.scrollHeight;
  }, [active?.id, active?.messages, panel]);

  function updateActive(update: (item: ConversationView) => ConversationView) {
    setActive((current) => current ? update(current) : current);
  }

  async function startConversation(projectId = activeProjectId) {
    if (streaming) return;
    if (!projectId) {
      setProjectDialog(true);
      return;
    }
    try {
      setError("");
      const created = await createConversation(projectId);
      await refreshProjects(created.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新建对话失败");
    }
  }

  async function saveProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = workspaceDraft.trim();
    if (!path || projectSaving) return;
    setProjectSaving(true);
    setProjectError("");
    try {
      const project = await createProject(projectName, path);
      const conversation = await createConversation(project.id);
      setProjectDialog(false);
      setProjectName("");
      setWorkspaceDraft("");
      await refreshProjects(conversation.id);
    } catch (cause) {
      setProjectError(cause instanceof Error ? cause.message : "创建项目失败");
    } finally {
      setProjectSaving(false);
    }
  }

  async function chooseWorkspace() {
    if (!isTauri()) return;
    setProjectError("");
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        canCreateDirectories: true,
        title: "选择项目文件夹",
        defaultPath: workspaceDraft || undefined,
      });
      if (typeof selected === "string") setWorkspaceDraft(selected);
    } catch (cause) {
      setProjectError(cause instanceof Error ? cause.message : "无法打开文件夹选择器");
    }
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = input.trim();
    if (!content || streaming || !active) return;
    const conversationId = active.id;
    const clientMessageId = crypto.randomUUID();
    let replyId = "";
    updateActive((item) => ({
      ...item,
      messages: [...item.messages, { id: clientMessageId, sender: "user", content, time: clockTime() }],
    }));
    setInput("");
    setError("");
    setStreaming(true);
    setPanel("chat");
    const abort = new AbortController();
    controller.current = abort;
    try {
      await streamConversationMessage(conversationId, content, clientMessageId, {
        onDelta: (delta, messageId) => {
          replyId = messageId || replyId || `reply-${clientMessageId}`;
          updateActive((item) => ({
            ...item,
            messages: item.messages.some((message) => message.id === replyId)
              ? item.messages.map((message) => message.id === replyId ? { ...message, content: message.content + delta } : message)
              : [...item.messages, { id: replyId, sender: "leader", content: delta, time: clockTime() }],
          }));
        },
        onStatus: ({ id, status }) => setAgents((current) => current.map((agent) => agent.id === id ? { ...agent, status } : agent)),
        onMessage: (message) => updateActive((item) => ({
          ...item,
          messages: [...item.messages, { ...message, readBy: [], completedBy: [], time: clockTime() }],
        })),
        onRead: ({ message_id, reader }) => updateActive((item) => ({
          ...item,
          messages: item.messages.map((message) => message.id === message_id
            ? { ...message, read: message.mention === reader || message.read, readBy: [...new Set([...(message.readBy || []), reader])] }
            : message),
        })),
        onReaction: ({ message_id, worker }) => updateActive((item) => ({
          ...item,
          messages: item.messages.map((message) => message.id === message_id
            ? { ...message, completedBy: [...new Set([...(message.completedBy || []), worker])] }
            : message),
        })),
        onActivity: (activity) => updateActive((item) => ({
          ...item,
          activities: item.activities.some((entry) => entry.id === activity.id)
            ? item.activities.map((entry) => entry.id === activity.id ? { ...entry, ...activity } : entry)
            : [...item.activities, { ...activity, time: clockTime() }],
        })),
        onUsage: setUsage,
        onTitle: (title) => {
          setActive((item) => item?.id === conversationId ? { ...item, title } : item);
          setProjects((items) => items.map((project) => ({
            ...project,
            conversations: project.conversations.map((conversation) =>
              conversation.id === conversationId ? { ...conversation, title } : conversation),
          })));
        },
      }, abort.signal);
    } catch (cause) {
      if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "请求失败");
    } finally {
      controller.current = null;
      setStreaming(false);
      try {
        const [detail, projectItems, team] = await Promise.all([
          getConversation(conversationId), listProjects(), getTeamStatus(),
        ]);
        setActive(toConversationView(detail));
        setProjects(projectItems);
        setAgents(team.agents);
        if (team.usage) setUsage(team.usage);
      } catch { /* Keep the streamed view when the refresh is temporarily unavailable. */ }
    }
  }

  const visibleProjects = projects.map((project) => ({
    ...project,
    conversations: project.name.toLowerCase().includes(search.toLowerCase())
      ? project.conversations
      : project.conversations.filter((conversation) => conversation.title.toLowerCase().includes(search.toLowerCase())),
  })).filter((project) => !search || project.name.toLowerCase().includes(search.toLowerCase()) || project.conversations.length > 0);

  return <main className="workspace">
    <aside className="sidebar" aria-label="项目与聊天历史">
      <div className="sidebar-head">
        <div className="brand"><span className="brand-mark">W</span><strong>Wdrunge</strong></div>
        <button className="new-project" onClick={() => { setProjectError(""); setProjectDialog(true); }} disabled={streaming}><span>＋</span> 新建项目</button>
        <label className="history-search"><span>⌕</span><input aria-label="搜索项目和对话" placeholder="搜索项目或对话" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
      </div>
      <nav className="project-list" aria-label="项目列表">
        {visibleProjects.map((project) => <section className={`project-group ${project.id === activeProject?.id ? "selected" : ""}`} key={project.id}>
          <div className="project-row">
            <button className="project-name" onClick={() => { setActiveProjectId(project.id); if (project.conversations[0]) void openConversation(project.conversations[0].id); else setActive(null); }} title={project.workspace_path}><span className="project-folder">▾</span><span>{project.name}</span></button>
            <button className="project-new-chat" onClick={() => void startConversation(project.id)} disabled={streaming} title="新建对话">＋</button>
          </div>
          <div className="project-conversations">
            {project.conversations.map((conversation) => <button key={conversation.id} className={`history-item ${conversation.id === active?.id ? "selected" : ""}`} onClick={() => void openConversation(conversation.id)} disabled={streaming} title={conversation.title}><span className="history-icon">◻</span><span>{conversation.title}</span></button>)}
            {project.conversations.length === 0 && <button className="empty-project-chat" onClick={() => void startConversation(project.id)}>创建第一段对话</button>}
          </div>
        </section>)}
        {!loading && visibleProjects.length === 0 && <div className="history-empty">{search ? "没有匹配的项目或对话" : "还没有项目"}</div>}
      </nav>
      <div className="sidebar-foot"><span className="foot-mark">W</span><span>Agent 团队<small>{agents.length} 位成员已就位</small></span></div>
    </aside>

    <section className="office" aria-label="Agent 办公室"><div style={{width:'100%',height:'100%'}} inert={!!openFile}><OfficeScene agents={agents} /></div>{openFile && activeProject?.id === openFile.project && <DeliveryPreview key={`${openFile.project}:${openFile.path}`} project={openFile.project} path={openFile.path} revision={deliveryFiles.revision} status={deliveryFiles.status} onClose={() => setOpenFile(null)} onOpen={showFile} />}</section>

    <aside className="right-rail" aria-label="团队交流与工作日志">
      <div className="rail-tabs" role="tablist" aria-label="右侧内容">
        <button role="tab" aria-selected={panel === "chat"} className={panel === "chat" ? "active" : ""} onClick={() => setPanel("chat")}>团队群聊</button>
        <button role="tab" aria-selected={panel === "activity"} className={panel === "activity" ? "active" : ""} onClick={() => setPanel("activity")}>工作日志 <span>{active?.activities.length || 0}</span></button>
        <button role="tab" aria-selected={panel === "collaboration"} className={panel === "collaboration" ? "active" : ""} onClick={() => setPanel("collaboration")}>交付文档</button>
      </div>
      {panel === "collaboration" && activeProject ? <CollaborationPanel key={activeProject.id} project={activeProject.id} files={deliveryFiles} selected={openFile?.path || null} onOpen={showFile} /> : !active ? <section className="chat-panel empty-conversation"><div className="chat-welcome"><span className="welcome-icon">W</span><strong>{activeProject ? activeProject.name : "开始使用 Wdrunge"}</strong><p>{activeProject ? "在此项目中创建一段对话。" : "先创建一个项目，团队会围绕项目工作区持续协作。"}</p><button onClick={() => activeProject ? void startConversation(activeProject.id) : setProjectDialog(true)}>{activeProject ? "新建对话" : "新建项目"}</button></div></section> : panel === "chat" ? <section className="chat-panel" role="tabpanel" aria-label="团队群聊">
        <header className="panel-heading"><div><strong>{active.project_name} · {active.title}</strong><span>{Object.values(teamIdentities).map((member) => member.name).join(" · ")}</span></div><span className="member-count">6 人</span></header>
        <div className="workspace-banner"><span title={active.workspace_path}>项目工作区 · {active.workspace_path}</span></div>
        <div className="chat-feed" ref={chatFeed}>
          {active.messages.length === 0 && <div className="chat-welcome"><span className="welcome-icon">W</span><strong>开始一段对话</strong><p>向{memberName("leader")}发送消息。团队记录会保存在项目下。</p></div>}
          {active.messages.map((message) => <article className={`chat-message ${message.sender === "user" ? "mine" : ""}`} key={message.id}><AgentAvatar agent={message.sender} /><div className="message-body"><div className="message-meta"><strong>{nameOf(message.sender)}</strong><time>{message.time}</time></div><div className="bubble"><MessageContent message={message} /></div>{!!message.completedBy?.length && <div className="message-reactions" aria-label="任务完成状态">{message.completedBy.map((worker) => <span className="done-reaction" key={worker} title={`${nameOf(worker)}已完成`}><span aria-hidden="true">✓</span> Done · {nameOf(worker)}</span>)}</div>}</div></article>)}
          {streaming && <div className="processing"><i /><i /><i /><span>团队正在处理</span></div>}
        </div>
        <div className="composer-area">{error && <p className="error" role="alert">{error}</p>}<form className="composer" onSubmit={send}><textarea aria-label="发送消息" placeholder="给团队发消息…" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} rows={2} /><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span>{streaming ? <button type="button" onClick={() => controller.current?.abort()} aria-label="停止生成">■</button> : <button type="submit" disabled={!input.trim()} aria-label="发送">↑</button>}</div></form></div>
      </section> : <section className="log-panel" role="tabpanel" aria-label="工作日志">
        <div className="usage-dashboard"><div className="usage-total"><span>累计使用</span><strong>{usage.total_tokens.toLocaleString()}</strong><small>TOKENS</small></div><div className="context-usage"><div className="context-heading">上下文窗口占用</div>{agents.map((agent) => { const item = usage.agents.find((member) => member.id === agent.id); const percent = item?.context_percent ?? 0; return <div className="context-row" key={agent.id} title={`${item?.context_tokens ?? 0} / ${item?.context_window ?? 131072} tokens`}><span title={nameOf(agent.id)}>{nameOf(agent.id)}</span><div className="context-track"><i style={{ width: `${Math.min(percent, 100)}%` }} /></div><strong>{percent.toFixed(1)}%</strong></div>; })}</div></div>
        <header className="panel-heading"><div><strong>工具调用时间线</strong><span>按时间记录 Agent 的工具调用</span></div><span className="member-count">{active.activities.length} 条</span></header>
        <div className="activity-feed">{active.activities.length === 0 ? <div className="activity-empty">暂无工具调用记录</div> : <ol className="timeline">{[...active.activities].reverse().map((item) => <li key={item.id}><span className="timeline-avatar"><AgentAvatar agent={item.agent} /><span className={`timeline-dot ${item.phase}`} /></span><div className="timeline-top"><strong>{nameOf(item.agent)}</strong><time>{item.time}</time></div><p>{item.description}</p><div className="timeline-foot"><code>{item.tool}</code><span className={`activity-phase ${item.phase}`}>{item.phase === "running" ? "进行中" : item.phase === "completed" ? "已完成" : "失败"}</span></div></li>)}</ol>}</div>
      </section>}
    </aside>

    {projectDialog && <div className="workspace-dialog-backdrop" role="presentation"><form className="workspace-dialog" onSubmit={saveProject} role="dialog" aria-modal="true" aria-label="新建项目"><h2>新建项目</h2><p>项目对应一个本机工作区，并可以包含多段独立对话。</p><label htmlFor="project-name">项目名称</label><input id="project-name" autoFocus placeholder="默认使用文件夹名称" value={projectName} onChange={(event) => setProjectName(event.target.value)} /><label htmlFor="workspace-path">工作区目录</label><div className="workspace-picker"><input id="workspace-path" placeholder="选择或输入项目文件夹" value={workspaceDraft} onChange={(event) => setWorkspaceDraft(event.target.value)} /><button type="button" onClick={() => void chooseWorkspace()} disabled={!isTauri() || projectSaving}>{isTauri() ? "选择文件夹…" : "浏览器不支持选择"}</button></div>{!isTauri() && <small className="workspace-browser-hint">浏览器无法读取文件夹的真实绝对路径，请手动输入；桌面版可直接选择。</small>}{projectError && <p className="workspace-dialog-error" role="alert">{projectError}</p>}<div className="workspace-dialog-actions"><button type="button" onClick={() => setProjectDialog(false)} disabled={projectSaving}>取消</button><button type="submit" disabled={!workspaceDraft.trim() || projectSaving}>{projectSaving ? "创建中…" : "创建项目"}</button></div></form></div>}
  </main>;
}

export default App;
