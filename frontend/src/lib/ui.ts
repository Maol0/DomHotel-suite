import type * as ReactNS from "react";

export const host = window.QwenPaw.host;
export const React: typeof ReactNS = host.React;
export const antd = host.antd;
export const antdIcons = host.antdIcons ?? {};

export const {
  Avatar,
  Badge,
  Button,
  Drawer,
  Empty,
  List,
  Segmented,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  message,
} = antd;

export const { Title, Text: AntText, Paragraph } = Typography;

export const {
  ClockCircleOutlined,
  CheckCircleFilled,
  DesktopOutlined,
  ReloadOutlined,
  TeamOutlined,
  MessageOutlined,
  ThunderboltFilled,
  PauseCircleOutlined,
  StopOutlined,
} = antdIcons;

export function iconNode(Icon?: any): ReactNS.ReactNode {
  return Icon ? React.createElement(Icon) : undefined;
}
