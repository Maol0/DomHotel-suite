# DomHotel Suite — Changelog

## v1.6.4 (2026-09-24)

### 🎯 微信身份打通 + 退房反查对齐 + 前台收单统一 + 客服回复提速

- **退房反查对齐（本次核心修复）**：客人从微信入口绑房后，若退房在**房态看板 / 前台登记 / AGENT 之外**操作，`t_customers` 不会被清 → 之后微信来消息仍被识别成「还住在原房间」。修复分两层：
  - **写时反向解绑**：`checkin.checkout_checkin` 与 `rooms.checkout` 退房落地后调 `wecom_kf.sync_checkout_from_pms(room_no, phone, name)`，以房间为主键清绑该房活跃微信客人（置 `deleted/unbound_at/checkout_at`）并 `guest_profile.record_checkout` 关闭本次住宿（身份/历史跨退房保留，供回访识别）。
  - **读时自愈**：`resolve_current_room` 无 `in_house` 匹配且入住表存在该房间 **正向 `checked_out` 记录**（电话/姓名吻合）时软删陈旧绑定、促使其重新绑房；保守判定，不误伤不使用 `t_checkins` 的酒店。
- **微信身份握手（external_userid ↔ openid ↔ 电话）**：客服绑房后与前台入住（PMS）握手，缓存 openid，`guest_profile` 记录房号/房型/住宿史；未绑定的访客也缓存 openid；解绑后回访欢迎语带「上次住 X 房 / Y 房型」。新增只读诊断端点 `GET /admin/identity-debug`（`stored_identity / convert_live / pms_match_preview`，用于验证 openid 转换权限是否真的生效）。
- **客服回复提速**：AI 应答提示词仅在**会话首轮**注入身份/画像上下文（`chat_session_manager` 判定 `created_new`），后续轮次精简注入，显著缩短微信首屏回复延迟。
- **前台收单统一**：客人可发起的报修/需求（`wecom_kf` 客服报修、`hotel_ops_tools`、`guest_service`、`guest_portal`）一律先收单到 `frontdesk`（`_SERVICE_MAP` 全部指向 frontdesk），再由前台转派工程/客房/清洁，避免直达部门时派单口径不一。

## v1.6.3 (2026-09-22)

### 🔧 根治「建单不通知」+ 部门群发送方修正 + 发布包脱敏

- **通知注册握手**：定位到根因——运行时 `work_order_svc.__package__` 指向的父包从未注册进 `sys.modules`（宿主实际用 `plugin_domhotel_suite` 命名），导致 `_get_work_orders_module()` 无论按名字 endswith、按属性扫描、还是相对 import 都返回 `None` → 房态/客人/手动建单**全部静默跳过通知**。改为 `work_orders` 在模块加载时把 `_notify_staff_wo_change` 注入 `svc._NOTIFY_FN`，`svc_*` 直接 `await _notify_change(...)`，彻底不依赖模块名解析。全生命周期（建/派/接/完）统一走此入口，并加 `notify_fn=已注册✓/None(跳过通知!!)` stderr 判定。
- **部门群发送方按部门选 bot**：工单群播报原统一用 `hotel-wecom-assistant`，实测它不在部门群内、`messages/send` 返回 success 但群里收不到（`wecom/channel.py:send()` 吞回执）。改为按 `target_dept` 选各部门自己的 bot（engineering→`hotel-ai-engineering`、housekeeping→`hotel-ai-housekeeping`、frontdesk→`hotel-ai-frontdesk`）；`NOTIFY_GROUP_AGENT_ID` 仍可整体覆盖。
- **发布包脱敏**：从可发布源码中抹除内置的企业微信 `CorpID / AgentSecret / 智能表格 doc_id` 兜底常量，改为仅环境变量/运行时配置、默认空；补 `.env.example`、强化 `.gitignore`（排除 `data/`、`*.db*`、`*.bak`、`__pycache__/`、运行时 JSON）。

## v1.6.2 (2026-09-22)

### 🩹 数据同步修复 + 通知可观测

- **修复补货不同步**：`safe_sync` 是协程，但 `safe_sync_quiet`/`safe_sync_quiet_async` 用同步方式调用、从未 `await` → 补货 `supplies_log` 智能表格根本不同步（`coroutine never awaited` 佐证）。现修正。（`rooms.py` 补货 else 分支同类漏 await 一并修）
- **通知可观测化**：插件 `logger.*` 不进 supervisor 日志，通知跑没跑看不见。新增 `[WO通知]` stderr trace，覆盖个人/部门群的「入口·准备发送·结果·失败」，精确定位企微收不到的那一条链路。
- 核查确认：房态(rooms.py) 建/关单已全部经 `work_order_svc`；个人应用消息实测企微返回真实 msgid (链路通)。

## v1.6.1 (2026-09-22)

### 🔒 工单全生命周期收口 + 企业微信通知修复

- **建单全量统一**：`guest_portal` 报修/需求/H5、`wecom_kf` 客服报修、`guest_service` H5 全部改走 `work_order_svc.svc_create_work_order`（建单 + 派单 + 企微表格同步 + 员工卡片 + 客人回执一致）
- **确认/评价收口**：客人确认完成 → `svc_complete_work_order(force)`；评价补 `safe_sync` 同步企微表格
- **退房即建清洁工单**：前台 `checkout_checkin` 退房自动建 housekeeping 清洁单（与房态退房一致）
- **部门群通知修根因**：群消息改走真正拥有该群 chatid 的网关 bot （`NOTIFY_GROUP_AGENT_ID`，默认 `hotel-wecom-assistant`）；发送失败 `raise` + 显式告警日志，不再静默丢通知
- **客人来单不再自动派单**：`auto_dispatch=False`，落 pending 交前台确认 / AI 派单
- **修复**：完成时误覆盖在住房间为「空房」的在住保护；报修照片未落库

## v1.6.0 (2026-09-13)

### 🔧 架构重构 + 工具权限补全 + 排班系统

#### 新增
- `backend/routes/work_order_svc.py` — 工单 service 层（唯一业务逻辑层）
  - `svc_create_work_order()` — 创建 + 自动派单 + safe_sync + 通知
  - `svc_assign_work_order()` — 派单 + history + pending_actions + safe_sync + 通知
  - `svc_complete_work_order()` — 完成 + 耗时 + 房态联动 + pending_actions + safe_sync + 通知
  - `get_on_shift_staff()` — 在岗推断（排班表 → 当天工单 → fallback 全部）
  - `_auto_dispatch()` — 自动派单（在岗人员中选在途最少的）
- **10 个新 agent 工具**（从 15 → 25 个）：
  - 需求单: `staff_request_create`, `staff_request_list`
  - Ticket: `staff_ticket_list`, `staff_ticket_assign`, `staff_ticket_transition`
  - 待确认: `staff_pending_list`, `staff_pending_confirm`, `staff_pending_reject`
  - 排班: `staff_schedule_query`, `staff_schedule_set`

#### 重构
- `work_orders.py` 路由改为调用 service 函数
- `hotel_ops_tools.py` agent 工具改为调用 service 函数
- 自动派单逻辑：排班表 > 当天工单推断 > 部门全部员工

#### RBAC 权限修复
| Agent | 修复前 | 修复后 |
|-------|--------|--------|
| guest-service | 7 | 7 |
| frontdesk | 15 | 25 |
| engineering | 4 | 13 |
| housekeeping | 5 | 13 |

- AGENTS.md 文档全部重写，与 RBAC 实际权限对齐

## v1.5.1 (2026-09-11)

- 企微部门群通知改造：appchat → QwenPaw REST API
- 真实部门群 chatid 替换完成

## v1.5.0 (2026-09-10)

- 客人 H5 入口 + 二维码管理
- 微信客服回调配置
- 员工 H5 工作台
- 全链路冒烟检测
