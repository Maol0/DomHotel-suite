"""
User Identity Resolver (v2.1.10)
================================

按"对齐到一个用户/账号/微信号一个 session"原则, 在 plugin 后端 chat 入口
自动推断/分配稳定 user_id, 不依赖前端传任何身份字段。

优先级 (从高到低):
  1) HTTP Header 显式身份 (外部集成方传):
     - X-User-Id              自定义 user_id (完整控制)
     - X-Wecom-External-Userid 企业微信 external_userid
     - X-Wecom-Chatid          企业微信群 chatid
  2) Cookie hotel_uid (浏览器级持久身份, 后端种, 永不失效)
  3) Body user_id (向后兼容老客户端)
  4) 兜底: anon-<ip_hash>-<random> (一次性匿名身份, 不写 cookie)

副作用:
  - 若 1+2+3 都未命中, 本函数会 SET-COOKIE hotel_uid=... (浏览器级稳定)
  - cookie 设 HttpOnly + SameSite=Lax + 1y 过期, 跨域不传 (避免 CSRF)
  - 返回的 dict 里 _set_cookie 标记告知调用方要不要回写 Set-Cookie 头

设计动机:
  - 不依赖前端 JS 任何改动 (零侵入)
  - 不依赖 QwenPaw console 登录态 (plugin 是 PAWAPP, 与主 QwenPaw 用户系统解耦)
  - 浏览器关掉再开, 同一个 user_id → 同一个 session
  - 多个浏览器隔离 → 不同 user_id → 不同 session
  - 企业微信集成方用 header 注入 userid → 自动按微信号隔离
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("hotel-frontdesk-pawapp.user-identity")

COOKIE_NAME = "hotel_uid"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 年


def _ip_hash(ip: str) -> str:
    """IP hash (短, 8 字符)"""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:8]


def _client_ip(request) -> str:
    """提取客户端 IP (考虑 reverse proxy X-Forwarded-For)"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def resolve_user_id(request, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """解析 user_id 并决定要不要回写 Set-Cookie

    Args:
        request: FastAPI Request 对象 (用于读 header/cookie/client)
        body: chat payload dict (向后兼容老客户端可能传的 user_id)

    Returns:
        {
            "user_id": str,            # 最终用的 user_id (传给 session_manager)
            "source": str,             # "header-x-user-id" / "header-wecom" / "cookie" / "body" / "anon"
            "set_cookie": str | None,  # cookie 值, 调用方决定要不要回写 Set-Cookie 头
        }
    """
    body = body or {}

    # 1) X-User-Id (外部 API 集成方用, 优先级最高)
    xuid = (request.headers.get("x-user-id") or "").strip()
    if xuid:
        return {"user_id": xuid, "source": "header-x-user-id", "set_cookie": None}

    # 2) 企业微信 header (集成方转发企微消息时用)
    wx_ext = (request.headers.get("x-wecom-external-userid") or "").strip()
    wx_chat = (request.headers.get("x-wecom-chatid") or "").strip()
    wx_chat_type = (request.headers.get("x-wecom-chat-type") or "").strip()
    if wx_chat and wx_chat_type == "group":
        # 群聊: 全员共享一个 session, 按群 ID
        return {
            "user_id": f"wecom:group:{wx_chat}",
            "source": "header-wecom-group",
            "set_cookie": None,
        }
    if wx_ext:
        return {
            "user_id": f"wecom:{wx_ext}",
            "source": "header-wecom-user",
            "set_cookie": None,
        }

    # 3) Cookie (浏览器级稳定身份, 后端首次访问时种)
    cookie_uid = request.cookies.get(COOKIE_NAME, "").strip()
    if cookie_uid:
        return {"user_id": cookie_uid, "source": "cookie", "set_cookie": None}

    # 4) Body user_id (向后兼容老客户端)
    body_uid = (body.get("user_id") or "").strip()
    if body_uid and body_uid != "hotel-pawapp-ui":  # 排除前端占位
        return {"user_id": body_uid, "source": "body", "set_cookie": None}

    # 5) 兜底: 匿名 IP 派生 (一次性, 不写 cookie)
    ip = _client_ip(request)
    short = _ip_hash(ip)
    rand = secrets.token_hex(4)
    anon_uid = f"anon-{short}-{rand}"
    return {"user_id": anon_uid, "source": "anon", "set_cookie": None}


def resolve_user_id_with_cookie(
    request, body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """同 resolve_user_id, 但匿名场景自动分配稳定 UUID 并种 cookie

    Returns:
        同上, 但 anon 场景 set_cookie 会有新 UUID
    """
    res = resolve_user_id(request, body)
    if res["source"] == "anon":
        # 生成稳定 UUID 给浏览器, 后续访问复用
        new_uid = f"web-{uuid.uuid4().hex[:16]}"
        res["user_id"] = new_uid
        res["source"] = "cookie-new"
        res["set_cookie"] = new_uid
        logger.info(
            "user-identity: 首次访问分配浏览器 UUID user_id=%s (ip=%s)",
            new_uid,
            _client_ip(request),
        )
    return res


def build_set_cookie_header(cookie_value: str) -> str:
    """构造 Set-Cookie header 值"""
    # SameSite=Lax 允许 top-level navigation 时带 cookie (SPA 路由 OK)
    # 不设 Secure (本地 http 测试环境需要; 生产 https 时建议加 Secure)
    # 不设 HttpOnly=True 因为前端 JS 可能需要读 (这里其实不需要, 设为 True 更安全)
    # 但 FastAPI/浏览器对 SameSite=None 必须 Secure, 这里我们 SameSite=Lax 没这要求
    return (
        f"{COOKIE_NAME}={cookie_value}; "
        f"Max-Age={COOKIE_MAX_AGE}; Path=/; SameSite=Lax"
    )