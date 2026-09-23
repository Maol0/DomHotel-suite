/**
 * Agent Office i18n — follows QwenPaw console language
 * (``localStorage.language``  /  ``host.useLocale()``).
 */

import { React } from "./ui";

/* ------------------------------------------------------------------ */
/*  Language detection                                                 */
/* ------------------------------------------------------------------ */

const LANGUAGE_KEY = "language";
const LANG_EVENT = "ao-language-change";

function readLang(): string {
  try {
    return localStorage.getItem(LANGUAGE_KEY) || "";
  } catch {
    return "";
  }
}

function installSetItemHook(): void {
  const marker = "__aoLangHook";
  const proto = Storage.prototype as Storage & Record<string, unknown>;
  if (proto[marker]) return;
  const native = proto.setItem;
  proto.setItem = function (key: string, value: string) {
    native.call(this, key, value);
    if (key === LANGUAGE_KEY) {
      window.dispatchEvent(new CustomEvent(LANG_EVENT, { detail: value }));
    }
  };
  proto[marker] = true;
}

export type Locale = "zh" | "en";

function toLocale(raw: string | null | undefined): Locale {
  const base = String(raw || "").trim().split("-")[0].toLowerCase();
  return base === "zh" ? "zh" : "en";
}

/* ------------------------------------------------------------------ */
/*  React hook — re-renders on language change                        */
/* ------------------------------------------------------------------ */

export function useLocale(): Locale {
  const hostLocale = window.QwenPaw?.host?.useLocale;
  if (hostLocale) {
    try {
      return toLocale(hostLocale());
    } catch {
      /* fall through */
    }
  }

  const [locale, setLocale] = React.useState<Locale>(() =>
    toLocale(readLang()),
  );

  React.useEffect(() => {
    installSetItemHook();
    const onCustom = (e: Event) =>
      setLocale(toLocale((e as CustomEvent<string>).detail));
    const onStorage = (e: StorageEvent) => {
      if (e.key === LANGUAGE_KEY) setLocale(toLocale(e.newValue));
    };
    window.addEventListener(LANG_EVENT, onCustom);
    window.addEventListener("storage", onStorage);
    const timer = window.setInterval(
      () => setLocale(toLocale(readLang())),
      500,
    );
    return () => {
      window.removeEventListener(LANG_EVENT, onCustom);
      window.removeEventListener("storage", onStorage);
      window.clearInterval(timer);
    };
  }, []);

  return locale;
}

/** Read locale once (non-reactive, for use outside React). */
export function currentLocale(): Locale {
  return toLocale(readLang());
}

/* ------------------------------------------------------------------ */
/*  Message keys and translations                                     */
/* ------------------------------------------------------------------ */

const messages = {
  en: {
    // Menu / title
    pluginLabel: "Agent Office",
    heroTitle: "Agent Office",
    heroSubtitle:
      "See who's busy, who's slacking, and what just got done ✨",

    // Status chips
    chipBusy: "Busy",
    chipIdle: "Idle",
    chipOff: "Off",

    // Controls
    btnRefresh: "🔄 Refresh",
    updatedAgo: "{ago} ago",

    // Loading / error / empty
    loadingAgents: "Loading agents...",
    errorLoadAgents: "Failed to load agents: {err}",
    noAgents: "No agents yet",

    // Status labels
    statusRunning: "Busy",
    statusIdle: "Idle",
    statusDisabled: "Disabled",
    staleHint: " · offline?",
    metaResting: "Resting",
    metaBusyFor: "Busy for {dur}",
    metaWorking: "Working",
    metaFinished: "Finished {ago} ago",
    metaWaiting: "Waiting for work",

    // Courier
    courierFrom: "from {names}",
    courierTitle: "{from} dispatched to {to}{state}",
    courierActive: " (active)",
    courierRecent: " (recent)",

    // Drawer
    noDescription: "(no description)",
    backToTasks: "← Back to tasks",
    noMessages: "No messages",
    noTasks: "No task history",
    unnamedTask: "Unnamed task",
    tagRunning: "Running",
    collabFrom: "collab · from {agent} · ",
    updatedAt: "updated {ago} ago",

    // Avatar editor
    uploadAvatar: "⬆️ Upload",
    resetAvatar: "Reset",
    avatarUpdated: "Avatar updated (team-shared)",
    avatarReset: "Avatar reset to default",
    avatarFailed: "Failed: {reason}",
    avatarFailedRetry: "please try again",
    selectImage: "Please select an image file",
    imageReadFailed: "Failed to read image",
    avatarHint: "Stored on server — shared across team & browsers.",

    // Preset labels
    presetSupervisor: "Supervisor",
    presetCode: "Code",
    presetResearch: "Research",
    presetWriting: "Writing",
    presetOps: "Ops",

    // Duration / time
    durSec: "{n}s",
    durMin: "{n}m",
    durHourMin: "{h}h {m}m",
    durDayHour: "{d}d {h}h",
    agoLabel: "{dur} ago",
  },

  zh: {
    pluginLabel: "智能体办公室",
    heroTitle: "智能体办公室",
    heroSubtitle: "看看团队里谁在忙、谁在摸鱼，以及刚刚完成了什么 ✨",

    chipBusy: "忙碌",
    chipIdle: "空闲",
    chipOff: "休息",

    btnRefresh: "🔄 刷新",
    updatedAgo: "{ago}前更新",

    loadingAgents: "正在加载智能体...",
    errorLoadAgents: "无法加载智能体：{err}",
    noAgents: "还没有任何智能体",

    statusRunning: "忙碌中",
    statusIdle: "空闲",
    statusDisabled: "已禁用",
    staleHint: " · 离线?",
    metaResting: "休息中",
    metaBusyFor: "已忙 {dur}",
    metaWorking: "正在工作",
    metaFinished: "{ago}前完成",
    metaWaiting: "等待派活",

    courierFrom: "来自 {names}",
    courierTitle: "{from} 派给 {to}{state}",
    courierActive: "（进行中）",
    courierRecent: "（刚刚）",

    noDescription: "（无描述）",
    backToTasks: "← 返回任务列表",
    noMessages: "暂无消息",
    noTasks: "暂无任务记录",
    unnamedTask: "未命名任务",
    tagRunning: "运行中",
    collabFrom: "协作 · 来自 {agent} · ",
    updatedAt: "{ago}前更新",

    uploadAvatar: "⬆️ 上传头像",
    resetAvatar: "恢复默认",
    avatarUpdated: "头像已更新（团队共享）",
    avatarReset: "已恢复默认头像",
    avatarFailed: "操作失败：{reason}",
    avatarFailedRetry: "请稍后再试",
    selectImage: "请选择图片文件",
    imageReadFailed: "图片读取失败",
    avatarHint: "头像存储在服务器，团队统一、换浏览器也保留。",

    presetSupervisor: "主管",
    presetCode: "代码",
    presetResearch: "研究",
    presetWriting: "写作",
    presetOps: "运维",

    durSec: "{n} 秒",
    durMin: "{n} 分钟",
    durHourMin: "{h} 小时 {m} 分",
    durDayHour: "{d} 天 {h} 小时",
    agoLabel: "{dur}前",
  },
} as const;

export type MessageKey = keyof typeof messages.en;

export function t(
  locale: Locale,
  key: MessageKey,
  params?: Record<string, string | number>,
): string {
  let text: string = messages[locale]?.[key] ?? messages.en[key] ?? key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

/**
 * React hook that returns a bound translate function for the current locale.
 * Components call `const t = useT()` then `t("key")` or `t("key", { n: 5 })`.
 */
export function useT(): (
  key: MessageKey,
  params?: Record<string, string | number>,
) => string {
  const locale = useLocale();
  return React.useCallback(
    (key: MessageKey, params?: Record<string, string | number>) =>
      t(locale, key, params),
    [locale],
  );
}
