# DomHotel 智能酒店套件 · domhotel-suite

> QwenPaw **PawApp** 插件 · 酒店房务一体化工作台（房态看板 + 工单全生命周期 + 前台接待 + 微信客服 + 企业微信通知/智能表格同步 + 4 个部门 AI 助手）

当前版本 **v1.6.3** · 适配 QwenPaw ≥ 2.0.0 · `author: DomHotel Suite Team`

---

## ✨ 它是什么

把「酒店房务日常」整合进一个插件、一套数据、一处写入路径：

| 能力 | 说明 |
|------|------|
| 🧭 HUB 首启向导 | 管理员确认 → 酒店名称 → 楼栋/楼层/房型批量生成房态 → 引导接入企业微信 |
| 🏨 房态工作台 | 楼栋→楼层→房型→房号 四层看板；房态直接开/收工单（联动通知与排程） |
| 📋 工单系统 | 建单 / 派单 / 接单 / 完成 / 挂起，全生命周期统一走 `work_order_svc` |
| 🛎️ 前台接待 | 入住登记 / 退房 / CSV 导出，操作实时联动房态与工单 |
| 💬 微信客服 (KF) | 客人报修/需求 → 自动生成工单 + 内部员工企微通知 + 客人回执；非命令文本由 AI 应答 |
| 🔔 企业微信通知 | 员工个人应用消息卡片 + 部门群播报 + 客人回执，失败自动重试并入队补发 |
| 📊 智能表格同步 | 8 张业务表与企业微信智能表格双向同步 |
| 🤖 4 个 AI 助手 | 前台 / 工程 / 客房 / 客服 智能体一键部署（见 `templates/`） |
| 🔐 4 档权限 | 访客 / 员工 / 经理 / 管理员角色体系 |

## 📦 目录结构

```
domhotel-suite/
├── plugin.json              # PawApp 清单（id/version/entry/权限/设置）
├── backend/                 # 后端（FastAPI + 自研 PawApp 加载）
│   ├── __init__.py          # 插件入口
│   ├── main.py              # 启动钩子（合并路由）
│   ├── data_layer.py        # SQLite(WAL) 原子数据层
│   ├── wecom_*.py           # 企业微信：应用消息/智能表格/客服/群
│   ├── auth.py / security.py / user_identity.py
│   ├── checkin.py / wizard.py
│   ├── v3api.py / v3*_*.py  # 统一 API 网关与兼容桥接
│   └── routes/              # 业务路由（合并进单个 merged_router）
│       ├── work_order_svc.py    # ★ 工单全生命周期唯一写入层
│       ├── work_orders.py       # 工单 HTTP 路由 + 企业微信通知实现
│       ├── rooms.py             # 房态（已统一走 svc）
│       ├── guest_portal / guest_service / kf   # 客人端
│       ├── staff_portal / schedule_mgmt / dispatch
│       ├── admin / auth / export / reports
│       ├── ai_deploy / ai_assistants / wecom_tools / wecom_bot / wecom_diag
│       └── hotel_tools / hotel_ops_tools       # 智能体工具（对话即工作台）
├── ui/                      # 前端（原生 HTML/JS 单页 + 客人 H5 + 员工 H5）
│   ├── index.html           # 主工作台
│   ├── staff-h5.html / guest-*.html
│   └── plugin-entry.js      # PawApp 前端入口
├── templates/               # 4 个部门 AI 助手模板（agent.json + SOUL/AGENTS/PROFILE）
├── docs/                    # 架构 / 部署 / 使用 / 技术文档
├── .env.example             # 全部环境变量样例（无真实凭证）
└── .gitignore
```

## 🚀 快速开始

1. **安装**：把本目录放到 QwenPaw 的插件目录，重启 QwenPaw 加载。后端入口 `backend/__init__.py`，前端 `ui/plugin-entry.js`（见 `plugin.json.entry`）。
2. **配置凭证**：参照 `.env.example` 填入企业微信自建应用 `WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_AGENT_SECRET`，或首启用「配置向导 / 企微面板」写入运行时配置（优先级 运行时 > env > 默认空）。
3. **接入智能表格**：在企微客户端建表，把各表 `doc_id` 配到 `WECOM_DOC_*`（或用 agent tool `wecom_create_docs` 自动建表）。
4. **部署 AI 助手**：面板「一键部署」4 个部门智能体（模板在 `templates/`）。

> ⚠️ 本仓库**不含任何真实数据或凭证**。凭证一律来自环境变量 / 运行时配置。数据层落盘在 `HOTEL_DATA_DIR`（默认 `hotel.db`，WAL 模式），已被 `.gitignore` 排除。

## 🔄 工单通知链路（v1.6.3 重点）

所有工单写操作统一进入 `backend/routes/work_order_svc.py`，通知由 `work_orders._notify_staff_wo_change` 实现，`svc` 在模块加载时通过**注册握手**拿到通知函数（不依赖任何 sys.modules 名称解析）：

```
svc_create / assign / accept / complete / pause
      │
      ├── _notify_change(wo, action, operator)      # 统一通知入口
      │       ├── 个人应用消息卡片 → _resolve_notify_userids → _send_app_message（含重试 / 入队补发）
      │       └── 部门群播报 → 按 target_dept 选发送 bot：
      │             engineering  → hotel-ai-engineering
      │             housekeeping → hotel-ai-housekeeping
      │             frontdesk    → hotel-ai-frontdesk
      │
      └── 客人来源工单 → wecom_kf 回执给客人
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 📚 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构、模块、数据层、通知链路、扩展点
- [docs/DEPLOY.md](docs/DEPLOY.md) — 部署 / 配置 / 运维（含企业微信接入步骤）
- [docs/技术文档.md](docs/技术文档.md) · [docs/使用文档.md](docs/使用文档.md) — 详细技术与使用说明
- [CHANGELOG.md](CHANGELOG.md) — 版本变更
- [VOICE_INPUT_CONFIG.md](VOICE_INPUT_CONFIG.md) — 语音输入配置

## 🔒 安全

- 仓库不含密钥、令牌、真实企业数据。`.gitignore` 排除 `*.env`、`data/`、`*.db*`、`*.bak`、`__pycache__/`、运行时配置 JSON。
- 发布前再次自查：`grep -rnE 'ww[0-9a-f]{10,}' backend` 应无真实值命中。

## 📄 许可

内部套件，未附带开源许可。对外发布前请补充 `LICENSE`。
