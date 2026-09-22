# -*- coding: utf-8 -*-
"""企微智能表格同步 — 2.0 plugin (hotel-frontdesk-pawapp)

设计要点（参照用户拍板的 Q1=C / Q2=A）：
  - 双写双读：本地 JSON 仍是 source of truth，企微智能表格是镜像副本
  - 写策略：每次本地写成功后，异步写企微；企微失败不阻塞主流程
  - 失败兜底：写本地失败队列 `wecom_pending_queue.json`，后台定时重试
  - 读策略：从本地读（企微不参与查询，避免双读不一致）
  - 凭证复用：与 config.json 里 wecom_smartsheet MCP server 同一套凭证
  - 删除策略：软删除（deleted=true），企微端保留历史，不推删除

数据流向：
  plugin API → 本地 JSON _save() → 同步钩子 → wecom_sync._push()
                                            ↓ 成功
                                       企微智能表格
                                            ↓ 失败
                                       本地失败队列（后台补传）

Phase 5 扩展（2026-08-04）：
  - DEFAULT_DOCS 从 4 张扩到 8 张（新增 staff/departments/room_types/floors）
  - TABLE_SCHEMAS 加 4 张 admin 表
  - record_id 策略：log 表用组合键（room_no+time），主数据用 ID（staff.id/dept.id/...）
  - 注意：doc_id 占位（待用户提供真实的 doc_id 后填入）

v1.2.0 修复（2026-09-02，对照企微官方文档 path/99907 + path/101154）：
  - add_records 请求体缺必填 sheet_id、多传非法 record_id（官方 AddRecord 只有 values，
    record_id 由企微生成并在响应返回）→ 已修正；sheet_id 通过 get_sheet API 自动发现
    （取文档第一个 smartsheet 子表），也可用 env WECOM_SHEET_{TABLE} 显式指定
  - values 的 key 用字段标题（key_type=CELL_VALUE_KEY_TYPE_FIELD_TITLE），
    要求企微端表格字段名与 TABLE_SCHEMAS 的 key 对齐
  - 失败队列路径与 data_layer.DATA_DIR 对齐（不再写死原插件工作区）
  - 新增 live_probe()：真实调 gettoken + get_sheet，把 60020 可信 IP / 凭证失效
    在配置阶段暴露给向导 Step 4
  - start_retry_loop 加防重入 + 无 running loop 时 daemon 线程兑底
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# v1.2.0: retry 循环防重入标志
_retry_started = False

# v1.4.1: 敏感凭证单独落盘, 与运行时配置分离
_SECRET_KEYS = frozenset({
    "WECOM_AGENT_SECRET",
    "WECOM_KF_TOKEN",
    "WECOM_KF_ENCODING_AES_KEY",
})


def _secrets_path() -> Path:
    """敏感凭证文件路径, 权限 0600"""
    return _queue_path().parent / ".wecom_secrets"


def _load_secrets_file() -> None:
    """读取 .wecom_secrets 到环境变量(不覆盖已存在的环境变量)"""
    p = _secrets_path()
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _SECRET_KEYS and not os.environ.get(k):
                os.environ[k] = v.strip()
    except Exception as exc:
        logger.warning("[wecom_sync] 读取 secrets 文件失败: %s", exc)


def _write_secrets_file(secrets: dict) -> None:
    """写入敏感凭证到 .wecom_secrets, 权限 0600"""
    p = _secrets_path()
    lines = [f"{k}={v}" for k, v in secrets.items() if k in _SECRET_KEYS and v]
    if not lines:
        if p.exists():
            p.unlink()
        return
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception as exc:
        logger.warning("[wecom_sync] 无法设置 secrets 文件权限 0600: %s", exc)


def _migrate_secrets_from_runtime_cfg() -> None:
    """一次性迁移: 把 wecom_runtime_config.json 里的敏感凭证移到 .wecom_secrets"""
    p = _runtime_config_path()
    if not p.exists():
        return
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return
        secrets = {k: cfg.pop(k) for k in list(cfg.keys()) if k in _SECRET_KEYS}
        if secrets:
            _write_secrets_file(secrets)
            _load_secrets_file()
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                p.chmod(0o600)
            except Exception as exc:
                logger.warning("[wecom_sync] 无法设置运行时配置文件权限 0600: %s", exc)
            logger.info("[wecom_sync] 已迁移 %d 个敏感字段到 .wecom_secrets", len(secrets))
    except Exception as exc:
        logger.warning("[wecom_sync] 迁移敏感凭证失败: %s", exc)


# ─────────────────────────────────────────────
# 凭证 & 配置（与 wecom-smartsheet MCP server 完全一致）
# ─────────────────────────────────────────────

WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_SECRET = os.environ.get(
    "WECOM_AGENT_SECRET",
    "",
)
WECOM_BASE_URL = "https://qyapi.weixin.qq.com"

# Phase 5.1: 通讯录同步凭证占位（暂未启用,等用户配 WECOM_CONTACTS_SECRET + WECOM_TRUSTED_IP）
# 注意：通讯录同步用的是独立 secret,不是 WECOM_AGENT_SECRET
WECOM_CONTACTS_SECRET = os.environ.get("WECOM_CONTACTS_SECRET", "")
WECOM_TRUSTED_IP = os.environ.get("WECOM_TRUSTED_IP", "")
if not WECOM_CONTACTS_SECRET or not WECOM_TRUSTED_IP:
    logger.info(
        "[wecom_sync] 通讯录同步凭证未配齐 (WECOM_CONTACTS_SECRET=%s, WECOM_TRUSTED_IP=%s) "
        "→ /admin/staff/sync_from_wecom 暂不可用,但不影响智能表格双向同步",
        "已配" if WECOM_CONTACTS_SECRET else "未配",
        "已配" if WECOM_TRUSTED_IP else "未配",
    )

# 8 张智能表格的默认 doc_id（首次启动前在企微客户端建好，把 doc_id 填到下面）
# 建表方式：先在企业微信客户端建 8 张智能表格，把 doc_id 填到这里；env 可覆盖
# 多酒店场景：可以按 hotel_id 分表（v1 单店）
# ⚠️ Phase 5 占位：所有 doc_id 待用户提供后填入
DEFAULT_DOCS = {
    # 主数据 (admin)
    "staff": os.environ.get("WECOM_DOC_STAFF", ""),
    "departments": os.environ.get("WECOM_DOC_DEPARTMENTS", ""),
    "room_types": os.environ.get("WECOM_DOC_ROOM_TYPES", ""),
    "floors": os.environ.get("WECOM_DOC_FLOORS", ""),
    # 业务
    "work_orders": os.environ.get(
        "WECOM_DOC_WORK_ORDERS",
        "",
    ),
    # 业务日志（追加）
    # v2.1.18 修复: rooms_log / guests_log / supplies_log 默认 docid 必须空 (占位符会推到错误的表格 → 60020 IP/60011 not allow)
    # 用户接入企微时, 必须为这3张表单独配 WECOM_DOC_ROOMS_LOG / GUESTS_LOG / SUPPLIES_LOG env
    "rooms_log": os.environ.get("WECOM_DOC_ROOMS_LOG", ""),
    "guests_log": os.environ.get("WECOM_DOC_GUESTS_LOG", ""),
    "supplies_log": os.environ.get("WECOM_DOC_SUPPLIES_LOG", ""),
}


# ─────────────────────────────────────────────
# v1.3.0 运行时配置层 — 三层优先级: runtime json > env > 内置默认
# 目的: 智能体 (agent tool wecom_config_set / POST /wecom/config) 可直接
# 写入凭证, 免改 docker env 免重启容器。写入后自动清 token/探测/sheet 缓存。
# ─────────────────────────────────────────────
_RUNTIME_CFG_FILE = "wecom_runtime_config.json"
# v1.3.4 修复: doc 键统一大写 — get_docid() 读 runtime 用 WECOM_DOC_{TABLE大写},
# 原白名单却生成小写键 (WECOM_DOC_rooms_log), 导致所有 doc_id 被拒写入 (v1.3.0 隐藏 bug)
_RUNTIME_KEYS = frozenset(
    {"WECOM_CORP_ID", "WECOM_AGENT_SECRET"}
    | {f"WECOM_DOC_{t.upper()}" for t in DEFAULT_DOCS}
    # v1.4.0: 微信客服 (kf) 相关配置 — 面板/智能体工具可写, 免改 env 免重启
    # WECOM_KF_ID 客服账号ID | WECOM_KF_TOKEN/WECOM_KF_ENCODING_AES_KEY 回调验证+加解密
    # KF_AI_AGENT_ID AI 应答智能体 | KF_NOTIFY_USERIDS 内部通知员工 | WECOM_AGENT_ID 应用消息 agentid
    # KF_CALLBACK_BASE_URL 部署环境的公网访问地址 (拼回调 URL 用, 传一次记住, 换服务器覆盖)
    | {
        "WECOM_KF_ID",
        "WECOM_KF_TOKEN",
        "WECOM_KF_ENCODING_AES_KEY",
        "KF_AI_AGENT_ID",
        "KF_NOTIFY_USERIDS",
        "WECOM_AGENT_ID",
        "KF_CALLBACK_BASE_URL",
        # v2.2.2: 部门 appchat 群 chatid 映射（JSON 字符串）
        "DEPT_APPCHAT_CHATIDS",
        # v2.2.2: 部门通知 userid 映射（JSON 字符串，如 {"engineering":["LiuHaiYang"]}）
        "DEPT_NOTIFY_USERIDS",
    }
)


def _runtime_config_path() -> Path:
    """运行时配置文件路径 (与失败队列同目录, 跟随 data_layer 数据目录)"""
    return _queue_path().parent / _RUNTIME_CFG_FILE


def _load_runtime_cfg() -> dict:
    try:
        p = _runtime_config_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k in _RUNTIME_KEYS}
    except Exception as exc:
        logger.warning("[wecom_sync] 读取运行时配置失败(忽略,回退 env/内置默认): %s", exc)
    return {}


def get_corp_id() -> str:
    return _load_runtime_cfg().get("WECOM_CORP_ID") or WECOM_CORP_ID


def get_agent_secret() -> str:
    return _load_runtime_cfg().get("WECOM_AGENT_SECRET") or WECOM_AGENT_SECRET


def get_docid(table: str) -> str:
    return _load_runtime_cfg().get(f"WECOM_DOC_{table.upper()}") or DEFAULT_DOCS.get(table, "")


def get_kf_setting(key: str, default: str = "") -> str:
    """读微信客服 (kf) / 应用消息类运行时配置 (v1.4.0): runtime json > env > default

    key 取值: WECOM_KF_ID / WECOM_KF_TOKEN / WECOM_KF_ENCODING_AES_KEY /
              KF_AI_AGENT_ID / KF_NOTIFY_USERIDS / WECOM_AGENT_ID / KF_CALLBACK_BASE_URL
    """
    val = _load_runtime_cfg().get(key) or os.environ.get(key, "")
    return val or default


def effective_config() -> dict:
    """当前生效配置 (脱敏, 供诊断端点/agent tool 展示)"""
    cfg = _load_runtime_cfg()
    docs = {t: get_docid(t) for t in DEFAULT_DOCS}
    kf_token = get_kf_setting("WECOM_KF_TOKEN")
    kf_aes = get_kf_setting("WECOM_KF_ENCODING_AES_KEY")
    notify_uids = [u.strip() for u in get_kf_setting("KF_NOTIFY_USERIDS").replace(
        "，", ",").split(",") if u.strip()]
    return {
        "corp_id": get_corp_id(),
        "secret_configured": bool(get_agent_secret()),
        "secret_source": (
            "runtime" if cfg.get("WECOM_AGENT_SECRET")
            else "env" if os.environ.get("WECOM_AGENT_SECRET")
            else "builtin_default"
        ),
        "explicitly_configured": bool(
            cfg.get("WECOM_CORP_ID") and cfg.get("WECOM_AGENT_SECRET")
        ) or bool(
            os.environ.get("WECOM_CORP_ID") and os.environ.get("WECOM_AGENT_SECRET")
        ),
        "docs": docs,
        "docs_ready": sum(1 for v in docs.values() if v),
        "docs_total": len(docs),
        "runtime_config_file": str(_runtime_config_path()),
        # v1.4.0: 微信客服 (kf) 状态 (脱敏 — 只报是否已配, 不回显密钥)
        "kf": {
            "kf_id": get_kf_setting("WECOM_KF_ID"),
            "kf_id_configured": bool(get_kf_setting("WECOM_KF_ID")),
            "callback_configured": bool(kf_token and kf_aes),
            "callback_config_source": (
                "runtime" if cfg.get("WECOM_KF_TOKEN")
                else "env" if kf_token else ""
            ),
            "ai_agent_id": get_kf_setting("KF_AI_AGENT_ID", "hotel-ai-guest-service"),
            "ai_agent_source": (
                "runtime" if cfg.get("KF_AI_AGENT_ID")
                else "env" if os.environ.get("KF_AI_AGENT_ID")
                else "default"
            ),
            "notify_userids_count": len(notify_uids),
            "agent_id_configured": bool(get_kf_setting("WECOM_AGENT_ID")),
            # 部署环境公网地址 (拼回调 URL; 未存时为空, 由 Request/浏览器地址动态推断)
            "callback_base_url": get_kf_setting("KF_CALLBACK_BASE_URL"),
        },
    }


def set_runtime_config(values: dict) -> dict:
    """写入运行时配置 (v1.3.0, 智能体/HTTP 均可调)

    v1.4.1 安全改造:
    - 敏感凭证 (WECOM_AGENT_SECRET / WECOM_KF_TOKEN / WECOM_KF_ENCODING_AES_KEY)
      写入 .wecom_secrets (权限 0600), 不进入 wecom_runtime_config.json
    - 其他配置写入 wecom_runtime_config.json
    - 只接受白名单键 (_RUNTIME_KEYS), 非法键记入 rejected
    - 空字符串/None 跳过 (不覆盖已有值)
    - 写入后清空 token / live 探测 / sheet_id 缓存, 立即生效
    返回 {ok, written(脱敏), skipped, rejected, effective}
    """
    cfg = _load_runtime_cfg()
    secrets = {}
    written, skipped, rejected = {}, [], []
    for k, v in (values or {}).items():
        # v1.3.4: doc 键大小写归一 (智能体/表单可能传小写), 与 get_docid 的大写键对齐
        if isinstance(k, str) and k.upper().startswith("WECOM_DOC_"):
            k = k.upper()
        if k not in _RUNTIME_KEYS:
            rejected.append(k)
            continue
        v = (v or "").strip() if isinstance(v, str) else v
        if not v:
            skipped.append(k)
            continue
        if k in _SECRET_KEYS:
            secrets[k] = v
        else:
            cfg[k] = v
        written[k] = v
    if written:
        try:
            # 1) 敏感凭证落 .wecom_secrets
            if secrets:
                existing = {k: os.environ.get(k, "") for k in _SECRET_KEYS}
                existing.update(secrets)
                _write_secrets_file(existing)
                _load_secrets_file()
            # 2) 非敏感配置落 json
            _runtime_config_path().write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                _runtime_config_path().chmod(0o600)
            except Exception as exc:
                logger.warning("[wecom_sync] 无法设置运行时配置文件权限 0600: %s", exc)
        except Exception as exc:
            logger.error("[wecom_sync] 写入运行时配置失败: %s", exc)
            return {
                "ok": False, "error": str(exc)[:200],
                "written": {}, "skipped": skipped, "rejected": rejected,
            }
        # 凭证/docid 变了 → 清所有缓存, 下次调用重新取 token/sheet
        _token_cache.update(token="", expires_at=0)
        _probe_cache.update(ts=0.0, data=None)
        _sheet_id_cache.clear()
        logger.info("[wecom_sync] 运行时配置已更新: %s", list(written))
    return {
        "ok": True,
        "written": {k: ("***" if k in _SECRET_KEYS else v) for k, v in written.items()},
        "skipped": skipped,
        "rejected": rejected,
        "effective": effective_config(),
    }

# 失败队列 (v1.2.0: 惰性解析, 与 data_layer 数据目录对齐, 不再写死原插件工作区)
# v1.4.0-persistent: 默认使用持久化目录
_DATA_DIR_LEGACY = "/app/working/dompaw-data-backup"


def _queue_path() -> Path:
    """失败队列文件路径。

    优先级: env HOTEL_DATA_DIR_V20 (与 data_layer 一致) > env HOTEL_DATA_DIR
    (向后兼容) > /app/working/dompaw-data-backup (持久化目录) > data_layer.DATA_DIR > cwd/data/v20。
    独立部署时队列落在自己的数据目录, 不会污染其他插件的工作区。
    """
    env = os.environ.get("HOTEL_DATA_DIR_V20") or os.environ.get("HOTEL_DATA_DIR")
    if env:
        return Path(env) / "wecom_pending_queue.json"
    # v1.4.0-persistent: 优先使用持久化目录,避免相对导入 data_layer 失败时落到 /app/data/v20
    persistent = Path("/app/working/dompaw-data-backup")
    if persistent.exists():
        return persistent / "wecom_pending_queue.json"
    try:
        from . import data_layer  # type: ignore
        return data_layer.DATA_DIR / "wecom_pending_queue.json"
    except Exception:
        return Path.cwd() / "data" / "v20" / "wecom_pending_queue.json"


# 兼容旧引用 (不建议再直接使用)
DATA_DIR = Path(os.environ.get("HOTEL_DATA_DIR_V20", os.environ.get("HOTEL_DATA_DIR", _DATA_DIR_LEGACY)))
QUEUE_PATH = DATA_DIR / "wecom_pending_queue.json"

# v1.4.1: 启动时迁移敏感凭证到独立 secrets 文件并加载
_migrate_secrets_from_runtime_cfg()
_load_secrets_file()


# ─────────────────────────────────────────────
# access_token 缓存（与 MCP server 一样）
# ─────────────────────────────────────────────

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0}
_token_lock = asyncio.Lock()


async def _get_access_token() -> str:
    """获取/刷新 access_token，缓存 7000s（官方 7200s 留余量）"""
    async with _token_lock:
        if _token_cache["token"] and _token_cache["expires_at"] > time.time() + 60:
            return _token_cache["token"]

        url = f"{WECOM_BASE_URL}/cgi-bin/gettoken"
        params = {"corpid": get_corp_id(), "corpsecret": get_agent_secret()}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            data = r.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: {data}")

        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + 7000
        return _token_cache["token"]


# 公开别名，供 wecom_kf 等模块使用（避免相对导入 wecom_api 在 daemon 线程失败）
get_access_token = _get_access_token


# ─────────────────────────────────────────────
# 智能表格字段类型映射（参照 wecom-smartsheet MCP 的规范）
# ─────────────────────────────────────────────

def _fmt_value(field_type: str, value: Any) -> Any:
    """按企微智能表格要求格式化字段值"""
    if value is None or value == "":
        return None
    if field_type == "FIELD_TYPE_TEXT":
        return [{"type": "text", "text": str(value)}]
    if field_type == "FIELD_TYPE_NUMBER":
        try:
            return float(value)
        except Exception:
            return None
    if field_type == "FIELD_TYPE_DATE_TIME":
        # 转毫秒时间戳字符串
        if isinstance(value, (int, float)):
            return str(int(value))
        try:
            ts = datetime.fromisoformat(str(value)).timestamp() * 1000
            return str(int(ts))
        except Exception:
            return None
    if field_type == "FIELD_TYPE_SINGLE_SELECT":
        return [{"text": str(value)}]
    if field_type == "FIELD_TYPE_SELECT":
        return [{"text": str(value)}]
    if field_type == "FIELD_TYPE_CHECKBOX":
        return bool(value)
    return value


# 各表的字段映射（要跟企微客户端建的表格字段对齐！）
# 字段名（key）= 企微表格字段名；field_type 决定格式化方式
TABLE_SCHEMAS: dict[str, list[dict]] = {
    # ────────── admin 主数据 (Phase 5 新增) ──────────
    "staff": [
        {"key": "id",                   "field_type": "FIELD_TYPE_TEXT"},
        {"key": "name",                 "field_type": "FIELD_TYPE_TEXT"},
        {"key": "department_id",        "field_type": "FIELD_TYPE_TEXT"},
        {"key": "role",                 "field_type": "FIELD_TYPE_TEXT"},
        {"key": "phone",                "field_type": "FIELD_TYPE_TEXT"},
        {"key": "on_duty",              "field_type": "FIELD_TYPE_CHECKBOX"},
        # Phase 5.1: 企微对齐字段（用户去企微客户端 staff 表手动加这 3 列）
        {"key": "wecom_userid",         "field_type": "FIELD_TYPE_TEXT"},
        {"key": "wecom_sync_status",    "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "wecom_last_synced_at", "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "created_at",           "field_type": "FIELD_TYPE_DATE_TIME"},
    ],
    "departments": [
        {"key": "id",         "field_type": "FIELD_TYPE_TEXT"},
        {"key": "name",       "field_type": "FIELD_TYPE_TEXT"},
        {"key": "description","field_type": "FIELD_TYPE_TEXT"},
        {"key": "manager",    "field_type": "FIELD_TYPE_TEXT"},
        {"key": "created_at", "field_type": "FIELD_TYPE_DATE_TIME"},
    ],
    "room_types": [
        {"key": "id",         "field_type": "FIELD_TYPE_TEXT"},
        {"key": "name",       "field_type": "FIELD_TYPE_TEXT"},
        {"key": "beds",       "field_type": "FIELD_TYPE_NUMBER"},
        {"key": "area",       "field_type": "FIELD_TYPE_NUMBER"},
        {"key": "price",      "field_type": "FIELD_TYPE_NUMBER"},
        {"key": "created_at", "field_type": "FIELD_TYPE_DATE_TIME"},
    ],
    "floors": [
        {"key": "id",         "field_type": "FIELD_TYPE_TEXT"},
        {"key": "name",       "field_type": "FIELD_TYPE_TEXT"},
        {"key": "order",      "field_type": "FIELD_TYPE_NUMBER"},
        {"key": "created_at", "field_type": "FIELD_TYPE_DATE_TIME"},
    ],
    # ────────── 业务主数据 ──────────
    "work_orders": [
        {"key": "wo_id",        "field_type": "FIELD_TYPE_TEXT"},
        {"key": "room_no",      "field_type": "FIELD_TYPE_TEXT"},
        {"key": "work_type",    "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "priority",     "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "status",       "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "target_dept",  "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "assignee",     "field_type": "FIELD_TYPE_TEXT"},
        {"key": "assignee_id",  "field_type": "FIELD_TYPE_TEXT"},
        {"key": "reporter",     "field_type": "FIELD_TYPE_TEXT"},
        {"key": "description",  "field_type": "FIELD_TYPE_TEXT"},
        {"key": "created_at",   "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "updated_at",   "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "completed_at", "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "result_note",  "field_type": "FIELD_TYPE_TEXT"},
    ],
    # ────────── 业务日志 ──────────
    "rooms_log": [
        {"key": "room_no",     "field_type": "FIELD_TYPE_TEXT"},
        {"key": "status",      "field_type": "FIELD_TYPE_SINGLE_SELECT"},
        {"key": "guest_name",  "field_type": "FIELD_TYPE_TEXT"},
        {"key": "guest_phone", "field_type": "FIELD_TYPE_TEXT"},
        {"key": "checkin_time","field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "updated_at",  "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "updated_by",  "field_type": "FIELD_TYPE_TEXT"},
        {"key": "note",        "field_type": "FIELD_TYPE_TEXT"},
    ],
    "guests_log": [
        {"key": "room_no",      "field_type": "FIELD_TYPE_TEXT"},
        {"key": "guest_name",   "field_type": "FIELD_TYPE_TEXT"},
        {"key": "guest_phone",  "field_type": "FIELD_TYPE_TEXT"},
        {"key": "checkin_time", "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "checkout_time","field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "checkin_days", "field_type": "FIELD_TYPE_NUMBER"},
    ],
    "supplies_log": [
        {"key": "room_no", "field_type": "FIELD_TYPE_TEXT"},
        {"key": "items",   "field_type": "FIELD_TYPE_TEXT"},
        {"key": "time",    "field_type": "FIELD_TYPE_DATE_TIME"},
        {"key": "operator","field_type": "FIELD_TYPE_TEXT"},
    ],
}

# record_id 字段映射: 每张表用哪个字段做幂等键
RECORD_ID_FIELD: dict[str, str] = {
    "staff": "id",
    "departments": "id",
    "room_types": "id",
    "floors": "id",
    "work_orders": "wo_id",
    "rooms_log": "record_key",   # 在调用处用 room_no+updated_at 拼
    "guests_log": "record_key",  # room_no+checkin_time
    "supplies_log": "record_key",# room_no+time
}


def _build_record(table: str, data: dict) -> dict:
    """把本地 dict 转成企微智能表格记录格式"""
    fields: dict[str, Any] = {}
    for col in TABLE_SCHEMAS.get(table, []):
        key = col["key"]
        if key in data:
            v = _fmt_value(col["field_type"], data[key])
            if v is not None:
                fields[key] = v
    return fields


# ─────────────────────────────────────────────
# 失败队列（按 Q2=A：静默失败 + 后台补传）
# ─────────────────────────────────────────────

def _load_queue() -> list[dict]:
    _p = _queue_path()
    if not _p.is_file():
        return []
    try:
        return json.loads(_p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_queue(queue: list[dict]) -> None:
    _p = _queue_path()
    _p.parent.mkdir(parents=True, exist_ok=True)
    _p.write_text(
        json.dumps(queue[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8",  # 只保留最近 200 条，避免无限增长
    )


def _enqueue(table: str, op: str, payload: dict, error: str) -> None:
    """失败入队（按 record_key 去重，同一记录多次失败只留最新一次）"""
    queue = _load_queue()
    # 按表的 record_id 字段取幂等键
    id_field = RECORD_ID_FIELD.get(table, "id")
    record_key = payload.get(id_field) or payload.get("wo_id") or payload.get("room_no") or str(time.time())
    # 去重：同一 record_key + table 只留最新
    queue = [
        q for q in queue
        if not (q["table"] == table and q.get("record_key") == record_key)
    ]
    queue.append({
        "table": table,
        "op": op,  # "add" / "update" / "skip"
        "record_key": record_key,
        "payload": payload,
        "error": str(error)[:500],
        "failed_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "retries": 0,
    })
    _save_queue(queue)
    logger.warning("[wecom_sync._enqueue] table=%s key=%s err=%s", table, record_key, error[:120])


# ─────────────────────────────────────────────
# v1.6.1: 内部员工通知失败持久队列
# 通知走 message/send (与智能表格 add_records 不同传输), 复用同一
# wecom_pending_queue.json, 用 table="_notify" 区分; retry_pending 特判后
# 回调 work_orders.resend_staff_notice 补发。
# ─────────────────────────────────────────────

NOTIFY_TABLE = "_notify"


def enqueue_notify(userids, title, detail, wo_id="", card_action="", error="") -> None:
    """内部员工通知发送失败时入队, 由后台 retry loop 补发 (按 wo_id+title 去重)。"""
    try:
        queue = _load_queue()
        record_key = f"notify:{wo_id}:{title}"
        queue = [q for q in queue
                 if not (q["table"] == NOTIFY_TABLE and q.get("record_key") == record_key)]
        queue.append({
            "table": NOTIFY_TABLE,
            "op": "add",
            "record_key": record_key,
            "payload": {
                "userids": list(userids), "title": title, "detail": detail,
                "wo_id": wo_id, "card_action": card_action,
            },
            "error": str(error)[:500],
            "failed_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "retries": 0,
        })
        _save_queue(queue)
        logger.warning("[wecom_sync.enqueue_notify] wo=%s err=%s", wo_id, str(error)[:120])
    except Exception as exc:
        logger.warning("[wecom_sync.enqueue_notify] 入队失败: %s", exc)


def _work_orders_module():
    """延迟取 work_orders 模块 (避免 wecom_sync 顶层反向依赖 routes)。"""
    for k, v in sys.modules.items():
        if k.endswith("routes.work_orders") and hasattr(v, "resend_staff_notice"):
            return v
    return None


# ─────────────────────────────────────────────
# 主同步入口（plugin 端点调用）
# ─────────────────────────────────────────────

# sheet_id 缓存 (table -> sheet_id), v1.2.0 新增
_sheet_id_cache: dict[str, str] = {}


async def _get_sheet_id(table: str) -> str:
    """获取表对应的智能表格子表 sheet_id (add_records 必填参数)

    优先级: env WECOM_SHEET_{TABLE} 显式指定 > get_sheet API 自动发现
    (取文档第一个 type=smartsheet 的子表) > 内存缓存。
    """
    env_val = os.environ.get(f"WECOM_SHEET_{table.upper()}", "")
    if env_val:
        return env_val
    if table in _sheet_id_cache:
        return _sheet_id_cache[table]

    docid = get_docid(table)
    if not docid:
        return ""

    token = await _get_access_token()
    url = f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/get_sheet"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{url}?access_token={token}",
            json={"docid": docid, "need_all_type_sheet": False},
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"get_sheet errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    sheet_list = data.get("sheet_list", []) or []
    # 优先取 smartsheet 类型 (文档里可能还有 dashboard/external 页)
    for sh in sheet_list:
        if sh.get("type") == "smartsheet":
            _sheet_id_cache[table] = sh.get("sheet_id", "")
            return _sheet_id_cache[table]
    if sheet_list:
        _sheet_id_cache[table] = sheet_list[0].get("sheet_id", "")
        return _sheet_id_cache[table]
    raise RuntimeError(f"doc {docid} 中未找到智能表格子表 (sheet_list 为空)")


# ─────────────────────────────────────────────
# 一键建表 (v1.3.5: agent tool wecom_create_docs 的后端实现)
# API 链路: create_doc(doc_type=10 智能表格) → get_sheet 自动发现子表
#           (空则 add_sheet 兜底) → add_fields 按 TABLE_SCHEMAS 建字段
# 官方文档: path/97470 (create_doc) / 99896 (add_sheet) / 99904 (add_fields)
# ─────────────────────────────────────────────

TABLE_CN_NAMES: dict[str, str] = {
    "staff": "员工表",
    "departments": "部门表",
    "room_types": "房型表",
    "floors": "楼层表",
    "work_orders": "工单表",
    "rooms_log": "房态日志表",
    "guests_log": "客人日志表",
    "supplies_log": "物资日志表",
}


def _build_fields_payload(table: str, plain: bool = False) -> list[dict]:
    """TABLE_SCHEMAS → add_fields 请求的 fields 数组。

    plain=False: NUMBER/DATE_TIME 带最小 property — 首选;
    plain=True:  只传 field_title + field_type — 服务端拒绝 property 时的降级重试。
    """
    fields: list[dict] = []
    for col in TABLE_SCHEMAS.get(table, []):
        f: dict[str, Any] = {
            "field_title": col["key"],
            "field_type": col["field_type"],
        }
        if not plain:
            if col["field_type"] == "FIELD_TYPE_NUMBER":
                f["property_number"] = {"decimal_places": 0}
            elif col["field_type"] == "FIELD_TYPE_DATE_TIME":
                f["property_date_time"] = {"auto_fill": False}
        fields.append(f)
    return fields


async def _wecom_post(path: str, body: dict) -> dict:
    """企微 POST 封装 (自动带 access_token; errcode 非 0 原样返回, 由调用方分支)"""
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{WECOM_BASE_URL}{path}?access_token={token}", json=body
        )
        return r.json()


async def _find_sheet_id(docid: str, fallback_title: str = "") -> str:
    """查文档的第一个智能表格子表; 查不到或文档无子表时 add_sheet 兜底新建。"""
    data = await _wecom_post(
        "/cgi-bin/wedoc/smartsheet/get_sheet",
        {"docid": docid, "need_all_type_sheet": False},
    )
    if data.get("errcode") == 0:
        sheet_list = data.get("sheet_list", []) or []
        for sh in sheet_list:
            if sh.get("type") == "smartsheet":
                return sh.get("sheet_id", "")
        if sheet_list:
            return (sheet_list[0] or {}).get("sheet_id", "")
    # 文档没有子表 (API 新建的智能表格可能不带默认子表) → add_sheet
    data = await _wecom_post(
        "/cgi-bin/wedoc/smartsheet/add_sheet",
        {"docid": docid, "properties": {"title": fallback_title or "数据表"}},
    )
    if data.get("errcode") == 0:
        sid = ((data.get("properties") or {}).get("sheet_id", "")) or ""
        if sid:
            return sid
        # add_sheet 成功但未回传 sheet_id → 重查一次取最后一张
        data = await _wecom_post(
            "/cgi-bin/wedoc/smartsheet/get_sheet",
            {"docid": docid, "need_all_type_sheet": False},
        )
        if data.get("errcode") == 0:
            lst = data.get("sheet_list", []) or []
            if lst:
                return (lst[-1] or {}).get("sheet_id", "")
    return ""


async def create_table_docs(
    tables: list[str] | None = None,
    admin_users: list[str] | None = None,
    doc_name_prefix: str = "",
) -> dict:
    """一键创建企微智能表格并把 doc_id 写入运行时配置 (v1.3.5)

    对每张表: create_doc(doc_type=10) → 子表发现/新建 → add_fields
    (三级降级: 带 property → 纯类型 → 全文本) → doc_id 集中写运行时配置。
    前提: 凭证有效且可信 IP 已通过 (否则 create_doc 会被 60020 拦截,
    调用方 wecom_create_docs 会先做 live 检查)。
    """
    requested = list(tables) if tables else list(DEFAULT_DOCS)
    valid = [t for t in requested if t in TABLE_SCHEMAS]
    unknown = [t for t in requested if t not in TABLE_SCHEMAS]
    results: dict[str, Any] = {}
    written: dict[str, str] = {}

    for t in valid:
        r: dict[str, Any] = {"table": t, "ok": False}
        try:
            # 1) 创建智能表格文档 (doc_type=10)
            doc_name = f"{doc_name_prefix}{TABLE_CN_NAMES.get(t, t)}"
            body: dict[str, Any] = {"doc_type": 10, "doc_name": doc_name}
            if admin_users:
                body["admin_users"] = list(admin_users)
            data = await _wecom_post("/cgi-bin/wedoc/create_doc", body)
            if data.get("errcode") != 0 or not data.get("docid"):
                raise RuntimeError(
                    f"create_doc errcode={data.get('errcode')} {data.get('errmsg')}"
                )
            docid = str(data["docid"])
            r["docid"] = docid
            r["url"] = f"https://docs.qq.com/smartsheet/{docid}"

            # 2) 子表发现/兜底新建
            sheet_id = await _find_sheet_id(docid, TABLE_CN_NAMES.get(t, t))
            if not sheet_id:
                raise RuntimeError("无法发现/新建智能表格子表 (sheet_id 为空)")
            r["sheet_id"] = sheet_id

            # 3) 建字段 — 三级降级 (property 被拒 → 纯类型 → 全文本)
            chosen: list[dict] | None = None
            degraded = ""
            last_err = ""
            for mode in ("rich", "plain", "text"):
                if mode == "rich":
                    fs = _build_fields_payload(t, plain=False)
                elif mode == "plain":
                    fs = _build_fields_payload(t, plain=True)
                else:
                    fs = [
                        {"field_title": c["key"], "field_type": "FIELD_TYPE_TEXT"}
                        for c in TABLE_SCHEMAS.get(t, [])
                    ]
                data = await _wecom_post(
                    "/cgi-bin/wedoc/smartsheet/add_fields",
                    {"docid": docid, "sheet_id": sheet_id, "fields": fs},
                )
                if data.get("errcode") == 0:
                    chosen = fs
                    if mode == "text":
                        degraded = (
                            "字段类型降级为纯文本 (时间/数字以文本显示), "
                            "可在企微客户端手动调整字段类型"
                        )
                    break
                last_err = f"errcode={data.get('errcode')} {data.get('errmsg')}"
            if chosen is None:
                raise RuntimeError(f"add_fields 三级尝试均失败: {last_err}")
            r["fields_added"] = len(chosen)
            if degraded:
                r["degraded"] = degraded

            written[f"WECOM_DOC_{t.upper()}"] = docid
            r["ok"] = True
        except Exception as exc:
            r["error"] = str(exc)[:300]
        results[t] = r

    save_result: dict[str, Any] = {}
    if written:
        save_result = set_runtime_config(written)
    return {
        "ok": bool(results) and all(v.get("ok") for v in results.values()),
        "created": sum(1 for v in results.values() if v.get("ok")),
        "failed": [t for t, v in results.items() if not v.get("ok")],
        "results": results,
        "unknown_tables": unknown,
        "config_saved": (
            save_result.get("written", {}) if save_result.get("ok") else save_result
        ),
    }


async def _add_record(table: str, payload: dict) -> bool:
    """向企微智能表格添加一条记录

    v1.2.0 修正 (对照官方文档 path/99907):
      - sheet_id 为必填参数 → 通过 _get_sheet_id 自动发现
      - records[] 内只传 values, 不传 record_id (新增时 record_id 由企微
        生成并在响应返回, 自定义 record_id 是非法参数)
      - key_type 固定 CELL_VALUE_KEY_TYPE_FIELD_TITLE: values 的 key 用
        字段标题, 要求企微端表格字段名与 TABLE_SCHEMAS 对齐
    注意: add 是纯新增, 同一业务键重复推送会产生重复行 (重试队列已按
    record_key 去重, 正常流程不会重复; 网络超时误判失败的极端场景除外)。
    """
    docid = get_docid(table)
    if not docid:
        # doc_id 没配置时,跳过推送 (Phase 5 兼容:允许先上线后补 doc_id)
        logger.debug("[wecom_sync._add_record] skip: no docid for table=%s", table)
        return False

    sheet_id = await _get_sheet_id(table)
    if not sheet_id:
        raise RuntimeError(
            f"table={table} 无法确定 sheet_id (可设 env WECOM_SHEET_{table.upper()} 显式指定)"
        )

    token = await _get_access_token()
    url = f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/add_records"

    body = {
        "docid": docid,
        "sheet_id": sheet_id,
        "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
        "records": [{
            "values": _build_record(table, payload),
        }],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{url}?access_token={token}",
            json=body,
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"add_records errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    return True


async def _push(table: str, op: str, payload: dict) -> bool:
    """主推送: 先 add_record; 失败 → 入队列
    op:
      - "add": 新增记录
      - "update": 更新记录 (Phase 5 简化: 重新 add, 企微同 record_id 会覆盖)
      - "skip": 软删除/无需推送, 直接返回
    """
    if op == "skip":
        logger.debug("[wecom_sync._push] skip table=%s", table)
        return True
    try:
        await _add_record(table, payload)
        # 用 RECORD_ID_FIELD 决定日志 key
        id_field = RECORD_ID_FIELD.get(table, "id")
        key = payload.get(id_field) or payload.get("wo_id") or payload.get("room_no")
        logger.info("[wecom_sync._push] ok table=%s op=%s key=%s", table, op, key)
        return True
    except Exception as exc:
        _enqueue(table, op, payload, str(exc))
        return False


def sync_now(table: str, op: str, payload: dict) -> None:
    """同步入口（plugin 写完本地 JSON 后调用，fire-and-forget，不阻塞主流程）"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_push(table, op, payload))
        else:
            loop.run_until_complete(_push(table, op, payload))
    except RuntimeError:
        # 没有 event loop（启动期）→ 用后台线程
        import threading
        def _runner():
            asyncio.run(_push(table, op, payload))
        threading.Thread(target=_runner, daemon=True).start()


async def safe_sync(table: str, payload: dict) -> None:
    """兼容旧 API：业务路由里常用 `await safe_sync(table, payload)`

    与 sync_now 等价,但 op 默认 'add'。
    主流程可写 `await safe_sync("work_orders", wo)` 而不必关心同步细节。
    """
    sync_now(table, "add", payload)

# ─────────────────────────────────────────────
# v2.1.17-safe: 静默版 safe_sync — 主流程不感知企微同步失败
# ─────────────────────────────────────────────

def safe_sync_quiet(table: str, payload: dict) -> None:
    """同步入口版,但吞掉所有异常。

    区别:
        - safe_sync: 内部 create_task 真崩了 task raise 不会传回路由层,
          但如果 _add_record 同步 raise (如 docid 缺失但实际无), 会炸路由
        - safe_sync_quiet: 在 safe_sync 基础上再 try/except 双保险,
          任何异常 → 进 stderr 日志,不影响主流程 200 返回

    用法 (路由层):
        try:
            safe_sync_quiet("work_orders", wo)  # 同步, fire-and-forget
        except Exception:
            pass  # 永远不会到这里,但保底
    """
    try:
        import asyncio
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        if _loop is not None:
            _loop.create_task(safe_sync(table, payload))
        else:
            _tmp = asyncio.new_event_loop()
            try:
                _tmp.run_until_complete(safe_sync(table, payload))
            finally:
                _tmp.close()
    except Exception as exc:
        import sys, traceback
        print(f"[safe_sync_quiet] {table} failed: {exc}\n{traceback.format_exc()}",
              file=sys.stderr, flush=True)


async def safe_sync_quiet_async(table: str, payload: dict) -> None:
    """async 版本 — 给 FastAPI async 路由用 (v2.1.16-hotfix4 replenish 用此版)

    同步 + 异步双保险: 即便 sync_now 内部 create_task 真崩,这条路径也兜底。
    """
    try:
        await safe_sync(table, payload)
    except Exception as exc:
        import sys, traceback
        print(f"[safe_sync_quiet_async] {table} failed: {exc}\n{traceback.format_exc()}",
              file=sys.stderr, flush=True)



# ─────────────────────────────────────────────
# 后台补传（每 60s 重试一次）
# ─────────────────────────────────────────────

async def retry_pending(max_n: int = 20) -> int:
    """重试失败队列里的任务，返回成功数"""
    queue = _load_queue()
    if not queue:
        return 0

    success = 0
    remaining: list[dict] = []
    for item in queue[:max_n]:
        try:
            if item.get("table") == NOTIFY_TABLE:
                _m = _work_orders_module()
                if not _m:
                    raise RuntimeError("work_orders 模块未加载, 无法补发通知")
                await _m.resend_staff_notice(item["payload"])
            else:
                await _add_record(item["table"], item["payload"])
            success += 1
            logger.info("wecom retry ok: table=%s key=%s",
                        item["table"], item.get("record_key"))
        except Exception as exc:
            item["retries"] = item.get("retries", 0) + 1
            item["last_error"] = str(exc)[:200]
            # 超过 10 次仍失败 → 永久放弃，留日志
            if item["retries"] < 10:
                remaining.append(item)
    _save_queue(remaining)
    return success


def start_retry_loop(interval_s: int = 60) -> None:
    """启动后台补传循环（plugin 加载时调用一次）

    v1.2.0:
      - 加防重入标志, 重复调用不会起多个循环
      - 无 running event loop 时改起 daemon 线程兑底
        (PluginLoader import 阶段未必有运行中的 loop)
    """
    global _retry_started
    if _retry_started:
        return
    _retry_started = True

    async def _loop():
        while True:
            try:
                await asyncio.sleep(interval_s)
                n = await retry_pending()
                if n > 0:
                    logger.info("wecom retry loop: %d recovered", n)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("wecom retry loop error: %s", exc)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_loop())
            logger.info("[wecom_sync] retry loop 已挂到运行中的 event loop")
            return
        raise RuntimeError("event loop 未运行")
    except RuntimeError:
        # 没有运行中的 loop (启动期/独立脚本) → daemon 线程跑独立 loop
        import threading

        def _runner():
            try:
                asyncio.run(_loop())
            except Exception as exc:
                logger.error("[wecom_sync] retry 线程退出: %s", exc)

        threading.Thread(target=_runner, daemon=True, name="wecom-retry").start()
        logger.info("[wecom_sync] retry loop 已在后台线程启动")


# ─────────────────────────────────────────────
# 诊断（运维用）
# ─────────────────────────────────────────────

# live_probe 结果缓存 (60s, 避免向导频繁刷新打企微)
_probe_cache: dict[str, Any] = {"ts": 0.0, "data": None}


async def live_probe(force: bool = False) -> dict:
    """真实连通性探测: gettoken + get_sheet (全部只读, 无任何写入)

    用途: /wecom/test_ping → 向导 Step 4 / 数据同步 tab,
    在配置阶段就把 60020 可信 IP / 凭证失效等问题暴露出来。

    force=True 时绕过 60s 缓存 (v1.3.3: 向导 Step4 门禁的「重新检测」用 —
    用户刚在企微后台配好可信 IP/改完凭证, 需要立即看到新结果)。

    返回: {checked, token_ok, api_ok, errcode, errmsg, client_ip,
           probe, checked_at}
      - token_ok=False         → corp_id/secret 无效 (40001 等)
      - token_ok=True 但 api_ok=False 且 errcode=60020 → 出口 IP 不在
        企微应用的可信 IP 白名单, client_ip 为探测到的出口 IP
      - api_ok=True            → 推送链路全程可用
    """
    import re as _re

    now_ts = time.time()
    if (not force) and _probe_cache["data"] and now_ts - _probe_cache["ts"] < 60:
        return dict(_probe_cache["data"])

    out: dict[str, Any] = {
        "checked": True,
        "token_ok": False,
        "api_ok": False,
        "errcode": None,
        "errmsg": "",
        "client_ip": "",
        "probe": "",
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 第一步: gettoken (不校验可信 IP, 只验证凭证本身)
    try:
        await _get_access_token()
        out["token_ok"] = True
        out["probe"] = "gettoken"
    except Exception as exc:
        out["errmsg"] = str(exc)[:300]
        _probe_cache.update(ts=now_ts, data=out)
        return out

    # 第二步: 用任一已配置的 docid 做只读探测 (get_sheet 受可信 IP 管控)
    docid = next((get_docid(t) for t in DEFAULT_DOCS if get_docid(t)), "")
    if not docid:
        out["probe"] = "gettoken_only"
        out["errmsg"] = "尚无智能表格 doc_id, 仅验证了凭证; 配置 WECOM_DOC_* 后可完整验证"
        _probe_cache.update(ts=now_ts, data=out)
        return out
    try:
        token = await _get_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/get_sheet?access_token={token}",
                json={"docid": docid},
            )
            data = r.json()
        out["probe"] = "get_sheet"
        if data.get("errcode") == 0:
            out["api_ok"] = True
        else:
            out["errcode"] = data.get("errcode")
            out["errmsg"] = str(data.get("errmsg", ""))[:300]
            m = _re.search(r"from ip:\s*([0-9.]+)", out["errmsg"])
            if m:
                out["client_ip"] = m.group(1)
    except Exception as exc:
        out["errmsg"] = str(exc)[:300]
    _probe_cache.update(ts=now_ts, data=out)
    return dict(out)


def queue_status() -> dict:
    """查询队列状态（给前端 /admin 面板用）"""
    queue = _load_queue()
    return {
        "pending": len(queue),
        "tables": {t: sum(1 for q in queue if q["table"] == t) for t in DEFAULT_DOCS},
        "oldest_failed_at": queue[0]["failed_at"] if queue else None,
    }