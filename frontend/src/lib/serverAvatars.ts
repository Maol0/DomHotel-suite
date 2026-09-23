import { setServerAvatars } from "./avatarStore";
import { host } from "./ui";

const PREFIX = "/agent-office";

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra ?? {}) };
  const token = host.getApiToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

interface AvatarMap {
  avatars: Record<string, string>;
}

async function readError(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: string };
    if (data?.detail) return data.detail;
  } catch {
    /* ignore */
  }
  return `HTTP ${res.status}`;
}

/** Fetch all server-stored avatars and refresh the shared cache. */
export async function refreshServerAvatars(): Promise<void> {
  const res = await fetch(host.getApiUrl(`${PREFIX}/avatars`), {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readError(res));
  const data = (await res.json()) as AvatarMap;
  setServerAvatars(data.avatars ?? {});
}

/** Upload (or replace) an agent's avatar, then refresh the cache. */
export async function putServerAvatar(
  agentId: string,
  dataUrl: string,
): Promise<void> {
  const res = await fetch(
    host.getApiUrl(`${PREFIX}/avatars/${encodeURIComponent(agentId)}`),
    {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ data_url: dataUrl }),
    },
  );
  if (!res.ok) throw new Error(await readError(res));
  const data = (await res.json()) as AvatarMap;
  setServerAvatars(data.avatars ?? {});
}

/** Delete an agent's avatar, then refresh the cache. */
export async function deleteServerAvatar(agentId: string): Promise<void> {
  const res = await fetch(
    host.getApiUrl(`${PREFIX}/avatars/${encodeURIComponent(agentId)}`),
    { method: "DELETE", headers: authHeaders() },
  );
  if (!res.ok) throw new Error(await readError(res));
  const data = (await res.json()) as AvatarMap;
  setServerAvatars(data.avatars ?? {});
}
