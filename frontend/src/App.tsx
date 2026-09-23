import { DEFAULT_REFRESH_INTERVAL } from "./constants";
import { useAgentStatuses } from "./hooks/useAgentStatuses";
import { AgentDrawer } from "./components/AgentDrawer";
import { Hero } from "./components/Hero";
import { OfficeRoom } from "./components/OfficeRoom";
import { useT } from "./lib/locale";
import { refreshServerAvatars } from "./lib/serverAvatars";
import { Empty, React, Spin } from "./lib/ui";
import type { AgentView } from "./types";

export function AppRoot() {
  const t = useT();
  const [intervalMs, setIntervalMs] = React.useState<number>(
    DEFAULT_REFRESH_INTERVAL,
  );
  const { agents, edges, loading, error, lastUpdated, refresh } =
    useAgentStatuses(intervalMs);
  const [selected, setSelected] = React.useState<AgentView | null>(null);

  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  React.useEffect(() => {
    void refreshServerAvatars().catch(() => undefined);
  }, []);

  const selectedLive = selected
    ? (agents.find((a) => a.id === selected.id) ?? selected)
    : null;

  const managerId = agents.length > 0 ? agents[0].id : null;

  return (
    <div className="ao-root">
      <Hero
        agents={agents}
        intervalMs={intervalMs}
        onIntervalChange={setIntervalMs}
        onRefresh={refresh}
        lastUpdated={lastUpdated}
        now={now}
      />

      {loading && agents.length === 0 ? (
        <div className="ao-center">
          <Spin tip={t("loadingAgents")} />
        </div>
      ) : error && agents.length === 0 ? (
        <div className="ao-center">
          <Empty description={t("errorLoadAgents", { err: error })} />
        </div>
      ) : agents.length === 0 ? (
        <div className="ao-center">
          <Empty description={t("noAgents")} />
        </div>
      ) : (
        <OfficeRoom
          agents={agents}
          edges={edges}
          now={now}
          managerId={managerId}
          onSelect={setSelected}
        />
      )}

      <AgentDrawer
        view={selectedLive}
        now={now}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}
