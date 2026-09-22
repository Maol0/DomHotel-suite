# -*- coding: utf-8 -*-
"""v1.3.0/v1.3.5/v1.4.0 智能体配置工具 — 让 QwenPaw 智能体在对话中直接检查/配置企微链路

注册 5 个 agent tools (PawApp.tool 装饰器 → PluginApi.register_tool):
  - wecom_status       查配置状态 + live 探测 (凭证/可信IP/出口IP) + 失败队列
  - wecom_config_set   运行时写入凭证 (corp_id/secret/doc_id), 免改 env 免重启
  - wecom_create_docs  v1.3.5 一键自动创建企微智能表格 (文档+字段) 并写 doc_id,
                       把「手工建表复制 doc_id」也变成智能体可代办
  - wecom_kf_config    v1.4.0 配置/检查微信客服 (客人微信进来 → AI 应答 +
                       报修直达内部员工), 回调凭证写入 + 引导
  - wecom_retry_queue  手动重推失败队列

设计背景 (为什么是 agent tool 而不是 CLI):
  企业微信没有官方 CLI, 官方自动化途径只有服务端 REST API (本插件已封装)。
  阿里云 CLI 只能管理阿里云资源 (ECS/EIP), 管不了企微配置。
  企微配置链路中只剩两个企微安全模型强制的手工步骤 (无 API, 智能体无法代办):
    1. 在企微后台创建自建应用拿 Secret;
    2. 「企业可信IP」白名单添加出口 IP (2022-06 后新自建应用强制)。
  其余全部 (凭证写入/智能表格创建/字段建列/doc_id 配置/微信客服凭证/链路验证/
  队列重推) 均可由智能体通过这些 tools 代办, 用户照智能体指引复制粘贴即可
  独立接入。tools 走 QwenPaw 治理白名单 (plugin.json agent_permissions),
  比 shell 直调 curl 更安全可控。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
import importlib.util

logger = logging.getLogger(__name__)

_BACKEND_PKG = "__domhotel_suite_backend__"


def _ensure_backend():
    """确保 __domhotel_suite_backend__ 包在 sys.modules 中 (工具执行时可能丢失)"""
    if _BACKEND_PKG in sys.modules:
        return
    _backend_dir = Path(__file__).resolve().parent.parent  # backend/
    spec = importlib.util.spec_from_file_location(
        _BACKEND_PKG, str(_backend_dir / "__init__.py"),
        submodule_search_locations=[str(_backend_dir)],
    )
    if spec:
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = _BACKEND_PKG
        mod.__path__ = [str(_backend_dir)]
        mod.__file__ = str(_backend_dir / "__init__.py")
        sys.modules[_BACKEND_PKG] = mod
        # 注册 routes 子包
        routes_dir = _backend_dir / "routes"
        routes_name = f"{_BACKEND_PKG}.routes"
        if routes_name not in sys.modules:
            rspec = importlib.util.spec_from_file_location(
                routes_name, str(routes_dir / "__init__.py"),
                submodule_search_locations=[str(routes_dir)],
            )
            if rspec:
                rmod = importlib.util.module_from_spec(rspec)
                rmod.__package__ = routes_name
                rmod.__path__ = [str(routes_dir)]
                sys.modules[routes_name] = rmod
        # 注册 wecom_sync / appchat / wecom_kf 等子模块
        for sub in ("wecom_sync", "appchat", "wecom_kf", "data_layer"):
            sub_file = _backend_dir / f"{sub}.py"
            sub_name = f"{_BACKEND_PKG}.{sub}"
            if sub_name not in sys.modules and sub_file.exists():
                sspec = importlib.util.spec_from_file_location(sub_name, str(sub_file))
                if sspec and sspec.loader:
                    smod = importlib.util.module_from_spec(sspec)
                    smod.__package__ = _BACKEND_PKG
                    sys.modules[sub_name] = smod
                    sspec.loader.exec_module(smod)

# 模块级 router 占位 — routes/__init__.py 的合并循环要求每个模块有 router 属性;
# 本模块只注册 tools 不注册路由, 保持空 router 即可
try:
    from fastapi import APIRouter
    router = APIRouter()
except Exception:  # pragma: no cover
    router = None


def register_routes(app=None) -> None:
    """占位 (与 routes/*.py 协议对齐, 本模块无 HTTP 路由)"""
    return None


# ─────────────────────────────────────────────
# agent tool 实现 (纯函数, 可独立测试)
# ─────────────────────────────────────────────

async def wecom_status(force: bool = True) -> dict:
    """检查企业微信推送链路的完整配置状态与真实连通性。

    何时调用: 用户询问"企微配置得怎么样了 / 能不能推 / 链路通不通",
    或在配置过程中需要确认当前进度和下一步时。

    参数:
      force: 默认 True 绕过 60s 探测缓存立即真实探测 — 用户刚配完可信 IP
        或改完凭证后复查时必须用最新结果 (缓存会返回旧状态误导用户)。

    返回 (均为只读, 无副作用):
      - config: 当前生效配置 (corp_id / secret 来源 / 8 张表 doc_id 配置情况)
      - live: 真实探测结果 — token_ok(凭证是否有效) / api_ok(链路是否全通)
        / errcode(60020=出口IP不在可信白名单) / client_ip(探测到的出口IP)
      - queue: 失败队列 (pending 条数 / 按表分布)
      - next_step: 给智能体的明确行动指引文本

    注意: 企微「企业可信IP」无 API, 需要管理员在企微管理后台手工添加
    client_ip, 智能体无法代办该步骤。
    """
    _ensure_backend()
    from .. import wecom_sync

    eff = wecom_sync.effective_config()
    try:
        live = await wecom_sync.live_probe(force=force)
    except Exception as exc:
        live = {"checked": False, "token_ok": False, "api_ok": False,
                "errcode": None, "errmsg": str(exc)[:200], "client_ip": ""}
    queue = wecom_sync.queue_status()

    # 给智能体的下一步指引 (按阻断优先级排序)
    if not eff["explicitly_configured"]:
        next_step = (
            "凭证还是插件内置默认值 (只在原部署环境有效)。请向用户要企业微信的 "
            "CorpID(我的企业→企业信息) 和自建应用的 Secret(应用管理→应用详情), "
            "然后调用 wecom_config_set 写入。"
        )
    elif not live.get("token_ok"):
        next_step = (
            "凭证无效 (corp_id/secret 不匹配或 secret 已重置)。请用户在企微后台"
            "重新查看 Secret, 调用 wecom_config_set 更新后再查。"
        )
    elif live.get("errcode") == 60020:
        next_step = (
            f"出口 IP {live.get('client_ip') or '(未知)'} 不在企微应用的可信 IP 白名单。"
            "这是唯一无法用 API 完成的步骤: 请用户以管理员身份登录企微管理后台 → "
            "应用管理 → 应用详情 → 开发者接口 → 企业可信IP → 添加该 IP。"
            "配好前推送会进失败队列, 配好后自动重推 (60s 一轮) 或调 wecom_retry_queue。"
        )
    elif not live.get("api_ok"):
        next_step = f"链路异常: errcode={live.get('errcode')} {live.get('errmsg', '')[:120]}"
    elif eff["docs_ready"] == 0:
        next_step = (
            "链路已通但还没配置任何智能表格 doc_id。直接调用 wecom_create_docs "
            "一键自动创建智能表格 (建文档+字段) 并写入 doc_id — 不需要用户手工建表; "
            "用户也可以自己在企微客户端建表后把 doc_id 告诉你, 用 wecom_config_set 写入。"
        )
    elif eff["docs_ready"] < eff["docs_total"]:
        next_step = (
            f"链路就绪 (凭证有效 + 可信 IP 通过 + {eff['docs_ready']}/{eff['docs_total']} 张表已配)。"
            "如需补齐未配置的表, 可调 wecom_create_docs 只建缺的那几张 "
            "(传 tables 参数指定表名)。"
        )
    else:
        next_step = (
            f"链路就绪 (凭证有效 + 可信 IP 通过 + {eff['docs_ready']}/{eff['docs_total']} 张表已配)。"
            "登记/退房/工单等操作会自动推送到企微智能表格。"
        )
        # v1.4.0: 追加微信客服 (kf) 状态提示 — 智能表格链路就绪后引导下一站
        kf = eff.get("kf", {})
        if not (kf.get("kf_id_configured") and kf.get("callback_configured")):
            next_step += (
                " 另: 微信客服未配置 (客人微信进来 AI 应答 + 报修直达内部) — "
                "如需开通, 引导用户到企微后台「应用管理→微信客服」拿客服账号 "
                "open_kfid + 回调 Token/EncodingAESKey, 调 wecom_kf_config 配置。"
            )
        else:
            next_step += " 微信客服已就绪 (客人微信消息 → AI 应答 + 报修建单)。"

    return {
        "config": eff,
        "live": live,
        "queue": queue,
        "next_step": next_step,
    }


async def wecom_config_set(
    corp_id: str = "",
    agent_secret: str = "",
    doc_staff: str = "",
    doc_departments: str = "",
    doc_room_types: str = "",
    doc_floors: str = "",
    doc_work_orders: str = "",
    doc_rooms_log: str = "",
    doc_guests_log: str = "",
    doc_supplies_log: str = "",
) -> dict:
    """运行时写入企业微信配置 (免改环境变量免重启容器, 写入即生效)。

    何时调用: 用户在对话中提供了 CorpID/Secret 或智能表格 doc_id,
    要求"帮我配置企业微信 / 把这些凭证配上"时。只需传本次要改的项,
    未传的项保持不变; 空字符串会被跳过。

    参数 (企业微信管理后台获取):
      corp_id: 企业 ID (我的企业→企业信息→企业ID, ww 开头)
      agent_secret: 自建应用的 Secret (应用管理→应用详情)
      doc_*: 各业务表的智能表格 doc_id (企微客户端创建文档后从链接复制,
        链接中 https://docs.qq.com/smartsheet/{doc_id} 的路径段)

    返回: 写入结果 + 写入后立即真实探测的 live 结果 (token_ok/api_ok/
    errcode=60020 时含出口 client_ip) + 下一步指引。
    """
    _ensure_backend()
    from .. import wecom_sync

    values = {
        "WECOM_CORP_ID": corp_id,
        "WECOM_AGENT_SECRET": agent_secret,
        "WECOM_DOC_STAFF": doc_staff,
        "WECOM_DOC_DEPARTMENTS": doc_departments,
        "WECOM_DOC_ROOM_TYPES": doc_room_types,
        "WECOM_DOC_FLOORS": doc_floors,
        "WECOM_DOC_WORK_ORDERS": doc_work_orders,
        "WECOM_DOC_ROOMS_LOG": doc_rooms_log,
        "WECOM_DOC_GUESTS_LOG": doc_guests_log,
        "WECOM_DOC_SUPPLIES_LOG": doc_supplies_log,
    }
    result = wecom_sync.set_runtime_config(values)
    if result.get("ok"):
        # 写完立即真探测 (set_runtime_config 已清缓存), 给智能体即时反馈
        try:
            result["live"] = await wecom_sync.live_probe()
        except Exception as exc:
            result["live"] = {"checked": False, "token_ok": False,
                              "errmsg": str(exc)[:200]}
        live = result.get("live") or {}
        if not live.get("token_ok"):
            result["next_step"] = (
                "凭证写入成功但 gettoken 验证失败 — 请核对 corp_id/secret 是否匹配"
                "(注意 Secret 重置后旧值立即失效)。"
            )
        elif live.get("errcode") == 60020:
            result["next_step"] = (
                f"凭证有效, 但出口 IP {live.get('client_ip', '')} 不在企微可信 IP 白名单"
                "(errcode 60020)。请让管理员在企微管理后台 → 应用详情 → 开发者接口 → "
                "企业可信IP 中手工添加该 IP — 企微未开放此操作的 API, 智能体无法代办。"
                "配好后让用户告诉你, 再调 wecom_status(force=True) 复查。"
            )
        elif live.get("api_ok") and result.get("effective", {}).get("docs_ready", 0) == 0:
            result["next_step"] = (
                "凭证有效且链路已通。下一步直接调用 wecom_create_docs 一键自动创建"
                "智能表格并写入 doc_id, 无需用户手工建表。"
            )
        else:
            result["next_step"] = "配置已生效。可调用 wecom_status 复查全链路状态。"
    return result


async def wecom_create_docs(
    tables: list[str] | None = None,
    admin_users: list[str] | None = None,
    doc_name_prefix: str = "",
) -> dict:
    """一键自动创建企业微信智能表格并写入 doc_id 配置 (v1.3.5)。

    何时调用: 凭证已配置且可信 IP 已通过 (wecom_status 的 api_ok=true), 但
    智能表格还没建/doc_id 没配时 — 用户说"帮我建表 / 把表格配好"。
    对每张表自动完成: 创建智能表格文档 → 建/发现子表 → 按 schema 建全部字段
    → doc_id 写入运行时配置, 全程无需用户在企微客户端手工建表。

    参数:
      tables: 要建的表名列表, 默认 3 张核心业务表 ["work_orders",
        "rooms_log", "guests_log"]; 可选全部 8 张: work_orders/rooms_log/
        guests_log/supplies_log/staff/departments/room_types/floors。
        补建时只传缺的那几张即可, 已配置的表不会被覆盖。
      admin_users: 可选, 企微 userid 列表 — 设为文档管理员后用户能在企微
        客户端直接管理这些表; 不传则文档归属于应用。
      doc_name_prefix: 可选, 文档名前缀 (如酒店名), 生成的文档名形如
        "XX酒店工单表"。

    返回: 每张表的 {ok, docid, url, fields_added} + 配置写入结果 + 下一步指引。

    前置要求: 可信 IP 白名单必须已通过 (errcode 60020 会被企微拦截,
    无法创建任何文档) — 调用前先用 wecom_status 确认 api_ok=true。
    """
    _ensure_backend()
    from .. import wecom_sync

    # 前置: 链路必须已通 (60020 会拦 create_doc, 建了也白建)
    try:
        live = await wecom_sync.live_probe(force=True)
    except Exception as exc:
        live = {"token_ok": False, "api_ok": False, "errmsg": str(exc)[:200]}
    if not live.get("api_ok"):
        if not live.get("token_ok"):
            return {
                "ok": False,
                "blocked_by": "credentials",
                "next_step": (
                    "凭证无效 (corp_id/secret 不匹配)。请向用户要正确的 "
                    "CorpID 和自建应用 Secret, 调 wecom_config_set 写入后重试。"
                ),
            }
        if live.get("errcode") == 60020:
            return {
                "ok": False,
                "blocked_by": "trusted_ip",
                "client_ip": live.get("client_ip", ""),
                "next_step": (
                    f"出口 IP {live.get('client_ip') or '(未知)'} 不在企微可信 IP 白名单, "
                    "企微会拦截所有 API (含建表)。这是企微安全设计, 无 API 可配 — "
                    "请让管理员在企微管理后台 → 应用详情 → 开发者接口 → 企业可信IP "
                    "中手工添加该 IP, 配好后调 wecom_status(force=True) 确认 api_ok "
                    "再重试本工具。"
                ),
            }
        return {
            "ok": False,
            "blocked_by": "api",
            "live": live,
            "next_step": (
                f"链路未通: errcode={live.get('errcode')} "
                f"{str(live.get('errmsg', ''))[:150]}。先调 wecom_status 排查。"
            ),
        }

    # 默认只建 3 张核心业务表 (与向导/文档指引一致); 全量需显式传 tables
    if tables is None:
        tables = ["work_orders", "rooms_log", "guests_log"]
    # 已配置 doc_id 的表默认跳过 (避免重复建表), 除非显式重复传入
    eff = wecom_sync.effective_config()
    already = [t for t in tables if eff.get("docs", {}).get(t)]
    todo = [t for t in tables if not eff.get("docs", {}).get(t)]

    if not todo:
        return {
            "ok": True,
            "created": 0,
            "skipped_already_configured": already,
            "next_step": (
                "这些表的 doc_id 都已配置, 无需重建。可调 wecom_status 查看整体状态; "
                "如确实要重建, 请先想清楚 (重建会得到新空表, 旧表数据不会迁移)。"
            ),
        }

    result = await wecom_sync.create_table_docs(
        tables=todo, admin_users=admin_users, doc_name_prefix=doc_name_prefix
    )
    result["skipped_already_configured"] = already

    if result.get("ok"):
        urls = {
            t: v.get("url", "")
            for t, v in (result.get("results") or {}).items()
            if v.get("ok")
        }
        result["next_step"] = (
            f"已创建 {result.get('created', 0)} 张智能表格并写入 doc_id 配置, "
            f"推送链路全部就绪。文档链接: {urls}。把这些链接发给用户即可在企微中"
            "查看; 建议提醒用户把文档收藏/移动到常用目录。"
        )
    else:
        result["next_step"] = (
            "部分表创建失败 (详见 results 里各表的 error)。常见原因: 可信 IP "
            "白名单未生效/字段数超限/企微文档权限。可修正后对失败的表重试本工具。"
        )
    return result


async def wecom_kf_config(
    kf_id: str = "",
    kf_token: str = "",
    kf_encoding_aes_key: str = "",
    ai_agent_id: str = "",
    notify_userids: str = "",
    wecom_agent_id: str = "",
    callback_base_url: str = "",
) -> dict:
    """配置或检查微信客服 — 客人用微信进来, AI 自动应答 + 报修直达内部 (v1.4.0)。

    何时调用: 用户说"帮我配置微信客服 / 让客人能从微信联系酒店 / 客服号"
    时。调用前先引导用户在企微后台 (管理后台 → 应用管理 → 微信客服) 拿三样:
      1. 客服账号 open_kfid (wk 开头) — 「客服账号」页添加后获得;
      2. 回调 Token + EncodingAESKey (43 位) — 客服账号 → API → 接收消息 →
         回调配置页生成, 回调 URL 填本工具返回的 callback_url;
      3. 确认「通过API管理微信客服账号」已勾选自建应用。
    前提: 本服务公网可访问 (企微要能推回调) + 出口 IP 在应用可信白名单
    (与智能表格推送共用同一应用与白名单)。

    参数 (全部可选, 只传要改的; 全空 = 仅查询当前状态):
      kf_id: 客服账号 open_kfid
      kf_token: 回调 Token
      kf_encoding_aes_key: 回调 EncodingAESKey (必须 43 位)
      ai_agent_id: AI 应答用 QwenPaw 智能体 ID (默认 hotel-ai-guest-service)
      notify_userids: 内部通知员工企微 userid (逗号分隔; 客人报修时推送)
      wecom_agent_id: 自建应用 AgentId 数字 (内部通知用)
      callback_base_url: 用户访问本系统的公网地址 (如 http://1.2.3.4:8889),
        用于拼出准确的回调 URL 给用户复制到企微后台。
        传入一次即记住 (存运行时配置), 之后查询/引导回显都用它;
        更换部署服务器时传新地址覆盖即可 (出口 IP 也会变, 需重新探测白名单)

    返回: 写入结果 (如有参数) + 当前客服状态 + 回调 URL + 下一步指引。
    """
    _ensure_backend()
    from .. import wecom_sync

    # EncodingAESKey 长度前置校验 (43 位; 错误 key 配进去回调全挂)
    if kf_encoding_aes_key and len(kf_encoding_aes_key.strip()) != 43:
        return {
            "ok": False,
            "next_step": (
                f"EncodingAESKey 长度应为 43 位, 收到 {len(kf_encoding_aes_key.strip())} 位。"
                "请让用户从企微后台客服回调配置页重新复制完整密钥。"
            ),
        }

    values = {}
    if kf_id:
        values["WECOM_KF_ID"] = kf_id
    if kf_token:
        values["WECOM_KF_TOKEN"] = kf_token
    if kf_encoding_aes_key:
        values["WECOM_KF_ENCODING_AES_KEY"] = kf_encoding_aes_key.strip()
    if ai_agent_id:
        values["KF_AI_AGENT_ID"] = ai_agent_id
    if notify_userids:
        values["KF_NOTIFY_USERIDS"] = notify_userids
    if wecom_agent_id:
        values["WECOM_AGENT_ID"] = wecom_agent_id
    # 部署地址传一次即记住 (换服务器覆盖) — 保证后续引导/查询回显的回调 URL 准确
    if callback_base_url:
        values["KF_CALLBACK_BASE_URL"] = callback_base_url.strip().rstrip("/")

    result: dict = {"ok": True, "written": False}
    if values:
        result = wecom_sync.set_runtime_config(values)
        result["written"] = bool(result.get("written"))

    eff = wecom_sync.effective_config()
    kf = eff.get("kf", {})
    # 回调 URL 优先级: 本次传参 > 已记住的部署地址 (KF_CALLBACK_BASE_URL) > 占位符
    effective_base = (callback_base_url or kf.get("callback_base_url") or "").strip().rstrip("/")
    callback_url = (
        f"{effective_base}/api/domhotel-suite/kf/callback"
        if effective_base
        else "<你访问本系统的公网地址>/api/domhotel-suite/kf/callback"
    )
    result["status"] = {
        "kf_id": kf.get("kf_id", ""),
        "kf_id_configured": kf.get("kf_id_configured", False),
        "callback_configured": kf.get("callback_configured", False),
        "ai_agent_id": kf.get("ai_agent_id", ""),
        "notify_userids_count": kf.get("notify_userids_count", 0),
        "agent_id_configured": kf.get("agent_id_configured", False),
        "callback_url": callback_url,
        "callback_base_url": effective_base,
    }

    if not result["status"]["kf_id_configured"]:
        result["next_step"] = (
            "还没有客服账号 ID。引导用户: 企微管理后台 → 应用管理 → 微信客服 → "
            "开通, 并在「通过API管理微信客服账号」勾选自建应用 → 「客服账号」"
            "→ 添加客服账号 → 把 open_kfid (wk 开头) 发给你, 连同回调页生成的 "
            f"Token/EncodingAESKey 一起传入本工具。回调 URL 填: {callback_url}"
        )
    elif not result["status"]["callback_configured"]:
        result["next_step"] = (
            "已有 open_kfid 但缺回调 Token/EncodingAESKey。引导用户: 客服账号 → "
            "API → 接收消息 → 回调配置, URL 填 " + callback_url +
            ", 保存时企微会先发验证请求 (本服务自动应答), 通过后把生成的 Token "
            "和 43 位 EncodingAESKey 发给你传入本工具。注意: 服务需公网可访问; "
            "若 callback_url 还是占位符, 请把用户实际访问系统的公网地址作为 "
            "callback_base_url 传入 (传一次即记住, 换服务器时传新地址覆盖)。"
        )
    else:
        result["next_step"] = (
            "微信客服链路已配置就绪。验证方式: 让客人扫客服二维码或在微信里搜索"
            "客服号发一条消息 — 系统会自动欢迎语 + AI 应答; 发「报修 空调坏了」"
            "直接建工单" + (
                "并推送内部员工" if result["status"]["notify_userids_count"] else
                " (如需同步推送内部员工, 传 notify_userids + wecom_agent_id 补配)"
            ) + "。"
        )
    return result


async def wecom_retry_queue() -> dict:
    """手动重推企业微信推送失败队列 (通常用于可信 IP 配好后立即补推)。

    何时调用: 用户说"可信 IP 配好了 / 把之前失败的推送补上"时。
    平时无需调用 — 后台每 60s 自动重试一轮。

    返回: 本轮重试成功条数 + 队列剩余情况。
    """
    _ensure_backend()
    from .. import wecom_sync

    n = await wecom_sync.retry_pending(max_n=50)
    queue = wecom_sync.queue_status()
    return {
        "retried_success": n,
        "queue": queue,
        "note": (
            "后台每 60s 也会自动重试一轮; 队列持续非空通常意味着链路仍不通"
            "(可信 IP 未生效 / 凭证失效), 可调 wecom_status 排查。"
        ),
    }


# ─────────────────────────────────────────────
# 注册入口 (由 routes/__init__.py 的 register_all 调用, 传入真 PawApp)
# ─────────────────────────────────────────────

def register_tools(app) -> int:
    """把 5 个配置工具注册到 PawApp (app.tool 装饰器 → register 时进 Agent 工具箱)"""
    n = 0
    try:
        app.tool(
            "wecom_status",
            description="检查企业微信推送链路配置状态与真实连通性 (凭证/可信IP/出口IP/doc_id/微信客服/失败队列), 返回下一步行动指引; 默认绕过缓存立即真探测",
            icon="📡",
        )(wecom_status)
        n += 1
        app.tool(
            "wecom_config_set",
            description="运行时写入企业微信配置 (CorpID/Secret/智能表格doc_id), 免改env免重启立即生效, 写完自动真实验证",
            icon="⚙️",
        )(wecom_config_set)
        n += 1
        app.tool(
            "wecom_create_docs",
            description="一键自动创建企业微信智能表格(文档+字段)并写入doc_id配置, 无需用户手工建表; 要求可信IP已通过(api_ok=true), 会自动检查",
            icon="🗂️",
        )(wecom_create_docs)
        n += 1
        app.tool(
            "wecom_kf_config",
            description="配置/检查微信客服 (客人微信进来→AI自动应答+报修直达内部员工): 写入客服账号open_kfid/回调Token/EncodingAESKey/AI智能体/内部通知员工, 返回回调URL与逐步引导; 全空参数=仅查状态",
            icon="💬",
        )(wecom_kf_config)
        n += 1
        app.tool(
            "wecom_retry_queue",
            description="手动重推企业微信推送失败队列 (可信IP配好后立即补推; 平时60s自动重试)",
            icon="🔁",
        )(wecom_retry_queue)
        n += 1
    except Exception as exc:
        logger.warning("[wecom_tools] agent tool 注册失败: %s", exc)
    return n


__all__ = [
    "register_tools", "register_routes",
    "wecom_status", "wecom_config_set", "wecom_create_docs",
    "wecom_kf_config", "wecom_retry_queue",
]
