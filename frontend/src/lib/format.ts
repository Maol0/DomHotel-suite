import type { Locale } from "./locale";
import { t } from "./locale";

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? null : ts;
}

export function formatDuration(ms: number, locale: Locale = "zh"): string {
  if (ms < 0) ms = 0;
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return t(locale, "durSec", { n: sec });
  const min = Math.floor(sec / 60);
  if (min < 60) return t(locale, "durMin", { n: min });
  const hr = Math.floor(min / 60);
  if (hr < 24) return t(locale, "durHourMin", { h: hr, m: min % 60 });
  const day = Math.floor(hr / 24);
  return t(locale, "durDayHour", { d: day, h: hr % 24 });
}

export function formatAgo(
  value: string | null | undefined,
  now: number = Date.now(),
  locale: Locale = "zh",
): string {
  const ts = parseTime(value);
  if (ts === null) return "—";
  return t(locale, "agoLabel", { dur: formatDuration(now - ts, locale) });
}

export function elapsedSince(
  value: string | null | undefined,
  now: number = Date.now(),
): number | null {
  const ts = parseTime(value);
  if (ts === null) return null;
  return now - ts;
}
