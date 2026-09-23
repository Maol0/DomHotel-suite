import type * as ReactNS from "react";

declare global {
  interface QwenPawDisposable {
    dispose(): void;
  }

  interface QwenPawHost {
    React: typeof ReactNS;
    ReactDOM?: unknown;
    antd: any;
    antdIcons?: any;
    getApiUrl: (path: string) => string;
    getApiToken: () => string | null;
    fetch?: (path: string, init?: RequestInit) => Promise<Response>;
    /** React hook — returns the current console locale string (e.g. "zh", "en"). */
    useLocale?: () => string;
  }

  interface QwenPawMenuItem {
    id: string;
    location?: "primary.agentScoped" | "primary.settings" | "userMenu";
    parentId?: string;
    before?: string;
    after?: string;
    order?: number;
    label: string | (() => ReactNS.ReactNode);
    icon?: ReactNS.ComponentType<any> | ReactNS.ReactNode;
    route?: string;
    visible?: () => boolean;
    isGroup?: boolean;
    divider?: boolean;
  }

  interface QwenPawRoute {
    id?: string;
    path: string;
    component: ReactNS.ComponentType<any>;
    label?: string;
    icon?: string;
    priority?: number;
  }

  interface QwenPawGlobal {
    host: QwenPawHost;
    menu?: {
      add: (
        pluginId: string,
        item: QwenPawMenuItem | QwenPawMenuItem[],
      ) => QwenPawDisposable;
    };
    route?: {
      add: (
        pluginId: string,
        route: QwenPawRoute | QwenPawRoute[],
      ) => QwenPawDisposable;
    };
    registerRoutes?: (pluginId: string, routes: QwenPawRoute[]) => void;
  }

  interface Window {
    QwenPaw: QwenPawGlobal;
  }
}

export {};
