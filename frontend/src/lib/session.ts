import type { ChatSpec } from "../types";

export interface CollabEdge {
  fromAgent: string;
  toAgent: string;
  /** True while the inter-agent task is still running (animated, bright). */
  active: boolean;
}

/**
 * Inter-agent chat sessions are named "<from>:to:<to>:<ts>:<hash>"
 * (see generate_unique_session_id in the QwenPaw core). Parse that shape to
 * recover who dispatched work to whom. Returns null for normal user sessions.
 */
export function parseCollabSession(
  sessionId: string | undefined | null,
): { fromAgent: string; toAgent: string } | null {
  if (!sessionId) return null;
  const parts = sessionId.split(":");
  const toIndex = parts.indexOf("to");
  if (toIndex <= 0 || toIndex + 1 >= parts.length) return null;
  const fromAgent = parts.slice(0, toIndex).join(":");
  const toAgent = parts[toIndex + 1];
  if (!fromAgent || !toAgent) return null;
  return { fromAgent, toAgent };
}

function timeMs(value: string | undefined | null): number {
  if (!value) return 0;
  const t = Date.parse(value);
  return Number.isNaN(t) ? 0 : t;
}

/**
 * Derive "A dispatched to B" edges from a target agent's chats.
 *
 * Inter-agent tasks are often short, so relying on a live "running" status
 * misses most hand-offs. We instead surface any hand-off that is either
 * currently running OR finished within `recentWindowMs`, so a dispatch stays
 * visible long enough to notice.
 */
export function collabEdgesFromChats(
  targetAgentId: string,
  chats: ChatSpec[],
  nowMs: number = Date.now(),
  recentWindowMs: number = 5 * 60 * 1000,
): CollabEdge[] {
  const byKey = new Map<string, CollabEdge>();
  for (const chat of chats) {
    const parsed = parseCollabSession(chat.session_id);
    if (!parsed) continue;
    if (parsed.toAgent !== targetAgentId) continue;

    const running = chat.status === "running";
    const updated = timeMs(chat.updated_at) || timeMs(chat.created_at);
    const recent = updated > 0 && nowMs - updated <= recentWindowMs;
    if (!running && !recent) continue;

    const key = `${parsed.fromAgent}->${parsed.toAgent}`;
    const existing = byKey.get(key);
    if (existing) {
      if (running) existing.active = true;
    } else {
      byKey.set(key, {
        fromAgent: parsed.fromAgent,
        toAgent: parsed.toAgent,
        active: running,
      });
    }
  }
  return Array.from(byKey.values());
}
