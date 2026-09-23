/**
 * Optional GIF animations for the monitor screen.
 *
 * Drop files into `src/assets/`:
 *   - `screen-running.gif`  — plays when the agent is busy
 *   - `screen-idle.gif`     — plays when the agent is idle
 *
 * If not present, the screen falls back to CSS animated chat-bubble lines.
 * GIFs are inlined into the bundle by Vite (no extra static files needed).
 */

const SCREEN_MODULES = (import.meta as any).glob(
  "../assets/screen-*.{gif,png,webp,apng}",
  { eager: true, query: "?url", import: "default" },
) as Record<string, string>;

function find(keyword: string): string | undefined {
  for (const [path, url] of Object.entries(SCREEN_MODULES)) {
    if (path.includes(keyword)) return url;
  }
  return undefined;
}

export const screenRunningGif = find("running");
export const screenIdleGif = find("idle");

export function screenGifFor(
  status: string,
): string | undefined {
  if (status === "running") return screenRunningGif ?? screenIdleGif;
  return screenIdleGif;
}
