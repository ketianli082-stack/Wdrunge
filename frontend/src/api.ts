import type {
  ChatMessage,
  ChatRequest,
  ChatStreamApiChatStreamPostData,
  TeamMemberResponse,
  TeamStatusResponse,
  TeamUsageResponse,
} from "./generated";

export type { ChatMessage };
export type TeamMember = TeamMemberResponse;
export type TeamUsage = TeamUsageResponse;
export type TeamMention = {
  worker: string;
  name: string;
  summary: string;
};
export type TeamChatMessage = {
  id: string;
  sender: string;
  content: string;
  mention?: string;
  mentions?: TeamMention[];
};
export type ReadReceipt = {
  message_id: string;
  reader: string;
};
export type MessageReaction = {
  message_id: string;
  worker: string;
  reaction: "done";
};
export type Activity = {
  id: string;
  agent: string;
  tool: string;
  description: string;
  phase: "running" | "completed" | "failed";
};
export type StreamCallbacks = {
  onDelta: (content: string) => void;
  onStatus?: (status: Pick<TeamMember, "id" | "status">) => void;
  onMessage?: (message: TeamChatMessage) => void;
  onRead?: (receipt: ReadReceipt) => void;
  onReaction?: (reaction: MessageReaction) => void;
  onActivity?: (activity: Activity) => void;
  onUsage?: (usage: TeamUsage) => void;
};

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/$/, "");
const CHAT_URL: ChatStreamApiChatStreamPostData["url"] = "/api/chat/stream";

export async function getTeamStatus(signal?: AbortSignal): Promise<TeamStatusResponse> {
  const response = await fetch(`${API_BASE_URL}/api/team`, { signal });
  if (!response.ok) throw new Error(`Team status failed (${response.status})`);
  return (await response.json()) as TeamStatusResponse;
}

export async function streamChat(
  messages: ChatMessage[],
  conversationId: string,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const body: ChatRequest = { messages, conversation_id: conversationId };
  const response = await fetch(`${API_BASE_URL}${CHAT_URL}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const data = (await response.json()) as { detail?: string };
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      // Keep the status message when the server did not return JSON.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("The server did not return a stream");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let complete = false;

  function dispatch(frame: string) {
    const lines = frame.split("\n");
    const event = lines.find((line) => line.startsWith("event: "))?.slice(7);
    const data = lines
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6))
      .join("\n");
    if (!event || !data) return;

    const payload = JSON.parse(data) as {
      content?: string;
      message?: string;
      id?: string;
      status?: TeamMember["status"];
      sender?: string;
      mention?: string;
      mentions?: TeamMention[];
      message_id?: string;
      reader?: string;
      worker?: string;
      reaction?: string;
      agent?: string;
      tool?: string;
      description?: string;
      phase?: Activity["phase"];
      total_tokens?: number;
      agents?: TeamUsage["agents"];
    };
    if (event === "delta" && payload.content) callbacks.onDelta(payload.content);
    if (event === "status" && payload.id && payload.status) {
      callbacks.onStatus?.({ id: payload.id, status: payload.status });
    }
    if (event === "chat_message" && payload.id && payload.sender && payload.content) {
      callbacks.onMessage?.({ id: payload.id, sender: payload.sender, content: payload.content, mention: payload.mention, mentions: payload.mentions });
    }
    if (event === "read_receipt" && payload.message_id && payload.reader) {
      callbacks.onRead?.({ message_id: payload.message_id, reader: payload.reader });
    }
    if (event === "message_reaction" && payload.message_id && payload.worker && payload.reaction === "done") {
      callbacks.onReaction?.({ message_id: payload.message_id, worker: payload.worker, reaction: "done" });
    }
    if (event === "activity" && payload.id && payload.agent && payload.tool && payload.description && payload.phase) {
      callbacks.onActivity?.({ id: payload.id, agent: payload.agent, tool: payload.tool, description: payload.description, phase: payload.phase });
    }
    if (event === "usage" && typeof payload.total_tokens === "number" && Array.isArray(payload.agents)) {
      callbacks.onUsage?.({ total_tokens: payload.total_tokens, agents: payload.agents });
    }
    if (event === "error") throw new Error(payload.message || "Agent request failed");
    if (event === "done") complete = true;
  }

  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (!complete) throw new Error("The response ended before completion");
  } finally {
    reader.releaseLock();
  }
}
