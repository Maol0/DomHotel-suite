# 部署与运维 · domhotel-suite

## 1. 前置条件

- QwenPaw ≥ 2.0.0（PawApp 宿主）。
- Python 3.11+（后端依赖 `httpx`、`fastapi` 等，通常由宿主提供）。
- 企业微信：一个**自建应用**（拿 `AgentId` + `Secret`）、企业 `CorpID`；如需群播报，把部门 bot 拉进对应部门群。

## 2. 安装插件

把整个 `domhotel-suite/` 放到 QwenPaw 插件目录，重启宿主加载。入口见 `plugin.json`：
- backend: `backend/__init__.py`
- frontend: `ui/plugin-entry.js`

加载成功后路由挂在 `/api/domhotel-suite`。验证：

```bash
curl -s http://127.0.0.1:8088/api/domhotel-suite/health
```

## 3. 配置凭证

优先级：**运行时配置 `wecom_runtime_config.json` > 环境变量 > 内置默认（空）**。

方式 A · 环境变量：复制 `.env.example` 按需填写并注入容器/进程环境。

方式 B · 配置面板 / 智能体工具（免改 env 免重启）：首启「配置向导」或企微面板写入运行时配置；agent tool `wecom_config_set` 亦可。

必填（开通知）：
```
WECOM_CORP_ID=ww********
WECOM_AGENT_ID=1000002
WECOM_AGENT_SECRET=****
```

## 4. 企业微信接入步骤

1. **自建应用**：企业微信后台创建应用 → 记 `AgentId` / `Secret`；配「企业可信 IP」为你的出口 IP。
2. **应用消息到人**：配好上面三项即可发个人工单卡片。
3. **部门群播报**：确认各部门 bot（`hotel-ai-engineering` 等）已在对应群内、且企微通道有活跃长连接。
   - 默认按 `target_dept` 自动选发送 bot；如需全局改用某个持长连接的网关 bot，设 `NOTIFY_GROUP_AGENT_ID`。
   - 部门群 chatid 通过运行时配置 `DEPT_APPCHAT_CHATIDS`（JSON：`{"engineering":"wr...","housekeeping":"wr...","frontdesk":"wr..."}`）提供。
   - 员工 userid 映射 `DEPT_NOTIFY_USERIDS`（JSON），或兜底 `KF_NOTIFY_USERIDS`。
4. **智能表格双向同步**：企微客户端建 8 张表 → 把各表 `doc_id` 配到 `WECOM_DOC_*`；或用 agent tool `wecom_create_docs` 自动建表回填。
5. **微信客服（可选）**：配 `WECOM_KF_ID / WECOM_KF_TOKEN / WECOM_KF_ENCODING_AES_KEY / KF_CALLBACK_BASE_URL`，回调 URL 指向 `/api/domhotel-suite/...`（向导会拼好提示你粘贴到企微后台）。

## 5. 部署 AI 助手

面板「一键部署」或用 agent tool 部署 4 个部门智能体（模板在 `templates/`）。首启 `on_launch` 会尝试自动部署（`.lock` 防抖，失败不致命）。

## 6. 数据与备份

- 数据落 `HOTEL_DATA_DIR/hotel.db`（WAL：`-wal` / `-shm`）。
- **不要**把 `data/`、`*.db*`、`wecom_runtime_config.json`、各类运行时 JSON 提交进仓库（已在 `.gitignore`）。

## 7. 排障

- **收不到通知**：查 stderr 里的 `[WO通知]` trace：
  - `notify_fn=None(跳过通知!!)` → 通知函数未注册（握手失效，检查 work_orders 是否被加载）。
  - `部门群发送成功` 但群里没有 → 发送方 bot 不在群内 / 通道无出站连接（`messages/send` 的 `success` 不代表真送达，`wecom/channel.py:send()` 会吞回执）。改用该部门自己的 bot。
- **建单不通知、直连却能发**：多因模块名称解析；v1.6.3 已用注册握手根治。
- **日志**：插件 `logger` 不进容器日志，调试请用 `print(..., file=sys.stderr)`（见 ARCHITECTURE §4）。

## 8. 发布前自查（去数据 / 去密钥）

```bash
# 真实企微 corp id 兜底值应为空
grep -rnE 'environ\.get\("WECOM_CORP_ID",\s*"[^"]' backend   # 期望：无输出
grep -rnE 'ww[0-9a-f]{10,}|[A-Za-z0-9_-]{40,}' backend        # 人工确认无真实密钥
find . -name '*.bak' -o -name '*.db' -o -name '__pycache__'   # 期望：无
```
