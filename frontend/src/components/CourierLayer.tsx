import { useLocale, t } from "../lib/locale";
import { React } from "../lib/ui";
import type { CollabEdge } from "../lib/session";
import type { AgentView } from "../types";

interface CourierLayerProps {
  stageRef: React.RefObject<HTMLDivElement>;
  edges: CollabEdge[];
  agents: AgentView[];
}

interface Path {
  id: string;
  d: string;
  len: number;
  labelX: number;
  labelY: number;
  fromName: string;
  toName: string;
  active: boolean;
}

const R = 10; // corner radius

/**
 * Build an orthogonal (right-angle) path from source bottom → channel → target bottom,
 * with rounded corners at each bend.
 */
function orthoPath(
  sx: number, sy: number,
  ex: number, ey: number,
  channelY: number,
): { d: string; len: number; labelX: number; labelY: number } {
  const r = R;
  const goRight = ex >= sx;
  const xDir = goRight ? 1 : -1;
  const hDist = Math.abs(ex - sx);

  // If source and target are very close horizontally, add offset
  if (hDist < r * 3) {
    const offset = 40 * xDir;
    const midX = sx + offset;
    const d = [
      `M ${sx} ${sy}`,
      `L ${sx} ${channelY - r}`,
      `Q ${sx} ${channelY} ${sx + r * Math.sign(offset)} ${channelY}`,
      `L ${midX - r * Math.sign(offset)} ${channelY}`,
      `Q ${midX} ${channelY} ${midX} ${channelY - r}`,
      `L ${midX} ${ey + r}`,
      `Q ${midX} ${ey} ${midX - r * Math.sign(offset - (ex - sx))} ${ey}`,
      `L ${ex} ${ey}`,
    ].join(" ");
    const vDown = channelY - sy;
    const hLen = Math.abs(midX - sx);
    const vUp = channelY - ey;
    return { d, len: vDown + hLen + vUp + Math.abs(midX - ex), labelX: (sx + midX) / 2, labelY: channelY - 14 };
  }

  const d = [
    `M ${sx} ${sy}`,
    // Down to channel
    `L ${sx} ${channelY - r}`,
    `Q ${sx} ${channelY} ${sx + r * xDir} ${channelY}`,
    // Horizontal to target column
    `L ${ex - r * xDir} ${channelY}`,
    `Q ${ex} ${channelY} ${ex} ${channelY - r}`,
    // Up to target
    `L ${ex} ${ey}`,
  ].join(" ");

  const vDown = channelY - sy;
  const hLen = hDist;
  const vUp = channelY - ey;
  const len = vDown + hLen + vUp;
  const labelX = (sx + ex) / 2;
  const labelY = channelY - 14;

  return { d, len, labelX, labelY };
}

export function CourierLayer({ stageRef, edges, agents }: CourierLayerProps) {
  const locale = useLocale();
  const [size, setSize] = React.useState({ w: 0, h: 0 });
  const [paths, setPaths] = React.useState<Path[]>([]);

  const nameById = React.useMemo(() => {
    const m = new Map<string, string>();
    for (const a of agents) m.set(a.id, a.name);
    return m;
  }, [agents]);

  const edgeKey = React.useMemo(
    () => edges.map((e) => `${e.fromAgent}->${e.toAgent}:${e.active ? 1 : 0}`).join("|"),
    [edges],
  );

  const measure = React.useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const base = stage.getBoundingClientRect();
    setSize({ w: base.width, h: base.height });

    const rectOf = (id: string): DOMRect | null => {
      const el = stage.querySelector<HTMLElement>(
        `[data-agent-id="${(window as any).CSS?.escape?.(id) ?? id}"]`,
      );
      return el ? el.getBoundingClientRect() : null;
    };

    // Count edges per target to stagger channel heights
    const targetCount = new Map<string, number>();
    const targetIdx = new Map<string, number>();
    for (const edge of edges) {
      targetCount.set(edge.toAgent, (targetCount.get(edge.toAgent) ?? 0) + 1);
    }

    const next: Path[] = [];
    let i = 0;
    for (const edge of edges) {
      if (edge.fromAgent === edge.toAgent) continue;
      const fr = rectOf(edge.fromAgent);
      const tr = rectOf(edge.toAgent);
      if (!fr || !tr) continue;

      // Source bottom center
      const sx = fr.left - base.left + fr.width / 2;
      const sy = fr.top - base.top + fr.height + 4;
      // Target bottom center
      const ex = tr.left - base.left + tr.width / 2;
      const ey = tr.top - base.top + tr.height + 4;

      // Channel Y: below the tallest card bottom + offset
      const maxBottom = Math.max(sy, ey);
      const idx = targetIdx.get(edge.toAgent) ?? 0;
      targetIdx.set(edge.toAgent, idx + 1);
      const channelY = maxBottom + 20 + idx * 22;

      const result = orthoPath(sx, sy, ex, ey, channelY);

      next.push({
        id: `c${i}-${edge.fromAgent}-${edge.toAgent}`.replace(/[^a-zA-Z0-9_-]/g, "_"),
        d: result.d,
        len: result.len,
        labelX: result.labelX,
        labelY: result.labelY,
        fromName: nameById.get(edge.fromAgent) ?? edge.fromAgent,
        toName: nameById.get(edge.toAgent) ?? edge.toAgent,
        active: edge.active,
      });
      i += 1;
    }
    setPaths(next);
  }, [edges, nameById, stageRef]);

  React.useLayoutEffect(() => {
    measure();
    const stage = stageRef.current;
    if (!stage) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(stage);
    window.addEventListener("resize", measure);
    const tid = window.setTimeout(measure, 250);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
      window.clearTimeout(tid);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [measure, edgeKey]);

  if (paths.length === 0 || size.w === 0) return null;

  return (
    <svg
      className="ao-courier-svg"
      width={size.w}
      height={size.h + 60}
      viewBox={`0 0 ${size.w} ${size.h + 60}`}
      aria-hidden="true"
    >
      <defs>
        <marker
          id="ao-arrow"
          viewBox="0 0 10 10"
          refX="5"
          refY="5"
          markerWidth="8"
          markerHeight="8"
          orient="auto-start-reverse"
        >
          <path d="M 0 1 L 8 5 L 0 9 Z" fill="var(--c-coral)" />
        </marker>
        <marker
          id="ao-arrow-muted"
          viewBox="0 0 10 10"
          refX="5"
          refY="5"
          markerWidth="8"
          markerHeight="8"
          orient="auto-start-reverse"
        >
          <path d="M 0 1 L 8 5 L 0 9 Z" fill="#b0b8c8" />
        </marker>
      </defs>

      {/* White halo for contrast */}
      {paths.map((p) => (
        <path
          key={`${p.id}-bg`}
          d={p.d}
          fill="none"
          stroke="#fff"
          strokeWidth={p.active ? 7 : 5}
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity={0.85}
        />
      ))}

      {/* Flight paths */}
      {paths.map((p) => (
        <path
          key={p.id}
          id={p.id}
          className={"ao-flight" + (p.active ? "" : " ao-flight--recent")}
          d={p.d}
          markerEnd={p.active ? "url(#ao-arrow)" : "url(#ao-arrow-muted)"}
        />
      ))}

      {/* Animated flow dots (active only) */}
      {paths.filter((p) => p.active).map((p, idx) => (
        <circle key={`${p.id}-dot`} className="ao-flight__dot" r="4.5">
          <animateMotion
            dur="2.5s"
            begin={`${idx * 0.6}s`}
            repeatCount="indefinite"
            keyPoints="0;1"
            keyTimes="0;1"
            calcMode="linear"
          >
            <mpath href={`#${p.id}`} />
          </animateMotion>
        </circle>
      ))}

      {/* Labels */}
      {paths.map((p) => (
        <foreignObject
          key={`${p.id}-label`}
          x={p.labelX - 80}
          y={p.labelY - 10}
          width={160}
          height={24}
        >
          <div
            className={"ao-flight__label" + (p.active ? "" : " ao-flight__label--recent")}
            title={t(locale, "courierTitle", {
              from: p.fromName,
              to: p.toName,
              state: p.active ? t(locale, "courierActive") : t(locale, "courierRecent"),
            })}
          >
            <span className="ao-flight__from">{p.fromName}</span>
            <span className="ao-flight__arrow">→</span>
            <span className="ao-flight__to">{p.toName}</span>
          </div>
        </foreignObject>
      ))}
    </svg>
  );
}
