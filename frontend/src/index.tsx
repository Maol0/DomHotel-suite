import { AppRoot } from "./App";
import { HOME_PATH, PLUGIN_ID } from "./constants";
import { ensurePluginStyles } from "./styles";
import { currentLocale, t } from "./lib/locale";
import { React } from "./lib/ui";
import menuIconUrl from "./assets/menu-icon.png";

const MenuIcon = () =>
  React.createElement("span", {
    style: {
      display: "inline-block",
      width: "1em",
      height: "1em",
      marginRight: "4px",
      verticalAlign: "-0.125em",
      backgroundColor: "currentColor",
      WebkitMaskImage: `url(${menuIconUrl})`,
      WebkitMaskSize: "contain",
      WebkitMaskRepeat: "no-repeat",
      WebkitMaskPosition: "center",
      maskImage: `url(${menuIconUrl})`,
      maskSize: "contain",
      maskRepeat: "no-repeat",
      maskPosition: "center",
    } as React.CSSProperties,
  });

function registerPlugin() {
  ensurePluginStyles();

  const menuItems = [
    {
      id: "agent-office.home",
      label: () => t(currentLocale(), "pluginLabel"),
      icon: MenuIcon,
      route: "agent-office.home",
      order: 900,
    },
  ];

  const routes = [
    {
      id: "agent-office.home",
      path: HOME_PATH,
      component: AppRoot,
    },
  ];

  if (window.QwenPaw.menu?.add && window.QwenPaw.route?.add) {
    window.QwenPaw.menu.add(PLUGIN_ID, menuItems);
    window.QwenPaw.route.add(PLUGIN_ID, routes);
    return;
  }

  window.QwenPaw.registerRoutes?.(PLUGIN_ID, [
    {
      path: HOME_PATH,
      component: AppRoot,
      label: "Agent Office",
      icon: "🏢",
      priority: 40,
    },
  ]);
}

registerPlugin();
