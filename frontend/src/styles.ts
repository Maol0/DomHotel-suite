import pluginStyles from "./styles.css?raw";

const STYLE_ELEMENT_ID = "agent-office-styles";

export function ensurePluginStyles(): void {
  if (document.getElementById(STYLE_ELEMENT_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ELEMENT_ID;
  style.textContent = pluginStyles;
  document.head.appendChild(style);
}
