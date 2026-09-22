# 架构说明 · domhotel-suite

本文档面向二次开发与运维。使用/部署见 [DEPLOY.md](DEPLOY.md)。

## 1. 运行形态

`domhotel-suite` 是 **QwenPaw PawApp 插件**（`plugin.json.type = "app"`），不是独立进程：

- 后端入口 `backend/__init__.py`；`backend/main.py` 注册 `@app.on_launch` 启动钩子。
- 所有 `routes/*.py` 的 FastAPI endpoint 在加载时**合并进单个 `merged_router`**，由 `register_all(app)` 一次性 `app.include_router` 挂载，统一前缀 `/api/domhotel-suite`。
  - 为什么合并：PawApp 的 `register(api)` 会对每个 sub-router 调一次 `register_http_router`，多 router 会触发「prefix already registered」而加载失败；合并后只注册一次。
- 前端 `ui/plugin-entry.js` + 静态页由 `routes/ui.py` serve。
- 全量约 183 个 endpoint（`@router.get/post/put/delete`）。

## 2. 模块分层

```
┌────────────────────────────────────────────────────────┐
│  ui/ (工作台 index.html · staff-h5 · guest-*)           │
├────────────────────────────────────────────────────────┤
│  routes/  HTTP 层（鉴权依赖 auth.require_*、参数校验）  │
│    work_orders · rooms · guest_portal · staff_portal …  │
├────────────────────────────────────────────────────────┤
│  routes/work_order_svc.py  ★ 业务服务层（唯一写入路径） │
│    svc_create/assign/accept/complete/pause_work_order    │
├────────────────────────────────────────────────────────┤
│  能力模块：                                              │
│    wecom_api / wecom_sync(智能表格) / wecom_kf(客服) /   │
│    appchat(群) · data_layer(SQLite) · auth/security ·    │
│    checkin/wizard · v3api 网关                           │
├────────────────────────────────────────────────────────┤
│  智能体工具 routes/hotel_tools · hotel_ops_tools ·        │
│    wecom_tools（对话即工作台：建房态/开关工单/配企微）    │
└────────────────────────────────────────────────────────┘
```

**设计原则：写路径唯一。** 无论来自 HTTP（前台、房态看板）、客人端（H5 / 微信客服）、还是智能体工具，工单/房态的落库与副作用都收敛到 `work_order_svc`，保证通知、智能表格同步、住客在住保护、耗时统计、pending_actions 联动在所有入口一致。

## 3. 数据层 `data_layer.py`

- SQLite（`hotel.db`，**WAL 模式**），整表写用单事务原子提交，根治 JSON 整文件重写导致的并发丢写/损坏（订单与房态不同步的历史根因）。
- 首次访问自动从旧 JSON 迁移；对外 API 不变。
- 逻辑表：staff / departments / room_types / floors / rooms / work_orders / rooms_log / guests_log / supplies_log / customers / pending / chat_sessions …
- 落盘目录由 `HOTEL_DATA_DIR` 指定（默认 `dompaw-data-backup`），**属运行时数据，绝不入库**。

## 4. 企业微信集成

三条独立通道，各自凭证独立：

| 通道 | 实现 | 用途 | 凭证 |
|------|------|------|------|
| 应用消息 | `wecom_api` + `work_orders._send_app_message` | 员工个人工单卡片（REST `message/send`） | `WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_AGENT_SECRET` |
| 部门群 | `POST /api/messages/send`（X-Agent-Id 指定 bot） | 部门群播报工单 | 该 bot 的企微长连接 |
| 智能表格 | `wecom_sync` | 8 表双向同步 | `WECOM_DOC_*` doc_id |
| 微信客服 | `wecom_kf` | 客人报修/需求回调、AI 应答、回执 | `WECOM_KF_*` |

### 通知链路（v1.6.3 重构）

```
svc_*(wo, operator)
  └─ await _notify_change(wo, action, operator, tag)
        ├─ fn = _NOTIFY_FN            # work_orders 加载时 register_notify 注入
        ├─ _resolve_notify_userids(wo) # 部门 userid 映射，兜底 KF_NOTIFY_USERIDS
        │    └─ _send_staff_notice_resilient → 个人应用消息（重试 3 次，失败入队补发）
        ├─ _notify_staff_group → _get_dept_chatid(dept) → _send_group_notification
        │    └─ 按 target_dept 选发送 bot：engineering→hotel-ai-engineering 等
        └─ 客人来源 → wecom_kf.notify_guest_work_order_change（客人回执）
```

关键修复历史（见 CHANGELOG）：
1. **注册握手**：运行时 `svc.__package__` 指向的父包未注册进 `sys.modules`，按名字/属性/import 都找不到 `work_orders` 模块 → 通知被静默跳过。改为 `work_orders` 在 import 时把 `_notify_staff_wo_change` 注入 `svc._NOTIFY_FN`，彻底绕开名称解析。
2. **部门群发送方**：统一用 `hotel-wecom-assistant` 发群消息「返回成功但群里收不到」（该 bot 不在部门群内 / `wecom/channel.py:send()` 吞掉回执）。改为按部门选各自 bot（实测各部门 bot 才是有效发送方）。
3. **`safe_sync` 未 await**：`safe_sync_quiet/_async` 曾漏 `await`，智能表格同步静默不执行——已修。

> ⚠️ 可观测性坑：插件 `logger.*` 不进 supervisor 日志，只有 `print(..., file=sys.stderr)` 可见。通知链路因此加了 `[WO通知]` stderr trace。

## 5. 鉴权与多 agent

- `auth.require_manager/require_employee` 作为 FastAPI 依赖；角色：访客/员工/经理/管理员。
- 智能体调用带 `X-Agent-Id` 白名单（`hotel-ai-frontdesk/engineering/housekeeping/guest-service`），插件 `default_role=manager`。
- `plugin.json.agent_permissions` 声明允许接入的智能体与默认角色。

## 6. 扩展点

- 新增业务路由：在 `routes/` 建文件，导出 `router` 与 `register_routes(app)`，并在 `routes/__init__.py._PHASE2_MODULES` 登记。
- 新增工单副作用：只在 `work_order_svc.svc_*` 里加，确保所有入口共享。
- 新增智能体工具：`routes/hotel_ops_tools.py`（`register_tools(app)`）。
- 新增 AI 助手模板：`templates/<agent-id>/agent.json + SOUL/AGENTS/PROFILE.md`。
