import { RECENT_CHATS_LIMIT } from "../constants";
import { getAgentChat, listAgentChats } from "../lib/api";
import { formatAgo, formatDuration } from "../lib/format";
import { useLocale, t } from "../lib/locale";
import { parseCollabSession } from "../lib/session";
import {
  AntText,
  Button,
  Drawer,
  Empty,
  List,
  React,
  Spin,
  Tag,
} from "../lib/ui";
import type { AgentView, ChatMessage, ChatSpec } from "../types";
import { AvatarEditor } from "./AvatarEditor";
import { StatusPill } from "./StatusBadge";

function messageText(message: ChatMessage): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object" && "text" in part) {
          return String((part as { text?: unknown }).text ?? "");
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

interface AgentDrawerProps {
  view: AgentView | null;
  now: number;
  onClose: () => void;
}

export function AgentDrawer({ view, now, onClose }: AgentDrawerProps) {
  const locale = useLocale();
  const [chats, setChats] = React.useState<ChatSpec[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [openChatId, setOpenChatId] = React.useState<string | null>(null);
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = React.useState(false);

  const agentId = view?.id ?? null;

  React.useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setOpenChatId(null);
    setMessages([]);
    listAgentChats(agentId)
      .then((list) => {
        if (cancelled) return;
        const sorted = [...list].sort(
          (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at),
        );
        setChats(sorted.slice(0, RECENT_CHATS_LIMIT));
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const openChat = React.useCallback(
    (chatId: string) => {
      if (!agentId) return;
      setOpenChatId(chatId);
      setMessagesLoading(true);
      setMessages([]);
      getAgentChat(agentId, chatId)
        .then((history) => setMessages(history.messages ?? []))
        .catch(() => setMessages([]))
        .finally(() => setMessagesLoading(false));
    },
    [agentId],
  );

  return (
    <Drawer
      title={view ? view.name : ""}
      open={Boolean(view)}
      onClose={onClose}
      width={460}
    >
      {view ? (
        <div>
          <div style={{ marginBottom: 12 }}>
            <StatusPill view={view} />
            <div style={{ marginTop: 8, color: "#6b7280", fontSize: 13 }}>
              {view.description || t(locale, "noDescription")}
            </div>
          </div>

          <AvatarEditor agentId={view.id} agentName={view.name} />

          {openChatId ? (
            <div>
              <Button
                size="small"
                onClick={() => setOpenChatId(null)}
                style={{ marginBottom: 12 }}
              >
                {t(locale, "backToTasks")}
              </Button>
              {messagesLoading ? (
                <div className="ao-center">
                  <Spin />
                </div>
              ) : messages.length === 0 ? (
                <Empty description={t(locale, "noMessages")} />
              ) : (
                <List
                  size="small"
                  dataSource={messages}
                  renderItem={(msg: ChatMessage) => {
                    const text = messageText(msg);
                    if (!text) return null;
                    const isUser = msg.role === "user";
                    return (
                      <List.Item>
                        <div style={{ width: "100%" }}>
                          <Tag color={isUser ? "blue" : "default"}>
                            {msg.role ?? "?"}
                          </Tag>
                          <div
                            className={
                              "ao-drawer-msg" +
                              (isUser ? " ao-drawer-msg--user" : "")
                            }
                          >
                            {text}
                          </div>
                        </div>
                      </List.Item>
                    );
                  }}
                />
              )}
            </div>
          ) : loading ? (
            <div className="ao-center">
              <Spin />
            </div>
          ) : error ? (
            <div className="ao-error">{error}</div>
          ) : chats.length === 0 ? (
            <Empty description={t(locale, "noTasks")} />
          ) : (
            <List
              size="small"
              dataSource={chats}
              renderItem={(chat: ChatSpec) => {
                const collab = parseCollabSession(chat.session_id);
                const ago = formatDuration(
                  now - Date.parse(chat.updated_at),
                  locale,
                );
                return (
                  <List.Item
                    onClick={() => openChat(chat.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <div style={{ width: "100%" }}>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          gap: 8,
                        }}
                      >
                        <AntText strong ellipsis style={{ maxWidth: 280 }}>
                          {chat.name || t(locale, "unnamedTask")}
                        </AntText>
                        {chat.status === "running" ? (
                          <Tag color="green">{t(locale, "tagRunning")}</Tag>
                        ) : null}
                      </div>
                      <div style={{ fontSize: 12, color: "#6b7280" }}>
                        {collab
                          ? t(locale, "collabFrom", {
                              agent: collab.fromAgent,
                            })
                          : ""}
                        {t(locale, "updatedAt", { ago })}
                      </div>
                    </div>
                  </List.Item>
                );
              }}
            />
          )}
        </div>
      ) : null}
    </Drawer>
  );
}
