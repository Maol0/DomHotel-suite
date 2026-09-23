import { AVATAR_EVENT, resolveAvatar } from "../lib/avatarStore";
import { React } from "../lib/ui";

/**
 * Resolve an agent's avatar URL and re-render whenever any avatar override
 * changes (upload / preset pick / reset) anywhere in the app.
 */
export function useAvatar(agentId: string): string | undefined {
  const [version, setVersion] = React.useState(0);
  React.useEffect(() => {
    const handler = () => setVersion((v) => v + 1);
    window.addEventListener(AVATAR_EVENT, handler);
    return () => window.removeEventListener(AVATAR_EVENT, handler);
  }, []);
  return React.useMemo(
    () => resolveAvatar(agentId),
    [agentId, version],
  );
}
