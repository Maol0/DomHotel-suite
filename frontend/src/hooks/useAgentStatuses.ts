import {
  DEFAULT_REFRESH_INTERVAL,
  HIDDEN_REFRESH_INTERVAL,
  RECENT_COLLAB_WINDOW,
} from "../constants";
import { getAgentStatus, listAgentChats, listAgents } from "../lib/api";
import { collabEdgesFromChats, type CollabEdge } from "../lib/session";
import { React } from "../lib/ui";
import type { AgentStatus, AgentView } from "../types";

const IDLE_STATUS: AgentStatus = {
  status: "idle",
  running_task_count: 0,
  last_run_at: null,
  last_finish_at: null,
};

interface UseAgentStatusesResult {
  agents: AgentView[];
  edges: CollabEdge[];
  loading: boolean;
  error: string | null;
  lastUpdated: number | null;
  refresh: () => void;
}

/**
 * Polls the agent roster and each agent's live runtime status. Failures for a
 * single agent are isolated (that desk is marked stale, the rest keep working).
 * When the tab is hidden, polling drops to a slow heartbeat to save resources.
 */
export function useAgentStatuses(
  intervalMs: number = DEFAULT_REFRESH_INTERVAL,
): UseAgentStatusesResult {
  const [agents, setAgents] = React.useState<AgentView[]>([]);
  const [edges, setEdges] = React.useState<CollabEdge[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = React.useState<number | null>(null);
  const [tick, setTick] = React.useState(0);

  const refresh = React.useCallback(() => setTick((value) => value + 1), []);

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll(): Promise<void> {
      try {
        const roster = await listAgents();
        const views = await Promise.all(
          roster.map(async (agent): Promise<AgentView> => {
            if (!agent.enabled) {
              return {
                ...agent,
                runtime: { ...IDLE_STATUS, status: "disabled" },
                stale: false,
              };
            }
            try {
              const runtime = await getAgentStatus(agent.id);
              return { ...agent, runtime, stale: false };
            } catch {
              return { ...agent, runtime: { ...IDLE_STATUS }, stale: true };
            }
          }),
        );

        // Inter-agent hand-offs are short-lived, so we look at every agent
        // that is running OR was active within the recent window (not just
        // currently-running ones). This keeps the request count bounded to
        // recently-busy desks while still catching finished dispatches.
        const now = Date.now();
        const recentlyActive = (v: AgentView): boolean => {
          if (v.runtime.status === "running") return true;
          const last = Math.max(
            Date.parse(v.runtime.last_finish_at ?? "") || 0,
            Date.parse(v.runtime.last_run_at ?? "") || 0,
          );
          return last > 0 && now - last <= RECENT_COLLAB_WINDOW;
        };
        const targetIds = views.filter(recentlyActive).map((v) => v.id);
        const runningIds = new Set(
          views.filter((v) => v.runtime.status === "running").map((v) => v.id),
        );
        const collected: CollabEdge[] = [];
        await Promise.all(
          targetIds.map(async (id) => {
            try {
              const chats = await listAgentChats(id);
              collected.push(
                ...collabEdgesFromChats(id, chats, now, RECENT_COLLAB_WINDOW),
              );
            } catch {
              // Ignore — courier animation is best-effort.
            }
          }),
        );
        // A hand-off onto a still-running agent is treated as active (animated)
        // even if the chat-level status lags behind the runtime status.
        for (const edge of collected) {
          if (runningIds.has(edge.toAgent)) edge.active = true;
        }

        if (cancelled) return;
        setAgents(views);
        setEdges(collected);
        setError(null);
        setLastUpdated(Date.now());
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) {
          setLoading(false);
          const hidden =
            typeof document !== "undefined" && document.hidden === true;
          const delay = hidden ? HIDDEN_REFRESH_INTERVAL : intervalMs;
          timer = setTimeout(poll, delay);
        }
      }
    }

    setLoading(true);
    poll();

    const onVisible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, tick, refresh]);

  return { agents, edges, loading, error, lastUpdated, refresh };
}
