"""企微同步双向接口 - v2.1.18

完整架构:
  本地 (JSON)  ⇄  企业微信智能表格 (权威数据源)

提供端点:
  GET    /wecom/queue               - 看失败队列
  GET    /wecom/config              - 看生效配置
  POST   /wecom/retry               - 手动重试失败队列
  POST   /wecom/pull/{table}        - 从企微拉数据到本地 (反向同步)
  POST   /wecom/webhook             - 企微变更回调 (接收企微推过来的变更)
  POST   /wecom/init/{table}        - 初始化: 全表替换 (企微→本地)
  GET    /wecom/sync_log            - 看双向同步历史日志
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Body, Request

from .. import auth, data_layer, wecom_sync
from ..meta import (
    apply_meta_on_create,
    apply_meta_on_update,
    DATA_SOURCE_LABELS,
    now,
)
from qwenpaw.pawapp import get_ctx

logger = logging.getLogger(__name__)
router = APIRouter()


# ════════════════════════════════════════════════════════════
# 诊断 / 配置
# ════════════════════════════════════════════════════════════


@router.get("/wecom/queue")
async def wecom_queue_status(ctx=Depends(get_ctx)):
    """返回企微推送队列状态 (给前端 sync tab 用)"""
    return wecom_sync.queue_status()


@router.get("/wecom/config")
async def wecom_config(ctx=Depends(get_ctx)):
    """返回企微推送配置 (脱敏:不返回 secret, 只返回 corp_id 和各表 doc_id 是否已配置)

    v1.3.0: 改用运行时配置层 (runtime json > env > 内置默认), 新增 secret_source 溯源
    v1.3.4: 新增 docs (doc_id 实值, 供后台配置面板表单回显) 与 docs_source 逐表溯源 —
    doc_id 非凭证 (访问仍需 corp_id+secret), 回显便于只改一张表时不必全部重填。
    """
    eff = wecom_sync.effective_config()
    docs = eff["docs"]
    configured = {k: bool(v) for k, v in docs.items()}
    # 逐表溯源: runtime json > env > 内置默认/未配置
    runtime_cfg = wecom_sync._load_runtime_cfg()
    docs_source = {}
    env_key_map = {t: f"WECOM_DOC_{t.upper()}" for t in wecom_sync.DEFAULT_DOCS}
    for t in wecom_sync.DEFAULT_DOCS:
        if runtime_cfg.get(env_key_map[t]):
            docs_source[t] = "runtime"
        elif os.environ.get(env_key_map[t]):
            docs_source[t] = "env"
        elif wecom_sync.DEFAULT_DOCS.get(t):
            docs_source[t] = "builtin_default"
        else:
            docs_source[t] = ""
    return {
        "corp_id": eff["corp_id"],
        "secret_configured": eff["secret_configured"],
        "secret_source": eff["secret_source"],
        "explicitly_configured": eff["explicitly_configured"],
        "base_url": wecom_sync.WECOM_BASE_URL,
        "docs": docs,
        "docs_source": docs_source,
        "docs_configured": configured,
        "docs_total": eff["docs_total"],
        "docs_ready": eff["docs_ready"],
        "tables_supported": list(wecom_sync.TABLE_SCHEMAS.keys()),
        "runtime_config_file": eff["runtime_config_file"],
        # v1.4.0: 微信客服 (kf) 配置状态 (脱敏 — token/aes_key 只报是否已配不回显)
        "kf": {
            "kf_id": wecom_sync.get_kf_setting("WECOM_KF_ID"),  # open_kfid 非密钥可回显
            "kf_id_configured": bool(wecom_sync.get_kf_setting("WECOM_KF_ID")),
            "callback_configured": eff["kf"]["callback_configured"],
            "callback_config_source": eff["kf"]["callback_config_source"],
            "ai_agent_id": eff["kf"]["ai_agent_id"],
            "ai_agent_source": eff["kf"]["ai_agent_source"],
            "notify_userids": wecom_sync.get_kf_setting("KF_NOTIFY_USERIDS"),
            "agent_id": wecom_sync.get_kf_setting("WECOM_AGENT_ID"),
            "agent_id_configured": eff["kf"]["agent_id_configured"],
            # 已记住的部署地址 (拼回调 URL; 空串=未存, 前端回退浏览器地址动态推断)
            "callback_base_url": eff["kf"].get("callback_base_url", ""),
        },
        # v2.1.18: 架构方向提示
        "data_source": "local_mirror",
        "data_source_note": "本地 JSON 是快速访问镜像, 企微智能表格是权威数据源 (Phase 6+ 启用)",
        "sync_direction_supported": ["local→wecom", "wecom→local", "wecom_callback"],
    }


@router.post("/wecom/config")
async def wecom_config_set(
    payload: dict = Body(default_factory=dict),
    sess: dict = Depends(auth.require_manager()),
):
    """运行时写入企微配置 (v1.3.0, 需 manager 权限)

    免改 docker env 免重启, 写入即生效 (token/探测/sheet 缓存自动清除)。
    Body 白名单键 (非白名单忽略, 空值跳过):
      WECOM_CORP_ID / WECOM_AGENT_SECRET / WECOM_DOC_{STAFF,DEPARTMENTS,
      ROOM_TYPES,FLOORS,WORK_ORDERS,ROOMS_LOG,GUESTS_LOG,SUPPLIES_LOG}
    """
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="Body 需为非空 JSON 对象")
    result = wecom_sync.set_runtime_config(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail={"ok": False, "reason": result.get("error", "写入失败")})
    # 写完立即 live 探测一次 (缓存已清, 会真探测), 把结果一并返回
    try:
        result["live"] = await wecom_sync.live_probe()
    except Exception as exc:
        result["live"] = {"checked": False, "errmsg": str(exc)[:200]}
    result["hint"] = (
        "live.errcode=60020 时, 把 live.client_ip 加入企微应用的「企业可信IP」"
        "(管理后台 → 应用管理 → 应用详情 → 开发者接口 → 企业可信IP); "
        "此项企微未开放 API, 需管理员手工操作"
    )
    return result


@router.post("/wecom/retry")
async def wecom_retry_now(ctx=Depends(get_ctx)):
    """手动触发一次失败队列重试 (运维用)"""
    n = await wecom_sync.retry_pending(max_n=50)
    return {"retried_success": n}


# ════════════════════════════════════════════════════════════
# 反向同步: 企微 → 本地
# ════════════════════════════════════════════════════════════


@router.post("/wecom/pull/{table}")
async def wecom_pull_table(
    ctx=Depends(get_ctx),
    table: str = "",
    payload: dict = Body(default_factory=dict),
):
    """从企微智能表格拉一条记录到本地 (v2.1.18+)

    Body: {"record_id": "WO-xxx", "force": False}
    - record_id: 企微端记录 ID (必填)
    - force: 即使本地有更新也覆盖 (默认 False, last-write-wins 用 version 比较)

    注意: 当前实现从企微 smartsheet.get_records 拉单条,
    上线后让用户填企微 corp_id + secret + doc_id 后才会真去拉。
    当前 docid 占位 → 接口能通但拿不到数据。
    """
    if table not in data_layer.KNOWN_TABLES:
        raise HTTPException(status_code=400, detail=f"未知表: {table}")

    record_id = payload.get("record_id", "")
    if not record_id:
        raise HTTPException(status_code=400, detail="record_id 必填")
    force = bool(payload.get("force", False))

    docid = wecom_sync.get_docid(table)
    if not docid:
        raise HTTPException(
            status_code=400,
            detail=f"表 {table} 未配置企微 doc_id (env WECOM_DOC_{table.upper()} 或 POST /wecom/config)"
        )

    # 从企微智能表格拉一条
    try:
        from .. import wecom_api
        token = await wecom_api.get_access_token()
        # 用 update_records 反查 (企微 API 没 get_record by id, 用 list_records 过滤)
        # 这里先 return 501 表示接口已预留, 待企微 client 实现
        # TODO: 实现 get_records 拉单条逻辑
        return {
            "ok": False,
            "reason": "接口已预留, 待后续接企微 get_records",
            "hint": "需要先在企微后端建好智能表格, 把 doc_id 填到环境变量",
        }
    except Exception as exc:
        logger.warning(f"wecom pull {table}/{record_id} failed: {exc}")
        raise HTTPException(status_code=500, detail=f"拉取失败: {exc}")


@router.post("/wecom/init/{table}")
async def wecom_init_table_from_remote(
    ctx=Depends(get_ctx),
    table: str = "",
):
    """从企微全表初始化本地 (v2.1.18+, **慎用: 会覆盖本地全部数据**)

    用途:
      - 全新部署, 让本地数据从企微拉过来
      - 误删本地 JSON, 从企微恢复

    行为:
      1. 从企微智能表格拉所有记录
      2. 替换本地表 (本地数据会被覆盖)
      3. 写入 data_source='wecom_init' 元信息
    """
    if table not in data_layer.KNOWN_TABLES:
        raise HTTPException(status_code=400, detail=f"未知表: {table}")
    docid = wecom_sync.get_docid(table)
    if not docid:
        raise HTTPException(
            status_code=400,
            detail=f"表 {table} 未配置企微 doc_id, 无法初始化",
        )
    # 同样预留
    return {
        "ok": False,
        "reason": "接口已预留, 待企微 client 完整实现 get_records",
        "hint": "需要先在企微后端建好智能表格, 把 doc_id 填到环境变量",
    }


# ════════════════════════════════════════════════════════════
# 企微回调 (webhook)
# ════════════════════════════════════════════════════════════


@router.post("/wecom/webhook")
async def wecom_webhook(
    request: Request,
    ctx=Depends(get_ctx),
):
    """接收企微智能表格的变更回调 (v2.1.18+)

    用途:
      - 企微用户在智能表格里改了房态/工单 → 推送到这里 → 写本地 JSON
      - 解决"企微是权威数据源"的同步链路

    安全: 需要校验签名 (此处预留, 实际用 corp_id + secret 做 HMAC)
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"raw": body}
    logger.info(f"[wecom_webhook] 收到回调: {body}")
    # TODO: 校验签名 / 解析 event / 找到本地对应记录 / 用 version 比对决定是否覆盖
    return {
        "ok": True,
        "received": True,
        "hint": "接口已注册, 后续实现签名校验 + 字段映射",
        "received_at": now(),
    }


# ════════════════════════════════════════════════════════════
# 同步历史日志
# ════════════════════════════════════════════════════════════


@router.get("/wecom/sync_log")
async def wecom_sync_log(
    ctx=Depends(get_ctx),
    limit: int = 50,
):
    """看双向同步历史日志 (本地的 sync_log 表, 每次同步写一条)

    数据格式: {time, table, op, direction, record_id, source, success, error}
    """
    log = data_layer.load_table("wecom_sync_log")
    log.reverse()  # 最新在前
    return {
        "total": len(log),
        "limit": limit,
        "items": log[:limit],
    }


@router.api_route("/wecom/test_ping", methods=["GET", "POST"])
async def wecom_test_ping(force: int = 0, ctx=Depends(get_ctx)):
    """测试企微连通性 (v2.1.18+: 诊断用; 整合版: 兼容 GET 供向导 Step 4 拉配置状态)

    v1.2.0: 新增 live 真实探测 (gettoken + get_sheet, 全只读):
      - live.token_ok=False → 凭证无效
      - live.api_ok=False 且 errcode=60020 → 出口 IP 不在企微应用可信 IP 白名单,
        live.client_ip 为探测到的出口 IP, 需加到企微管理后台 → 应用详情 → 企业可信IP
      - live.api_ok=True → 推送链路全程可用
    另新增 explicitly_configured: 区分「用户 env/运行时显式配置」与「插件内置默认凭证」
    (后者只在原部署环境有效, 新环境必须配置自己的凭证)。
    v1.3.3: ?force=1 绕过 60s 探测缓存 — 向导 Step4 门禁的「重新检测」用。
    """
    eff = wecom_sync.effective_config()
    try:
        live = await wecom_sync.live_probe(force=bool(force))
    except Exception as exc:  # 探测异常不影响状态返回
        live = {
            "checked": False, "token_ok": False, "api_ok": False,
            "errcode": None, "errmsg": f"探测失败: {exc}"[:300],
            "client_ip": "", "probe": "", "checked_at": "",
        }
    return {
        "corp_id": eff["corp_id"],
        "secret_configured": eff["secret_configured"],
        "secret_source": eff["secret_source"],
        "explicitly_configured": eff["explicitly_configured"],
        "docs_ready": eff["docs_ready"],
        "docs_total": eff["docs_total"],
        "docs_detail": {k: bool(v) for k, v in eff["docs"].items()},
        "queue_pending": wecom_sync.queue_status().get("pending", 0),
        "live": live,
        "hint": (
            "live.errcode=60020 时, 把 live.client_ip 加入企微应用的「企业可信IP」"
            "(管理后台 → 应用管理 → 应用详情 → 开发者接口 → 企业可信IP)"
        ),
    }