// Flat-illustration palette used for procedural fallback avatars. These match
// the office theme accent colors so empty desks still look on-brand.
const PALETTE = [
  "#4F46E5", // indigo
  "#38BDF8", // sky
  "#FB923C", // orange
  "#10B981", // emerald
  "#A855F7", // violet
  "#F43F5E", // rose
  "#0EA5E9", // light blue
  "#F59E0B", // amber
];

// User-provided avatars: drop "<agentId>.png" into src/assets/agents/.
// They are eagerly imported (and inlined by vite) so no extra request is made.
const AVATAR_MODULES = (import.meta as any).glob(
  "../assets/agents/*.png",
  { eager: true, query: "?url", import: "default" },
) as Record<string, string>;

const AVATAR_BY_ID: Record<string, string> = {};
for (const [path, url] of Object.entries(AVATAR_MODULES)) {
  const file = path.split("/").pop() ?? "";
  const id = file.replace(/\.png$/i, "");
  if (id) AVATAR_BY_ID[id] = url;
}

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash << 5) - hash + value.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

/** A stable accent color for an agent, used by desks and fallback avatars. */
export function agentColor(agentId: string): string {
  return PALETTE[hashString(agentId) % PALETTE.length];
}

/** The image URL for an agent's avatar, or undefined to use the color block. */
export function avatarImage(agentId: string): string | undefined {
  return AVATAR_BY_ID[agentId];
}

/** First grapheme of a name, used inside fallback avatars. */
export function avatarInitial(name: string): string {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "?";
  return Array.from(trimmed)[0].toUpperCase();
}
