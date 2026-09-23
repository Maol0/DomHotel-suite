// Optional office background. Drop "office-bg.png" (or .jpg) into src/assets/.
// When present it is inlined by vite and used as the room backdrop; otherwise
// the CSS gradient floor is used as a fallback.
const BG_MODULES = (import.meta as any).glob(
  "../assets/office-bg.{png,jpg,jpeg,webp}",
  { eager: true, query: "?url", import: "default" },
) as Record<string, string>;

export function officeBackground(): string | undefined {
  const first = Object.values(BG_MODULES)[0];
  return typeof first === "string" ? first : undefined;
}
