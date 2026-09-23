import { useAvatar } from "../hooks/useAvatar";
import { agentColor, avatarInitial } from "../lib/avatar";
import { useLocale, t } from "../lib/locale";
import { screenGifFor } from "../lib/screenGif";
import { React } from "../lib/ui";
import type { AgentView } from "../types";
import { StatusMeta, StatusPill } from "./StatusBadge";

interface AgentDeskProps {
  view: AgentView;
  now: number;
  isManager: boolean;
  incomingFrom: string[];
  onSelect: (view: AgentView) => void;
}

const STATUS_DOT: Record<string, string> = {
  running: "#2dd4a7",
  idle: "#9aa1b2",
  disabled: "#cbd2dd",
};

export function AgentDesk({
  view,
  now,
  isManager,
  incomingFrom,
  onSelect,
}: AgentDeskProps) {
  const locale = useLocale();
  const { status, running_task_count } = view.runtime;
  const image = useAvatar(view.id);
  const color = agentColor(view.id);
  const gif = screenGifFor(status);

  const cardClass = [
    "ao-card",
    `ao-card--${status}`,
    isManager ? "ao-card--manager" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const avatarStyle: React.CSSProperties = image
    ? { backgroundImage: `url(${image})` }
    : { background: color };

  return (
    <div
      className={cardClass}
      data-agent-id={view.id}
      onClick={() => onSelect(view)}
      role="button"
      tabIndex={0}
      title={view.description || view.name}
    >
      {isManager ? <div className="ao-card__crown">👑</div> : null}

      {running_task_count > 0 ? (
        <div className="ao-taskbadge">{running_task_count}</div>
      ) : null}

      {incomingFrom.length > 0 ? (
        <>
          <div className="ao-courier">✉️</div>
          <div className="ao-courier__tag">
            {t(locale, "courierFrom", { names: incomingFrom.join(", ") })}
          </div>
        </>
      ) : null}

      <div className="ao-desk">
        {/* Monitor */}
        <div className="ao-monitor">
          <div className="ao-monitor__screen">
            {gif ? (
              <img className="ao-screen-gif" src={gif} alt="" draggable={false} />
            ) : (
              <div className="ao-screen-content">
                <div className="ao-screen-line" />
                <div className="ao-screen-line" />
                <div className="ao-screen-line" />
              </div>
            )}
          </div>
          <div className="ao-monitor__stand" />
        </div>

        {/* Character on a chair */}
        <div className="ao-char">
          <div className="ao-chair">
            <div className="ao-chair__back" />
            <div className="ao-chair__seat" />
          </div>
          <div className="ao-char__body">
            <div className="ao-char__avatar" style={avatarStyle}>
              {image ? "" : avatarInitial(view.name)}
            </div>
            <div
              className="ao-char__status-dot"
              style={{ background: STATUS_DOT[status] ?? "#cbd2dd" }}
            />
          </div>
        </div>

        {/* Desk surface */}
        <div className="ao-desk__surface" />
      </div>

      <div className="ao-label">
        <div className="ao-label__name-row">
          <span className="ao-card__name">{view.name}</span>
          <StatusPill view={view} />
        </div>
        <StatusMeta view={view} now={now} />
      </div>
    </div>
  );
}
