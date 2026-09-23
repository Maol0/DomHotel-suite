export const PLUGIN_ID = "agent-office";
export const HOME_PATH = "/plugin/agent-office";

// Polling intervals (ms) selectable from the top bar.
export const REFRESH_INTERVALS = [1000, 3000, 10000] as const;
export const DEFAULT_REFRESH_INTERVAL = 3000;

// When the browser tab is hidden, poll far less frequently to save resources.
export const HIDDEN_REFRESH_INTERVAL = 30000;

// How many recent chats to surface per agent in the detail drawer.
export const RECENT_CHATS_LIMIT = 20;

// A dispatch (A -> B) keeps showing its connecting line for this long after
// the inter-agent task finished, so short hand-offs don't vanish before you
// can see them. Running hand-offs always show regardless of this window.
export const RECENT_COLLAB_WINDOW = 2 * 60 * 1000;
