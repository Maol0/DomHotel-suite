import type {
  AgentStatus,
  AgentSummary,
  ChatHistory,
  ChatSpec,
} from "../types";
import { host } from "./ui";

function authHeaders(init?: RequestInit): Record<string, string> {
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  const token = host.getApiToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(host.getApiUrl(path), {
    ...init,
    headers: authHeaders(init),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

/** List every configured agent (the roster). */
export async function listAgents(): Promise<AgentSummary[]> {
  const data = await api<{ agents: AgentSummary[] }>("/agents");
  return data.agents ?? [];
}

/** Fetch a single agent's live runtime status. */
export async function getAgentStatus(agentId: string): Promise<AgentStatus> {
  return api<AgentStatus>(`/agents/${encodeURIComponent(agentId)}/agent-status`);
}

/** List an agent's recent chats / tasks. */
export async function listAgentChats(agentId: string): Promise<ChatSpec[]> {
  return api<ChatSpec[]>(`/agents/${encodeURIComponent(agentId)}/chats`);
}

/** Fetch the full message history of a single chat. */
export async function getAgentChat(
  agentId: string,
  chatId: string,
): Promise<ChatHistory> {
  return api<ChatHistory>(
    `/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}`,
  );
}
