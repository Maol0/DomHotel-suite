import { formatDuration } from "../lib/format";
import { useLocale, t } from "../lib/locale";
import { officeBackground } from "../lib/scene";
import { React, Segmented } from "../lib/ui";
import type { AgentView } from "../types";

interface HeroProps {
  agents: AgentView[];
  intervalMs: number;
  onIntervalChange: (ms: number) => void;
  onRefresh: () => void;
  lastUpdated: number | null;
  now: number;
}

const INTERVAL_OPTIONS = [
  { label: "1s", value: 1000 },
  { label: "3s", value: 3000 },
  { label: "10s", value: 10000 },
];

export function Hero({
  agents,
  intervalMs,
  onIntervalChange,
  onRefresh,
  lastUpdated,
  now,
}: HeroProps) {
  const locale = useLocale();
  const running = agents.filter((a) => a.runtime.status === "running").length;
  const idle = agents.filter((a) => a.runtime.status === "idle").length;
  const disabled = agents.filter(
    (a) => a.runtime.status === "disabled",
  ).length;

  const art = officeBackground();
  const artStyle: React.CSSProperties = art
    ? { backgroundImage: `url(${art})` }
    : { background: "linear-gradient(135deg, #cdeafd, #cdf5ea)" };

  const updatedLabel = lastUpdated
    ? t(locale, "updatedAgo", {
        ago: formatDuration(now - lastUpdated, locale),
      })
    : null;

  return (
    <div className="ao-hero">
      <div className="ao-hero__main">
        <div className="ao-hero__title">
          <span>{t(locale, "heroTitle")}</span>
        </div>
        <div className="ao-hero__subtitle">
          {t(locale, "heroSubtitle")}
        </div>

        <div className="ao-chips">
          <span className="ao-chip ao-chip--running">
            <span
              className="ao-chip__dot"
              style={{ background: "#2dd4a7" }}
            />
            {t(locale, "chipBusy")} <b>{running}</b>
          </span>
          <span className="ao-chip ao-chip--idle">
            <span
              className="ao-chip__dot"
              style={{ background: "#9aa1b2" }}
            />
            {t(locale, "chipIdle")} <b>{idle}</b>
          </span>
          <span className="ao-chip ao-chip--disabled">
            <span
              className="ao-chip__dot"
              style={{ background: "#cbd2dd" }}
            />
            {t(locale, "chipOff")} <b>{disabled}</b>
          </span>
        </div>

        <div className="ao-controls">
          <Segmented
            size="small"
            options={INTERVAL_OPTIONS}
            value={intervalMs}
            onChange={(value: number) => onIntervalChange(value)}
          />
          <button className="ao-btn" onClick={onRefresh} type="button">
            {t(locale, "btnRefresh")}
          </button>
          {updatedLabel ? (
            <span className="ao-updated">{updatedLabel}</span>
          ) : null}
        </div>
      </div>

      <div className="ao-hero__art" style={artStyle} />
    </div>
  );
}
