import { formatAgo, formatDuration, elapsedSince } from "../lib/format";
import { useLocale } from "../lib/locale";
import { t } from "../lib/locale";
import { React } from "../lib/ui";
import type { AgentView } from "../types";

export function StatusPill({ view }: { view: AgentView }) {
  const locale = useLocale();
  const status = view.runtime.status;
  const cls = `ao-pill ao-pill--${status}`;
  const label =
    status === "running"
      ? t(locale, "statusRunning")
      : status === "idle"
        ? t(locale, "statusIdle")
        : t(locale, "statusDisabled");
  return (
    <span className={cls}>
      <span className="ao-pill__dot" />
      {label}
      {view.stale ? t(locale, "staleHint") : ""}
    </span>
  );
}

export function StatusMeta({ view, now }: { view: AgentView; now: number }) {
  const locale = useLocale();
  const { status, last_run_at, last_finish_at } = view.runtime;

  if (status === "disabled") {
    return <div className="ao-card__meta">{t(locale, "metaResting")}</div>;
  }
  if (status === "running") {
    const elapsed = elapsedSince(last_run_at, now);
    return (
      <div className="ao-card__meta">
        {elapsed !== null
          ? t(locale, "metaBusyFor", { dur: formatDuration(elapsed, locale) })
          : t(locale, "metaWorking")}
      </div>
    );
  }
  return (
    <div className="ao-card__meta">
      {last_finish_at
        ? t(locale, "metaFinished", {
            ago: formatDuration(now - Date.parse(last_finish_at), locale),
          })
        : t(locale, "metaWaiting")}
    </div>
  );
}
