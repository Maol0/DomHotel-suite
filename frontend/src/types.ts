export type AgentRuntimeStatus = "idle" | "running" | "disabled";

export interface AgentSummary {
  id: string;
  name: string;
  description: string;
  workspace_dir?: string;
  enabled: boolean;
}

export interface AgentStatus {
  status: AgentRuntimeStatus;
  running_task_count: number;
  last_run_at: string | null;
  last_finish_at: string | null;
}

export interface ChatSpec {
  id: string;
  name: string;
  session_id: string;
  user_id: string;
  channel: string;
  created_at: string;
  updated_at: string;
  status: string;
  source?: string;
}

export interface ChatMessage {
  role?: string;
  content?: unknown;
  [key: string]: unknown;
}

export interface ChatHistory {
  messages: ChatMessage[];
  status: string;
}

/** An agent merged with its live runtime status, ready for rendering. */
export interface AgentView extends AgentSummary {
  runtime: AgentStatus;
  /** True when this agent failed to report status in the latest poll. */
  stale: boolean;
}
