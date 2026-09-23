import { avatarImage as bundledById } from "./avatar";

// Built-in preset avatars: src/assets/presets/<key>.png (inlined by vite).
const PRESET_MODULES = (import.meta as any).glob("../assets/presets/*.png", {
  eager: true,
  query: "?url",
  import: "default",
}) as Record<string, string>;

const PRESETS_BY_KEY: Record<string, string> = {};
for (const [path, url] of Object.entries(PRESET_MODULES)) {
  const key = (path.split("/").pop() ?? "").replace(/\.png$/i, "");
  if (key) PRESETS_BY_KEY[key] = url;
}

import type { Locale, MessageKey } from "./locale";
import { t } from "./locale";

const PRESET_LABEL_KEYS: Record<string, MessageKey> = {
  supervisor: "presetSupervisor",
  code: "presetCode",
  research: "presetResearch",
  writing: "presetWriting",
  ops: "presetOps",
};
const PRESET_ORDER = ["supervisor", "code", "research", "writing", "ops"];

export interface PresetItem {
  key: string;
  label: string;
  url: string;
}

export function listPresets(locale: Locale = "zh"): PresetItem[] {
  const order = (k: string) => {
    const i = PRESET_ORDER.indexOf(k);
    return i < 0 ? 99 : i;
  };
  return Object.keys(PRESETS_BY_KEY)
    .sort((a, b) => order(a) - order(b))
    .map((key) => ({
      key,
      label: PRESET_LABEL_KEYS[key]
        ? t(locale, PRESET_LABEL_KEYS[key])
        : key,
      url: PRESETS_BY_KEY[key],
    }));
}

/** Look up a preset's inlined data URL by key (used when pushing to server). */
export function presetUrl(key: string): string | undefined {
  return PRESETS_BY_KEY[key];
}

export const AVATAR_EVENT = "ao-avatars-changed";

// Server-stored avatars: agentId -> data URL. This is the source of truth so
// avatars are team-wide and "follow the agent" across browsers/machines.
let serverCache: Record<string, string> = {};

/** Replace the in-memory server avatar cache and notify subscribers. */
export function setServerAvatars(map: Record<string, string>): void {
  serverCache = map ?? {};
  window.dispatchEvent(new Event(AVATAR_EVENT));
}

/** Current server avatar map (read-only snapshot). */
export function getServerAvatars(): Record<string, string> {
  return serverCache;
}

/**
 * Resolve the avatar URL for an agent. Priority:
 *   server avatar (shared, set via the backend API)
 *   > bundled file named "<agentId>.png"
 *   > undefined (caller falls back to a color block).
 */
export function resolveAvatar(agentId: string): string | undefined {
  return serverCache[agentId] ?? bundledById(agentId);
}
