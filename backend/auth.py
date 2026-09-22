# -*- coding: utf-8 -*-
"""v2.1.15 权限系统 — 4 档角色 + 账号密码 + 限速 + 审计

设计目标:
  - 4 档角色: super_admin / manager / employee / guest
  - super_admin 通过 setup 接口首次创建,后续只能 super_admin 改
  - 至少保留 1 个 super_admin (防锁死)
  - 密码 PBKDF2-HMAC-SHA256 + per-user salt (标准库,零依赖)
  - 登录限速: 5 次/分钟/IP (auth_log.json)
  - session 走 cookie hotel_uid (1 年, HttpOnly)

权限矩阵:
  guest      — 只读
  employee   — 只读 + 接受派单(改自己状态)
  manager    — employee + 创建/派单/确认/删除工单 + 批量 upsert rooms + 部分 admin 操作
  super_admin— 全部,含改他人角色

注: 纯网页模式 (开发用) 默认 guest,无任何写权限,避免误删
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, Response

from . import data_layer

logger = logging.getLogger("hotel-frontdesk-pawapp.auth")

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 4 档角色 (严格枚举,大小写敏感)
ROLE_SUPER_ADMIN = "super_admin"
ROLE_MANAGER = "manager"
ROLE_EMPLOYEE = "employee"
ROLE_GUEST = "guest"

ALL_ROLES = {ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE, ROLE_GUEST}

# 权限位 (helper 用, 不用 bitmask 也行,这里只用 ROLE 字符串比较)
# 但为了可读, 给一个 rank 排名 (高 → 低)
ROLE_RANK = {
    ROLE_SUPER_ADMIN: 100,
    ROLE_MANAGER: 50,
    ROLE_EMPLOYEE: 10,
    ROLE_GUEST: 1,
}

# cookie 名
COOKIE_UID = "hotel_uid"
COOKIE_ROLE = "hotel_role"
COOKIE_NAME = "hotel_uid"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 年

# 密码哈希参数
PBKDF2_ITERS = 200_000
PBKDF2_ALGO = "sha256"
SALT_BYTES = 16
HASH_BYTES = 32

# 登录限速
LOGIN_FAIL_WINDOW = 60  # 60 秒窗口
LOGIN_FAIL_MAX = 5  # 最多 5 次

# 文件路径
SETUP_FILE = Path(data_layer.DATA_DIR) / "auth_setup.json"
LOG_FILE = Path(data_layer.DATA_DIR) / "auth_log.json"

# ─────────────────────────────────────────────
# AI 助手权限配置 (从 plugin.json 读取)
# ─────────────────────────────────────────────

_PLUGIN_JSON = Path(__file__).resolve().parent.parent / "plugin.json"
_agent_perm_cache: Optional[Dict[str, Any]] = None


def _load_agent_permissions() -> Dict[str, Any]:
    """从 plugin.json 读取 agent_permissions 配置 (带缓存)"""
    global _agent_perm_cache
    if _agent_perm_cache is not None:
        return _agent_perm_cache
    try:
        if _PLUGIN_JSON.is_file():
            cfg = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
            perm = cfg.get("agent_permissions") or {}
            _agent_perm_cache = {
                "enabled": bool(perm.get("enabled", False)),
                "allowed_agents": set(perm.get("allowed_agents") or []),
                "default_role": perm.get("default_role") or ROLE_MANAGER,
            }
            logger.info(
                "[auth] agent_permissions loaded: enabled=%s, agents=%d",
                _agent_perm_cache["enabled"],
                len(_agent_perm_cache["allowed_agents"]),
            )
            return _agent_perm_cache
    except Exception as exc:
        logger.warning("[auth] 读取 agent_permissions 失败: %s", exc)
    _agent_perm_cache = {"enabled": False, "allowed_agents": set(), "default_role": ROLE_MANAGER}
    return _agent_perm_cache


# ─────────────────────────────────────────────
# 密码哈希
# ─────────────────────────────────────────────

def hash_password(password: str, salt_hex: Optional[str] = None) -> Dict[str, str]:
    """PBKDF2-HMAC-SHA256 哈希密码

    Returns:
        {"salt": hex, "hash": hex}
    """
    if salt_hex is None:
        salt = secrets.token_bytes(SALT_BYTES)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(
        PBKDF2_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERS, dklen=HASH_BYTES
    )
    return {"salt": salt_hex, "hash": dk.hex()}


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    """验证明文密码是否匹配存储的 hash (constant-time 比较)"""
    actual = hash_password(password, salt_hex)
    return hmac.compare_digest(actual["hash"], expected_hash_hex)


# ─────────────────────────────────────────────
# Setup / Staff 角色字段操作
# ─────────────────────────────────────────────

def is_setup_done() -> bool:
    """是否已初始化过 super_admin (setup 文件存在即视为已初始化)"""
    return SETUP_FILE.is_file()


def get_setup_info() -> Optional[Dict[str, Any]]:
    if not is_setup_done():
        return None
    try:
        return json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def mark_setup_done(seed_admin_id: str, seed_admin_name: str) -> None:
    SETUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETUP_FILE.write_text(
        json.dumps(
            {
                "initialized_at": data_layer.now_str(),
                "seed_admin_id": seed_admin_id,
                "seed_admin_name": seed_admin_name,
                "version": "v2.1.15",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def mark_setup_undone() -> bool:
    """v2.1.16-hotfix2: 反向操作 — 删掉 auth_setup.json (setup_done=false)

    返回: True=删了, False=本来就没有
    """
    if SETUP_FILE.is_file():
        SETUP_FILE.unlink()
        return True
    return False


AUTH_SETUP_PATH = str(SETUP_FILE)  # v2.1.16-hotfix2: 让外部 dev 端点能拿到路径


def list_staff(include_deleted: bool = False) -> List[Dict[str, Any]]:
    rows = data_layer.load_table("staff")
    if not include_deleted:
        rows = [r for r in rows if not r.get("deleted")]
    return rows


def find_staff_by_name_or_phone(identifier: str) -> Optional[Dict[str, Any]]:
    """按 姓名 / 手机号 / id 找 staff"""
    identifier = identifier.strip()
    if not identifier:
        return None
    for s in list_staff(include_deleted=True):
        if s.get("deleted"):
            continue
        # v2.1.18: 兼容 id / staff_id 两种字段名
        sid = s.get("id") or s.get("staff_id") or ""
        if sid == identifier:
            return s
        if s.get("name") == identifier:
            return s
        if s.get("phone") == identifier:
            return s
    return None


def find_staff_by_id(staff_id: str) -> Optional[Dict[str, Any]]:
    if not staff_id:
        return None
    # 精确匹配 id/staff_id
    for s in list_staff(include_deleted=True):
        sid = s.get("id") or s.get("staff_id") or ""
        if sid == staff_id and not s.get("deleted"):
            return s
    # 兼容: cookie 可能存的是不带前缀的 name/phone (如 "LiXiaoYao" vs "staff-LiXiaoYao")
    for s in list_staff(include_deleted=True):
        if s.get("deleted"):
            continue
        sid = s.get("id") or s.get("staff_id") or ""
        name = s.get("name") or ""
        phone = s.get("phone") or ""
        # 去掉 "staff-" 前缀后匹配
        stripped = sid.replace("staff-", "", 1) if sid.startswith("staff-") else sid
        if staff_id in (stripped, name, phone):
            return s
    return None


def count_super_admins() -> int:
    return sum(1 for s in list_staff() if s.get("role") == ROLE_SUPER_ADMIN)


def ensure_staff_role_field(staff: Dict[str, Any]) -> Dict[str, Any]:
    """v2.1.15 迁移: 老 staff 记录 role 字段补默认 (兼容 v2.1.14 数据)"""
    if not staff.get("role"):
        staff["role"] = ROLE_GUEST  # 默认 guest (无权限)
    if "auth_password_hash" not in staff:
        staff["auth_password_hash"] = None
    if "auth_salt" not in staff:
        staff["auth_salt"] = None
    if "auth_failed_count" not in staff:
        staff["auth_failed_count"] = 0
    if "auth_locked_until" not in staff:
        staff["auth_locked_until"] = 0
    if "created_via" not in staff:
        staff["created_via"] = "manual"  # 老数据默认手动
    return staff


# ─────────────────────────────────────────────
# 审计日志
# ─────────────────────────────────────────────

def append_log(action: str, ok: bool, ip: str = "", user_id: str = "",
               reason: str = "") -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": data_layer.now_str(),
            "ts_epoch": int(time.time()),
            "action": action,
            "ok": ok,
            "ip": ip,
            "user_id": user_id,
            "reason": reason,
        }
        rows = []
        if LOG_FILE.is_file():
            try:
                rows = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                rows = []
        rows.append(entry)
        # 只保留最近 1000 条 (避免无限增长)
        rows = rows[-1000:]
        LOG_FILE.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("auth_log append failed: %s", exc)


def recent_login_fails(ip: str, window_s: int = LOGIN_FAIL_WINDOW) -> int:
    """查 IP 在最近 window_s 秒内的失败次数 (限速用)"""
    if not LOG_FILE.is_file():
        return 0
    try:
        rows = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    cutoff = int(time.time()) - window_s
    return sum(
        1
        for r in rows
        if r.get("action") == "login" and not r.get("ok") and r.get("ip") == ip
        and r.get("ts_epoch", 0) >= cutoff
    )


# ─────────────────────────────────────────────
# Session (cookie)
# ─────────────────────────────────────────────

def set_session(response: Response, user_id: str, role: str) -> None:
    response.set_cookie(
        COOKIE_UID,
        user_id,
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        COOKIE_ROLE,
        role,
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_UID, path="/")
    response.delete_cookie(COOKIE_ROLE, path="/")


def _get_cookie_or_header(request: Request, name: str) -> str:
    """v2.2.1-fix: 同时从 cookie 和自定义 header 读登录凭证

    解决 QwenPaw OS 桌面应用 / WebView 中 fetch 不自动携带 cookie 的问题:
    前端手动从 document.cookie 解析后通过 X-Hotel-Uid / X-Hotel-Role 头发送。
    """
    val = (request.cookies.get(name) or "").strip()
    if val:
        return val
    header_name = "x-hotel-uid" if name == COOKIE_UID else "x-hotel-role"
    return (request.headers.get(header_name) or "").strip()


def get_session(request: Request) -> Dict[str, Any]:
    """从 cookie 或 header 读 user_id,反查 staff 得到完整身份信息

    v2.1.19: 支持 X-Agent-Id 头, 内部 AI 助手免登录
    v2.1.20: 权限配置化 — 从 plugin.json 的 agent_permissions 白名单校验
    v2.2.1: 支持 X-Hotel-Uid / X-Hotel-Role header,兼容 OS 桌面 WebView cookie 丢失
    """
    # v2.1.20: 内部 Agent 识别 (配置化白名单)
    agent_id = (request.headers.get("x-agent-id") or "").strip()
    if agent_id:
        perm = _load_agent_permissions()
        if not perm["enabled"]:
            logger.warning("[auth] X-Agent-Id=%s 但 agent_permissions 未启用", agent_id)
            raise HTTPException(
                status_code=403,
                detail={"ok": False, "reason": "AI 助手权限未启用，请在 plugin.json 配置 agent_permissions"},
            )
        if agent_id not in perm["allowed_agents"]:
            logger.warning("[auth] X-Agent-Id=%s 不在白名单中", agent_id)
            raise HTTPException(
                status_code=403,
                detail={"ok": False, "reason": f"AI 助手 {agent_id} 未授权"},
            )
        role = perm["default_role"]
        return {
            "user_id": f"agent:{agent_id}",
            "name": f"AI:{agent_id}",
            "role": role,
            "staff": None,
            "is_super_admin": role == ROLE_SUPER_ADMIN,
            "is_manager": role in (ROLE_SUPER_ADMIN, ROLE_MANAGER),
            "is_employee": role in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE),
            "is_guest": False,
            "_agent_id": agent_id,
        }

    uid = _get_cookie_or_header(request, COOKIE_UID)
    if not uid:
        return {"user_id": "", "name": "", "role": ROLE_GUEST, "staff": None,
                "is_super_admin": False, "is_manager": False, "is_employee": False, "is_guest": True}
    staff = find_staff_by_id(uid)
    if not staff:
        return {"user_id": uid, "name": "", "role": ROLE_GUEST, "staff": None,
                "is_super_admin": False, "is_manager": False, "is_employee": False, "is_guest": True}
    role = staff.get("role") or ROLE_GUEST
    if role not in ALL_ROLES:
        role = ROLE_GUEST
    return {
        "user_id": uid,
        "name": staff.get("name", ""),
        "role": role,
        "staff": staff,
        "is_super_admin": role == ROLE_SUPER_ADMIN,
        "is_manager": role in (ROLE_SUPER_ADMIN, ROLE_MANAGER),
        "is_employee": role in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE),
        "is_guest": role == ROLE_GUEST,
    }


def get_current_session(request: Request) -> Dict[str, Any]:
    """FastAPI Depends 用的版本"""
    return get_session(request)


# ─────────────────────────────────────────────
# 权限校验 (FastAPI Depends)
# ─────────────────────────────────────────────

def _require(role_min: str):
    """构造一个 Depends 工厂,要求 session.role rank >= role_min"""
    async def dep(request: Request) -> Dict[str, Any]:
        sess = get_session(request)
        sess_rank = ROLE_RANK.get(sess["role"], 0)
        need_rank = ROLE_RANK.get(role_min, 0)
        if sess_rank < need_rank:
            raise HTTPException(
                status_code=403,
                detail={
                    "ok": False,
                    "reason": f"需要 {role_min} 权限,当前身份 {sess['role']}",
                    "current_role": sess["role"],
                    "required_role": role_min,
                },
            )
        return sess
    return dep


def require_super_admin():
    return _require(ROLE_SUPER_ADMIN)


def require_manager():
    return _require(ROLE_MANAGER)


def require_employee():
    return _require(ROLE_EMPLOYEE)


# ─────────────────────────────────────────────
# 写操作辅助 (给 routes/ 模块复用)
# ─────────────────────────────────────────────

def save_staff_rows(rows: List[Dict[str, Any]]) -> None:
    data_layer.save_table("staff", rows)


def load_staff_rows() -> List[Dict[str, Any]]:
    rows = data_layer.load_table("staff")
    for r in rows:
        ensure_staff_role_field(r)
    return rows


# ─────────────────────────────────────────────
# v2.1.17 工具函数(消除 9 处重复)
# ─────────────────────────────────────────────

def find_active_staff(staff_id):
    for r in load_staff_rows():
        rid = r.get("id") or r.get("staff_id") or ""
        if rid == staff_id and not r.get("deleted"):
            return r
    return None


def find_active_staff_by_name(name):
    for r in load_staff_rows():
        if r.get("name") == name and not r.get("deleted"):
            return r
    return None


def find_active_staff_by_phone(phone):
    for r in load_staff_rows():
        if r.get("phone") == phone and not r.get("deleted"):
            return r
    return None


def count_super_admins(exclude_id=""):
    return sum(
        1 for r in load_staff_rows()
        if r.get("role") == ROLE_SUPER_ADMIN
        and not r.get("deleted")
        and (r.get("id") or r.get("staff_id") or "") != exclude_id
    )


def update_staff(staff_id, updates):
    from fastapi import HTTPException as _HTTPException
    rows = load_staff_rows()
    target = None
    for r in rows:
        rid = r.get("id") or r.get("staff_id") or ""
        if rid == staff_id and not r.get("deleted"):
            target = r
            break
    if not target:
        raise _HTTPException(status_code=404, detail=f"staff {staff_id} 不存在")
    target.update(updates)
    target["updated_at"] = now()
    save_staff_rows(rows)
    return target


def now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")



def validate_password_strength(pw: str) -> None:
    """密码强度校验 (≥6 位, 含字母或数字)

    v2.1.15 默认: 6 位起步 (兼容默认账号 domai/123456), 必须含字母或数字
    注: 这是开发/测试环境的宽松策略, 生产环境建议把 ≥6 改回 ≥10 + 必须含大小写+符号
    """
    if len(pw) < 6:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if not re.search(r"[A-Za-z]", pw) and not re.search(r"\d", pw):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="密码必须含字母或数字")
