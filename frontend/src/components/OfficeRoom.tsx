import { React } from "../lib/ui";
import type { CollabEdge } from "../lib/session";
import type { AgentView } from "../types";
import { AgentDesk } from "./AgentDesk";
import { CourierLayer } from "./CourierLayer";

interface OfficeRoomProps {
  agents: AgentView[];
  edges: CollabEdge[];
  now: number;
  managerId: string | null;
  onSelect: (view: AgentView) => void;
}

export function OfficeRoom({
  agents,
  edges,
  now,
  managerId,
  onSelect,
}: OfficeRoomProps) {
  const incoming = React.useMemo(() => {
    const nameById = new Map(agents.map((a) => [a.id, a.name]));
    const map: Record<string, string[]> = {};
    for (const edge of edges) {
      const list = map[edge.toAgent] ?? (map[edge.toAgent] = []);
      list.push(nameById.get(edge.fromAgent) ?? edge.fromAgent);
    }
    return map;
  }, [agents, edges]);

  const stageRef = React.useRef<HTMLDivElement>(null);

  return (
    <div className="ao-stage" ref={stageRef}>
      {/* Isometric tiled floor background */}
      <div className="ao-floor">
        <div className="ao-floor__tiles" />
      </div>

      <div className="ao-grid">
        {agents.map((view) => (
          <AgentDesk
            key={view.id}
            view={view}
            now={now}
            isManager={view.id === managerId}
            incomingFrom={incoming[view.id] ?? []}
            onSelect={onSelect}
          />
        ))}
      </div>
      <CourierLayer stageRef={stageRef} edges={edges} agents={agents} />
    </div>
  );
}
