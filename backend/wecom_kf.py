# -*- coding: utf-8 -*-
"""企微微信客服模块 — 客人端收发 + AI 应答 (v1.4.0 正确架构版)

v1.4.0 架构 (对齐企微官方微信客服正确模式, 修复旧版收不到消息的问题):

  旧版 (错误): 回调直接解析明文 XML 拿消息 —— 实际企微回调推的是 AES 加密的
    「事件通知」(不含消息体), 旧逻辑即使配好凭证也永远收不到客人消息。

  新版 (正确):
    ① 企微 POST 加密事件通知 (Event=kf_msg_or_event, 带 Token/OpenKfId) 到 /kf/callback
    ② SHA1 验签 + AES-256-CBC 解密 → 拿到事件 Token
    ③ 用 Token 调 POST /cgi-bin/kf/sync_msg 增量拉取消息 (cursor 持久化, 崩溃不丢)
    ④ 逐条处理: enter_session 事件 → 欢迎语 (send_msg_on_event, welcome_code 20s 有效);
       客人文本 → 命令模式 (绑定/状态/报修/解绑) / 其余 → QwenPaw 智能体 AI 应答
    ⑤ 回复客人 (send_msg), 报修等重要动作同步通知内部员工 (应用消息 message/send)

  回调 5 秒超时约束: 拉取+AI 应答 (30~60s) 放 FastAPI BackgroundTasks, 回调立即返回 "ok"。

配置 (运行时配置优先, env 兜底; 面板/智能体工具均可写, 免改 env 免重启):
  - WECOM_KF_ID               客服账号 ID (open_kfid)
  - WECOM_KF_TOKEN            回调 URL 验证 Token
  - WECOM_KF_ENCODING_AES_KEY 回调加密 EncodingAESKey (43 位)
  - KF_AI_AGENT_ID            AI 应答用 QwenPaw 智能体 (默认 hotel-ai-guest-service)
  - KF_NOTIFY_USERIDS         内部通知员工企微 userid (逗号分隔)
  - WECOM_AGENT_ID            自建应用 AgentId 数字 (应用消息用)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from . import data_layer
from . import guest_profile
from . import wecom_sync
from . import chat_session_manager
from .wecom_sync import WECOM_BASE_URL

# v1.5.1-fix 循环导入: 顶层模块不能反向依赖 routes 包 —
# `from .routes._helpers import now, new_id` 会触发 routes/__init__.py 全量加载,
# 其中 dispatch.py 顶层 `from ..wecom_kf import notify_staff_by_userids` 而此时
# wecom_kf 尚未初始化完成 (partially initialized), 预加载直接失败。
# now/new_id 是纯函数 (datetime+uuid), 本地等价实现即可。
import uuid as _uuid
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI


def now() -> str:
    # 与 routes/_helpers.now 完全一致: 固定北京时间 (+8)
    return _dt.now(_ZI("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}-{_dt.now(_ZI('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')}-{_uuid.uuid4().hex[:6]}"

# v1.4.1: 客服会话质检/留痕
_KF_SESSION_LOG = data_layer.DATA_DIR / "kf_sessions.jsonl"
_KF_SESSION_MAX_LEN = 200

logger = logging.getLogger(__name__)

# 回调 XML 来自公网, 解析前拒绝 DTD/实体声明 (防实体扩展/XXE, 不引新依赖)
_FORBIDDEN_XML = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def _safe_xml_fromstring(text: str) -> ET.Element:
    if _FORBIDDEN_XML.search(text or ""):
        raise ValueError("XML 含 DTD/实体声明, 已拒绝")
    return ET.fromstring(text)

# ─────────────────────────────────────────────
# 配置 (v1.4.0: 运行时配置优先, env 兜底 — 与智能表格凭证同一套机制)
# ─────────────────────────────────────────────

def get_kf_id() -> str:
    """客服账号 ID (open_kfid)"""
    return wecom_sync.get_kf_setting("WECOM_KF_ID")


def get_kf_token() -> str:
    """回调 URL 验证 Token"""
    return wecom_sync.get_kf_setting("WECOM_KF_TOKEN")


def get_kf_aes_key() -> str:
    """回调加密 EncodingAESKey (43 位)"""
    return wecom_sync.get_kf_setting("WECOM_KF_ENCODING_AES_KEY")


def get_kf_ai_agent() -> str:
    """AI 应答用 QwenPaw 智能体 ID"""
    return wecom_sync.get_kf_setting("KF_AI_AGENT_ID", "hotel-ai-guest-service")


def get_kf_notify_userids() -> List[str]:
    """内部通知员工企微 userid 列表 (逗号/中文逗号分隔)"""
    raw = wecom_sync.get_kf_setting("KF_NOTIFY_USERIDS")
    return [u.strip() for u in raw.replace("，", ",").split(",") if u.strip()]


def is_configured() -> bool:
    """微信客服收发链路是否配齐 (客服账号 + 回调验证 + 加解密密钥)"""
    return bool(get_kf_id() and get_kf_token() and get_kf_aes_key())


# sync_msg 游标与去重状态 (非业务表, 独立 JSON — 与 auth_setup 等同类)
_KF_STATE_FILE = Path(data_layer.DATA_DIR) / "kf_state.json"

# msgid 去重窗口: 企微可能重推事件通知, cursor 保存前崩溃也会重复拉取
_RECENT_MSGID_LIMIT = 200

# 防止回调与定时拉取并发处理同一消息导致重复回复
import asyncio as _asyncio
_sync_lock: Optional[_asyncio.Lock] = None


def _get_sync_lock() -> _asyncio.Lock:
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = _asyncio.Lock()
    return _sync_lock


def _load_kf_state() -> Dict[str, Any]:
    try:
        data = json.loads(_KF_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_kf_state(state: Dict[str, Any]) -> None:
    try:
        _KF_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[kf] 保存 sync 状态失败(忽略): %s", e)


# ─────────────────────────────────────────────
# 客人绑定数据管理
# ─────────────────────────────────────────────

def load_customers() -> List[Dict[str, Any]]:
    """加载客人绑定数据"""
    try:
        return data_layer.load_table("customers")
    except Exception:
        return []


def save_customers(customers: List[Dict[str, Any]]) -> None:
    """保存客人绑定数据"""
    data_layer.save_table("customers", customers)


def find_customer_by_external_userid(external_userid: str) -> Optional[Dict[str, Any]]:
    """通过企微 external_userid 查找客人

    优先返回当前活跃记录（deleted=false），没有活跃记录时才返回最近的历史记录。
    同一个客人可能有多条记录（退房后重新入住），取活跃的那条。
    """
    active = None
    latest_deleted = None
    for c in load_customers():
        if c.get("external_userid") != external_userid:
            continue
        if not c.get("deleted"):
            active = c
        else:
            latest_deleted = c
    return active or latest_deleted


def find_active_customer_by_external_userid(external_userid: str) -> Optional[Dict[str, Any]]:
    """通过企微 external_userid 查找活跃客人（未退房）"""
    for c in load_customers():
        if c.get("external_userid") == external_userid and not c.get("deleted"):
            return c
    return None


def find_customer_by_openid(openid: str) -> Optional[Dict[str, Any]]:
    """通过微信 OpenID 查找客人

    优先返回当前活跃记录（deleted=false），没有活跃记录时才返回最近的历史记录。
    """
    if not openid:
        return None
    active = None
    latest_deleted = None
    for c in load_customers():
        if c.get("openid") != openid:
            continue
        if not c.get("deleted"):
            active = c
        else:
            latest_deleted = c
    return active or latest_deleted


async def convert_external_userid_to_openid(external_userid: str) -> str:
    """将企微 external_userid (wm 开头) 转换为微信 OpenID

    API: POST /cgi-bin/externalcontact/convert_to_openid
    前提: 自建应用需开通「客户联系 → 通讯录 → 外部联系人」权限
    """
    if not external_userid or not external_userid.startswith("wm"):
        return ""
    try:
        token = await _get_kf_access_token()
        url = f"{WECOM_BASE_URL}/cgi-bin/externalcontact/convert_to_openid?access_token={token}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"external_userid": external_userid})
            data = r.json()
        if data.get("errcode") == 0:
            openid = data.get("openid", "")
            logger.info("[kf] OpenID 转换成功: %s → %s", external_userid[:12], openid[:8])
            return openid
        else:
            logger.warning("[kf] OpenID 转换失败: errcode=%s errmsg=%s",
                           data.get("errcode"), data.get("errmsg"))
            return ""
    except Exception as e:
        logger.warning("[kf] OpenID 转换异常: %s", e)
        return ""


def find_customer_by_room(room_no: str, guest_name: str = "") -> List[Dict[str, Any]]:
    """通过房间号查找客人（可能有多人）"""
    result = []
    for c in load_customers():
        if c.get("deleted"):
            continue
        if c.get("room_no") == room_no:
            if guest_name and guest_name not in c.get("guest_name", ""):
                continue
            result.append(c)
    return result


def bind_customer(
    external_userid: str,
    room_no: str,
    guest_name: str,
    guest_phone: str = "",
    nickname: str = "",
    openid: str = "",
) -> Dict[str, Any]:
    """绑定客人：external_userid ↔ room_no + guest_name

    如果已存在相同 external_userid 的记录（含已退房的历史客人），重新绑定房间。
    如果已存在相同 room_no + guest_name 的记录，关联 external_userid。
    """
    customers = load_customers()

    existing = None
    for c in customers:
        # 查找已退房的历史客人（同一 external_userid，可能 deleted=True）
        if c.get("external_userid") == external_userid:
            existing = c
            break

    # 回访客人：重新绑定房间（从 deleted 状态恢复）
    if existing and existing.get("deleted"):
        existing["deleted"] = False
        existing["room_no"] = room_no
        existing["guest_name"] = guest_name
        if guest_phone:
            existing["guest_phone"] = guest_phone
        if nickname:
            existing["nickname"] = nickname
        if openid:
            existing["openid"] = openid
        existing["unbound_at"] = ""
        existing["checkout_at"] = ""
        existing["updated_at"] = now()
        existing["last_active_at"] = now()
        existing["return_visits"] = existing.get("return_visits", 0) + 1
        save_customers(customers)
        return existing

    # 已有活跃记录，更新房间信息
    if existing and not existing.get("deleted"):
        existing["room_no"] = room_no
        existing["guest_name"] = guest_name
        if guest_phone:
            existing["guest_phone"] = guest_phone
        if nickname:
            existing["nickname"] = nickname
        if openid:
            existing["openid"] = openid
        existing["updated_at"] = now()
        existing["last_active_at"] = now()
        save_customers(customers)
        return existing

    # 全新客人
    customer = {
        "customer_id": new_id("CUST"),
        "external_userid": external_userid,
        "openid": openid,
        "room_no": room_no,
        "guest_name": guest_name,
        "guest_phone": guest_phone,
        "nickname": nickname,
        "bound_at": now(),
        "last_active_at": now(),
        "updated_at": now(),
        "created_at": now(),
        "deleted": False,
        "return_visits": 0,
    }
    customers.append(customer)
    save_customers(customers)
    return customer


def unbind_customer(external_userid: str) -> bool:
    """解绑客人（退房时调用）"""
    customers = load_customers()
    for c in customers:
        if c.get("external_userid") == external_userid:
            c["deleted"] = True
            c["unbound_at"] = now()
            c["updated_at"] = now()
            save_customers(customers)
            return True
    return False


# ─────────────────────────────────────────────
# 企微客服消息 API
# ─────────────────────────────────────────────

async def _get_kf_access_token() -> str:
    """获取客服 access_token（与普通 access_token 共用, 走运行时配置层）"""
    return await wecom_sync.get_access_token()


async def send_kf_message(
    external_userid: str,
    msg_type: str,
    content: str,
    kf_id: str = "",
) -> Dict[str, Any]:
    """发送客服消息给客人

    Args:
        external_userid: 客人的企微外部联系人 ID
        msg_type: 消息类型（text/image/link/miniprogram）
        content: 消息内容（text 类型为纯文本，其他类型为 JSON 字符串）
        kf_id: 客服账号 ID（默认用配置的）

    Returns:
        {"ok": True/False, "errcode": ..., "errmsg": ...}
    """
    kf_id = kf_id or get_kf_id()
    if not kf_id:
        return {"ok": False, "errcode": -1, "errmsg": "WECOM_KF_ID 未配置"}

    try:
        token = await _get_kf_access_token()
    except Exception as e:
        return {"ok": False, "errcode": -1, "errmsg": f"获取 access_token 失败: {e}"}

    url = f"{WECOM_BASE_URL}/cgi-bin/kf/send_msg?access_token={token}"

    msg_body: Dict[str, Any] = {
        "touser": external_userid,
        "open_kfid": kf_id,
        "msgtype": msg_type,
    }

    if msg_type == "text":
        msg_body["text"] = {"content": content}
    elif msg_type == "link":
        msg_body["link"] = json.loads(content)
    elif msg_type == "miniprogram":
        msg_body["miniprogram"] = json.loads(content)
    else:
        return {"ok": False, "errcode": -1, "errmsg": f"不支持的消息类型: {msg_type}"}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=msg_body)
        data = r.json()

    if data.get("errcode", 0) != 0:
        logger.warning("[kf] 发送消息失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}

    logger.info("[kf] 消息已发送: to=%s type=%s", external_userid, msg_type)
    return {"ok": True, "errcode": 0, "errmsg": "ok"}


async def send_welcome_message(code: str, content: str) -> Dict[str, Any]:
    """发送事件响应消息（进入会话欢迎语）

    用 enter_session 事件返回的 welcome_code 调 /cgi-bin/kf/send_msg_on_event。
    注意: welcome_code 20 秒内有效且仅可用一次 —— 事件处理后须立刻调用。
    """
    try:
        token = await _get_kf_access_token()
    except Exception as e:
        return {"ok": False, "errcode": -1, "errmsg": f"获取 access_token 失败: {e}"}

    url = f"{WECOM_BASE_URL}/cgi-bin/kf/send_msg_on_event?access_token={token}"
    body = {"code": code, "msgtype": "text", "text": {"content": content}}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=body)
        data = r.json()

    if data.get("errcode", 0) != 0:
        logger.warning("[kf] 欢迎语发送失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
    logger.info("[kf] 欢迎语已发送")
    return {"ok": True, "errcode": 0, "errmsg": "ok"}


# ─────────────────────────────────────────────
# 模板消息
# ─────────────────────────────────────────────

TEMPLATES = {
    "repair_received": {
        "title": "🔧 报修已受理",
        "content": (
            "尊敬的{guest_name}，您在 {room_no} 房间提交的报修已收到：\n"
            "📋 故障：{description}\n"
            "⏰ 提交时间：{created_at}\n\n"
            "我们会尽快安排维修人员处理，请耐心等候。"
        ),
    },
    "repair_assigned": {
        "title": "👷 维修人员已派出",
        "content": (
            "尊敬的{guest_name}，您的报修已安排维修人员：\n"
            "👨‍🔧 维修人员：{assignee}\n"
            "📋 故障：{description}\n"
            "⏰ 预计到达：15分钟内\n\n"
            "如有问题请拨打前台电话。"
        ),
    },
    "repair_in_progress": {
        "title": "🔧 维修进行中",
        "content": (
            "尊敬的{guest_name}，维修人员已到达您的房间：\n"
            "👨‍🔧 维修人员：{assignee}\n"
            "📋 故障：{description}\n\n"
            "维修中，请稍候。"
        ),
    },
    "repair_done": {
        "title": "✅ 维修已完成",
        "content": (
            "尊敬的{guest_name}，您在 {room_no} 房间的报修已完成：\n"
            "📋 故障：{description}\n"
            "✅ 结果：{result_note}\n"
            "⏰ 完成时间：{completed_at}\n\n"
            "感谢您的耐心，祝您入住愉快！"
        ),
    },
    "request_received": {
        "title": "📦 需求已受理",
        "content": (
            "尊敬的{guest_name}，您的需求已收到：\n"
            "📋 需求：{description}\n"
            "⏰ 提交时间：{created_at}\n\n"
            "我们会尽快处理，请耐心等候。"
        ),
    },
    "request_done": {
        "title": "✅ 需求已完成",
        "content": (
            "尊敬的{guest_name}，您的需求已处理完毕：\n"
            "📋 需求：{description}\n"
            "⏰ 完成时间：{completed_at}\n\n"
            "如有其他需要请随时联系我们。"
        ),
    },
}


def render_template(template_key: str, variables: Dict[str, str]) -> str:
    """渲染消息模板"""
    template = TEMPLATES.get(template_key)
    if not template:
        return f"未知模板: {template_key}"
    content = template["content"]
    for key, value in variables.items():
        content = content.replace(f"{{{key}}}", str(value))
    return content


# ─────────────────────────────────────────────
# 工单状态变更推送
# ─────────────────────────────────────────────

async def notify_guest_work_order_change(
    wo: Dict[str, Any],
    event: str,
) -> Dict[str, Any]:
    """工单状态变更时通知客人（仅限客人自己提交的工单）

    Args:
        wo: 工单数据
        event: 事件类型（received/assigned/in_progress/done）

    Returns:
        {"ok": True/False, "sent": True/False, "reason": "..."}
    """
    # 只通知客人自己提交的工单（通过微信客服/H5/客人端），
    # 员工手动创建的清洁/维修等工单不通知客人
    ds = (wo.get("data_source") or "").lower()
    is_guest = (
        ds in ("guest", "guest_kf", "guest_h5")
        or bool(wo.get("guest_id"))
        or "guest" in str(wo.get("reporter", "")).lower()
    )
    if not is_guest:
        return {"ok": True, "sent": False,
                "reason": f"非客人来源工单(data_source={ds}), 跳过客人通知"}

    room_no = wo.get("room_no", "")
    if not room_no:
        return {"ok": True, "sent": False, "reason": "无房间号，跳过推送"}

    # 查找客人绑定（find_customer_by_room 已过滤 deleted，只有活跃绑定才通知）
    customers = find_customer_by_room(room_no)
    if not customers:
        return {"ok": True, "sent": False, "reason": f"房间 {room_no} 未绑定企微客服"}

    work_type = wo.get("work_type", "")
    if work_type == "维修":
        template_map = {
            "received": "repair_received",
            "assigned": "repair_assigned",
            "in_progress": "repair_in_progress",
            "done": "repair_done",
        }
    else:
        template_map = {
            "received": "request_received",
            "assigned": "request_received",
            "in_progress": "request_received",
            "done": "request_done",
        }
    template_key = template_map.get(event)
    if not template_key:
        return {"ok": True, "sent": False, "reason": f"未映射的事件类型: {event}"}

    variables = {
        "room_no": room_no,
        "guest_name": "",
        "description": wo.get("description", ""),
        "assignee": wo.get("assignee", ""),
        "result_note": wo.get("result_note", ""),
        "created_at": wo.get("created_at", ""),
        "completed_at": wo.get("completed_at", ""),
    }

    results = []
    for customer in customers:
        ext_uid = customer.get("external_userid", "")
        if not ext_uid:
            continue

        variables["guest_name"] = customer.get("guest_name", "")
        content = render_template(template_key, variables)

        result = await send_kf_message(ext_uid, "text", content)
        results.append({
            "customer_id": customer.get("customer_id"),
            "external_userid": ext_uid,
            "guest_name": customer.get("guest_name"),
            "send_result": result,
        })

        customer["last_active_at"] = now()

    save_customers(load_customers())

    sent_count = sum(1 for r in results if r["send_result"].get("ok"))
    return {
        "ok": True,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "total_customers": len(customers),
        "results": results,
    }


# ─────────────────────────────────────────────
# 内部员工通知 (v1.4.0: 自建应用消息 message/send)
# ─────────────────────────────────────────────

async def notify_staff(title: str, detail: str = "") -> Dict[str, Any]:
    """内部员工通知 — 自建应用文本消息 POST /cgi-bin/message/send

    需运行时配置: KF_NOTIFY_USERIDS (员工企微 userid, 逗号分隔) +
    WECOM_AGENT_ID (自建应用 AgentId 数字)。
    未配置时静默跳过 (skipped=True), 不影响主流程。
    """
    userids = get_kf_notify_userids()
    agentid = wecom_sync.get_kf_setting("WECOM_AGENT_ID")
    if not userids or not agentid:
        return {"ok": False, "skipped": True,
                "reason": "KF_NOTIFY_USERIDS / WECOM_AGENT_ID 未配置, 跳过内部通知"}

    try:
        token = await _get_kf_access_token()
    except Exception as e:
        return {"ok": False, "reason": f"获取 access_token 失败: {e}"}

    content = title if not detail else f"{title}\n{detail}"
    url = f"{WECOM_BASE_URL}/cgi-bin/message/send?access_token={token}"
    body = {
        "touser": "|".join(userids),
        "msgtype": "text",
        "agentid": int(agentid),
        "text": {"content": content},
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=body)
            data = r.json()
    except Exception as e:
        logger.warning("[kf] 内部通知请求失败: %s", e)
        return {"ok": False, "reason": str(e)}

    if data.get("errcode", 0) != 0:
        logger.warning("[kf] 内部通知失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
    logger.info("[kf] 内部通知已发送: %s 位员工", len(userids))
    return {"ok": True, "notified": len(userids)}


async def notify_staff_by_userids(userids: List[str], title: str, detail: str = "") -> Dict[str, Any]:
    """指定员工 userid 列表发企微应用消息（message/send）

    与 notify_staff 区别：调用方自行决定通知对象，不读 KF_NOTIFY_USERIDS。
    """
    agentid = wecom_sync.get_kf_setting("WECOM_AGENT_ID")
    if not userids or not agentid:
        return {"ok": False, "skipped": True,
                "reason": "userids 为空 / WECOM_AGENT_ID 未配置, 跳过通知"}

    try:
        token = await _get_kf_access_token()
    except Exception as e:
        return {"ok": False, "reason": f"获取 access_token 失败: {e}"}

    content = title if not detail else f"{title}\n{detail}"
    url = f"{WECOM_BASE_URL}/cgi-bin/message/send?access_token={token}"
    body = {
        "touser": "|".join(userids),
        "msgtype": "text",
        "agentid": int(agentid),
        "text": {"content": content},
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=body)
            data = r.json()
    except Exception as e:
        logger.warning("[kf] 指定员工通知请求失败: %s", e)
        return {"ok": False, "reason": str(e)}

    if data.get("errcode", 0) != 0:
        logger.warning("[kf] 指定员工通知失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
    logger.info("[kf] 指定员工通知已发送: %s 位", len(userids))
    return {"ok": True, "notified": len(userids)}


# ─────────────────────────────────────────────
# 回调加解密 (企微标准 WXBizMsgCrypt 算法, v1.4.0)
# ─────────────────────────────────────────────

def _aes_key_bytes() -> bytes:
    """EncodingAESKey (43 位 Base64) → 32 字节 AES 密钥"""
    aes_key = get_kf_aes_key()
    if not aes_key:
        raise ValueError("WECOM_KF_ENCODING_AES_KEY 未配置")
    return base64.b64decode(aes_key + "=")


def _sha1_sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企微回调签名: sha1(字典序排序后的 token/timestamp/nonce/encrypt 拼接)"""
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def _aes_decrypt(encrypt_b64: str) -> str:
    """AES-256-CBC 解密企微回调密文 (IV = key 前 16 字节)

    明文结构: random(16B) + msg_len(4B 网络字节序) + msg + receiveid
    """
    key = _aes_key_bytes()
    data = base64.b64decode(encrypt_b64)
    if not data or len(data) % 16 != 0:
        raise ValueError("密文长度非法")

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()

    pad = padded[-1]
    if pad < 1 or pad > 32:
        raise ValueError("PKCS7 填充非法")
    plain = padded[:-pad]

    msg_len = int.from_bytes(plain[16:20], "big")
    if 20 + msg_len > len(plain):
        raise ValueError("msg_len 越界")
    return plain[20:20 + msg_len].decode("utf-8")


def verify_kf_callback(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
    """验证回调 URL (GET): 验签 + 解密 echostr

    企微后台保存回调 URL 时会 GET 本端点, 必须返回解密后的明文 echostr。
    Returns: 解密后的明文 (空串 = 验证失败)
    """
    token = get_kf_token()
    if not token or not msg_signature:
        return ""
    if _sha1_sign(token, timestamp, nonce, echostr) != msg_signature:
        logger.warning("[kf] 回调 URL 验证签名不匹配")
        return ""
    try:
        return _aes_decrypt(echostr)
    except Exception as e:
        logger.warning("[kf] 回调 echostr 解密失败: %s", e)
        return ""


def decrypt_callback_body(
    xml_body: str, msg_signature: str, timestamp: str, nonce: str
) -> str:
    """解密 POST 回调 body (企微事件通知)

    body 结构: <xml><ToUserName>..</ToUserName><Encrypt>..</Encrypt></xml>
    Returns: 解密后的明文 XML (空串 = 验签失败/解密失败)
    """
    token = get_kf_token()
    if not token or not msg_signature:
        return ""
    try:
        root = _safe_xml_fromstring(xml_body)
    except Exception:
        return ""
    encrypt = (root.findtext("Encrypt") or "").strip()
    if not encrypt:
        return ""
    if _sha1_sign(token, timestamp, nonce, encrypt) != msg_signature:
        logger.warning("[kf] 回调 POST 签名不匹配")
        return ""
    try:
        return _aes_decrypt(encrypt)
    except Exception as e:
        logger.warning("[kf] 回调 POST 解密失败: %s", e)
        return ""


def parse_kf_event_notice(plain_xml: str) -> Dict[str, Any]:
    """解析解密后的回调事件通知 XML

    微信客服的事件通知 (不含消息体, 只通知"有新消息"):
      <xml><ToUserName>..</ToUserName><CreateTime>..</CreateTime>
        <MsgType>event</MsgType><Event>kf_msg_or_event</Event>
        <Token>..</Token><OpenKfId>..</OpenKfId></xml>
    Returns:
        {"event", "token", "open_kfid", "to_username", "create_time"}
    """
    try:
        root = _safe_xml_fromstring(plain_xml)
        return {
            "to_username": (root.findtext("ToUserName") or "").strip(),
            "create_time": (root.findtext("CreateTime") or "").strip(),
            "msg_type": (root.findtext("MsgType") or "").strip(),
            "event": (root.findtext("Event") or "").strip(),
            "token": (root.findtext("Token") or "").strip(),
            "open_kfid": (root.findtext("OpenKfId") or "").strip(),
        }
    except Exception as e:
        logger.error("[kf] 解析事件通知失败: %s", e)
        return {}


# ─────────────────────────────────────────────
# sync_msg 拉取 + 消息处理 (v1.4.0 核心链路)
# ─────────────────────────────────────────────

async def sync_and_process(event_token: str, open_kfid: str = "") -> Dict[str, Any]:
    """收到回调事件通知后: 调 sync_msg 增量拉取消息并逐条处理

    - cursor 按 open_kfid 持久化 (kf_state.json), 崩溃/重启不丢消息
    - msgid 去重 (企微可能重推事件通知)
    - has_more 循环拉取 (上限 20 轮防死循环)
    - asyncio.Lock 串行化: 防止回调与定时拉取并发处理同一消息导致重复回复

    Returns: {"ok", "pulled", "processed", "replied", "next_cursor", "errors"}
    """
    lock = _get_sync_lock()
    if lock.locked():
        logger.info("[kf] sync_and_process 已有另一实例在运行, 跳过本次调用")
        return {"ok": True, "pulled": 0, "processed": 0, "replied": 0,
                "next_cursor": "", "errors": ["skipped: concurrent"]}
    async with lock:
        return await _sync_and_process_inner(event_token, open_kfid)


async def _sync_and_process_inner(event_token: str, open_kfid: str = "") -> Dict[str, Any]:
    """sync_and_process 的实际处理逻辑 (由锁保护)"""
    kf_id = open_kfid or get_kf_id()
    if not kf_id:
        return {"ok": False, "errors": ["WECOM_KF_ID 未配置, 无法拉取消息"]}

    state = _load_kf_state()
    cursor = (state.get("cursor") or {}).get(kf_id, "")
    pulled = processed = replied = 0
    errors: List[str] = []

    for _round in range(20):
        body: Dict[str, Any] = {"token": event_token, "limit": 1000, "open_kfid": kf_id}
        if cursor:
            body["cursor"] = cursor

        try:
            token = await _get_kf_access_token()
            url = f"{WECOM_BASE_URL}/cgi-bin/kf/sync_msg?access_token={token}"
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=body)
                data = r.json()
        except Exception as e:
            errors.append(f"sync_msg 请求失败: {e}")
            break

        if data.get("errcode") != 0:
            # 常见: 40001 access_token 失效 / 95011 应用无微信客服权限 /
            # 81001 未配置到「微信客服-可调用接口的应用」
            errors.append(
                f"sync_msg errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
            )
            break

        # msgid 去重 (事件通知可能重推 → 同一批消息重复拉到)
        recent: List[str] = state.setdefault("recent_msgids", [])
        seen = set(recent)
        for item in data.get("msg_list") or []:
            pulled += 1
            msgid = item.get("msgid", "")
            if msgid and msgid in seen:
                continue
            try:
                res = await handle_kf_msg_item(item, kf_id)
                if res.get("handled"):
                    processed += 1
                if res.get("replied"):
                    replied += 1
            except Exception as e:
                errors.append(f"消息 {msgid} 处理失败: {e}")
                logger.exception("[kf] 消息处理异常 msgid=%s", msgid)
            if msgid:
                seen.add(msgid)
                recent.append(msgid)

        if len(recent) > _RECENT_MSGID_LIMIT:
            del recent[: len(recent) - _RECENT_MSGID_LIMIT]

        next_cursor = data.get("next_cursor", "")
        has_more = data.get("has_more", 0)
        if next_cursor:
            cursor = next_cursor
            state.setdefault("cursor", {})[kf_id] = cursor
        state["updated_at"] = now()
        _save_kf_state(state)

        if not has_more:
            break

    if errors:
        logger.warning("[kf] sync_and_process: pulled=%s processed=%s errors=%s",
                       pulled, processed, errors[:3])
    else:
        logger.info("[kf] sync_and_process: pulled=%s processed=%s replied=%s",
                    pulled, processed, replied)
    return {
        "ok": not errors,
        "pulled": pulled,
        "processed": processed,
        "replied": replied,
        "next_cursor": cursor,
        "errors": errors[:5],
    }


async def _download_voice(media_id: str) -> Optional[bytes]:
    """下载企微客服语音文件

    Returns: 语音文件二进制内容，失败返回 None
    """
    try:
        token = await _get_kf_access_token()
        url = f"{WECOM_BASE_URL}/cgi-bin/media/get?access_token={token}&media_id={media_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("audio"):
                return r.content
            logger.warning("[kf] 下载语音失败: status=%s content-type=%s", r.status_code, r.headers.get("content-type"))
            return None
    except Exception as e:
        logger.warning("[kf] 下载语音异常: %s", e)
        return None


async def _speech_to_text(audio_data: bytes) -> str:
    """语音转文字（ASR）

    使用腾讯云 ASR 语音识别 API
    需要配置环境变量：TENCENT_SECRET_ID 和 TENCENT_SECRET_KEY
    """
    # 检查是否配置了腾讯云凭证
    secret_id = os.environ.get("TENCENT_SECRET_ID", "")
    secret_key = os.environ.get("TENCENT_SECRET_KEY", "")

    if not secret_id or not secret_key:
        logger.warning("[kf] ASR 未配置：缺少 TENCENT_SECRET_ID 或 TENCENT_SECRET_KEY")
        return ""

    try:
        # 腾讯云 ASR API
        import hashlib
        import hmac
        import base64
        from datetime import datetime

        # 构建请求
        service = "asr"
        action = "SentenceRecognition"
        version = "2019-06-14"
        region = "ap-guangzhou"
        timestamp = int(datetime.now().timestamp())
        date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")

        # 请求体
        payload = {
            "ProjectId": 0,
            "SubServiceType": 2,
            "EngSerViceType": "16k",
            "SourceType": 1,
            "VoiceFormat": "amr",
            "Data": base64.b64encode(audio_data).decode(),
            "DataLen": len(audio_data),
        }

        # 签名
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = f"content-type:application/json\nhost:{service}.tencentcloudapi.com\n"
        signed_headers = "content-type;host"
        hashed_request_payload = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
        canonical_request = f"{http_request_method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{hashed_request_payload}"

        algorithm = "TC3-HMAC-SHA256"
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode()).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

        secret_date = hmac.new(f"TC3{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, service.encode(), hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, "tc3_request".encode(), hashlib.sha256).digest()
        signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        authorization = f"{algorithm} Credential={secret_id}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

        # 发送请求
        headers = {
            "Content-Type": "application/json",
            "Host": f"{service}.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": version,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": region,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"https://{service}.tencentcloudapi.com", json=payload, headers=headers)
            data = r.json()

        if data.get("Response", {}).get("Error"):
            logger.warning("[kf] ASR 识别失败: %s", data["Response"]["Error"])
            return ""

        result = data.get("Response", {}).get("Result", "")
        logger.info("[kf] ASR 识别成功: %s", result[:100])
        return result

    except Exception as e:
        logger.warning("[kf] ASR 识别异常: %s", e)
        return ""


async def handle_kf_msg_item(item: Dict[str, Any], open_kfid: str = "") -> Dict[str, Any]:
    """处理 sync_msg 拉到的单条消息

    分发规则:
      - msgtype=event (origin=4 系统事件): enter_session → 欢迎语 (welcome_code)
      - origin=3 (微信客户发送): text → 命令/AI 应答; voice → ASR 识别后走文本流程
      - origin=5 (接待人员在企业微信端回复): 忽略
    """
    msgtype = item.get("msgtype", "")
    origin = item.get("origin", 0)

    # 系统事件消息
    if msgtype == "event":
        ev = item.get("event") or {}
        et = ev.get("event_type", "")
        if et == "enter_session":
            ext = ev.get("external_userid", "")
            code = ev.get("welcome_code", "")
            if not (code and ext):
                return {"handled": True, "replied": False,
                        "action": "enter_session_no_code"}
            customer = find_customer_by_external_userid(ext)
            if customer and not customer.get("deleted"):
                # 活跃客人（已绑定房间）
                kf_id = ext
                text = (
                    f"👋 欢迎回来！您绑定的房间是 {customer.get('room_no', '')}。\n\n"
                    f"您可以直接发送文字描述需求，我会智能回复。\n"
                    f"常用服务：\n"
                    f"• 发「报修」— 报修服务\n"
                    f"• 发「送物」— 送物服务\n"
                    f"• 发「人工」— 转人工客服\n"
                    f"• 发「退房」— 申请退房\n\n"
                    f"请直接输入您需要的服务 ⬇️"
                )
            elif customer and customer.get("deleted"):
                # 回访客人（有历史但已退房）
                visits = customer.get("return_visits", 0) + 1
                kf_id = ext
                text = (
                    f"👋 欢迎回来！这是您第 {visits} 次入住。\n"
                    f"您的历史需求记录已自动保留。\n\n"
                    f"请发送您的新房间号进行绑定：\n"
                    f"绑定 <房间号> <姓名>\n\n"
                    f"例如：绑定 1306 张三"
                )
            else:
                kf_id = ext
                text = (
                    "👋 欢迎入住桂山大酒店！我是AI客服助手。\n\n"
                    "您可以直接发送文字描述需求，我会智能回复。\n"
                    "常用服务：\n"
                    "• 发「报修」— 报修服务\n"
                    "• 发「送物」— 送物服务\n"
                    "• 发「人工」— 转人工客服\n"
                    "• 发「退房」— 申请退房\n\n"
                    "请先绑定房间：发送 绑定 <房间号> <姓名>\n"
                    "例如：绑定 1306 张三"
                )
            res = await send_welcome_message(code, text)
            return {"handled": True, "replied": bool(res.get("ok")), "action": "welcome"}
        return {"handled": False, "replied": False, "action": f"event:{et}"}

    ext = item.get("external_userid", "")
    if not ext:
        return {"handled": False, "replied": False, "action": "no_external_userid"}

    # v2.0: 首次接触时尝试将 external_userid 转换为微信 OpenID 并存储
    _customer = find_customer_by_external_userid(ext)
    _openid = (_customer or {}).get("openid", "")
    if not _openid and ext.startswith("wm"):
        _openid = await convert_external_userid_to_openid(ext)
        if _openid and _customer:
            _customer["openid"] = _openid
            save_customers(load_customers())  # persist

    # 接待人员在企业微信端回复 —— 记录为人工接管
    if origin == 5:
        if msgtype == "text":
            content = (item.get("text") or {}).get("content", "")
            log_kf_message(ext, "human", content)
        return {"handled": False, "replied": False, "action": "staff_reply_skip"}

    # 只处理微信客户发的消息
    if origin != 3:
        return {"handled": False, "replied": False, "action": f"origin_{origin}_skip"}

    # 文本消息
    if msgtype == "text":
        content = (item.get("text") or "").get("content", "")
        # v1.4.1: 记录客人消息
        log_kf_message(ext, "user", content)
        # 转人工关键词触发留痕
        if re.search(r"人工|客服|找前台|转接|升级", content):
            log_kf_handoff(ext, "ai", "human", reason="客人要求转人工")

        # v2.3.0: 快速回复机制 — 非命令消息先回复"正在处理"，再异步处理
        # 判断是否是需要快速回复的需求类消息
        _is_command = any([
            content.startswith("绑定") or content.startswith("bind"),
            content in ("解绑", "unbind", "取消绑定"),
            content in ("状态", "status", "查询", "查房"),
            content in ("历史", "history", "记录"),
            content.startswith("报修"),
            content in ("退房", "checkout"),
            content in ("帮助", "help", "?", "？"),
        ])

        if not _is_command:
            # 先快速回复客人
            quick_reply = "⏳ 收到您的需求，正在为您处理，请稍候..."
            log_kf_message(ext, "ai", quick_reply)
            await send_kf_message(ext, "text", quick_reply)

        # 异步处理消息（命令或AI应答）
        result = await handle_kf_text_message(ext, content, open_kfid or get_kf_id())
        reply = result.get("message", "")
        sent = False
        if reply:
            # v1.4.1: 记录 AI 回复
            log_kf_message(ext, "ai", reply)
            r = await send_kf_message(ext, "text", reply)
            sent = bool(r.get("ok"))
        return {"handled": True, "replied": sent, "action": result.get("action", "")}

    # 语音消息 - v1.5.1: 支持语音输入
    if msgtype == "voice":
        voice_data = item.get("voice", {})
        media_id = voice_data.get("media_id", "")
        if not media_id:
            return {"handled": True, "replied": False, "action": "voice_no_media_id"}

        # 下载语音文件
        audio_data = await _download_voice(media_id)
        if not audio_data:
            await send_kf_message(ext, "text", "语音识别失败，请重新发送或改用文字输入。")
            return {"handled": True, "replied": True, "action": "voice_download_failed"}

        # 语音转文字
        content = await _speech_to_text(audio_data)
        if not content:
            await send_kf_message(ext, "text", "语音识别失败，请重新发送或改用文字输入。")
            return {"handled": True, "replied": True, "action": "voice_asr_failed"}

        # 记录语音识别结果
        log_kf_message(ext, "user", f"[语音] {content}")

        # 走文本消息处理流程
        result = await handle_kf_text_message(ext, content, open_kfid or get_kf_id())
        reply = result.get("message", "")
        sent = False
        if reply:
            # 添加语音识别提示
            reply = f"🎤 语音识别：{content}\n\n{reply}"
            log_kf_message(ext, "ai", reply)
            r = await send_kf_message(ext, "text", reply)
            sent = bool(r.get("ok"))
        return {"handled": True, "replied": sent, "action": f"voice_{result.get('action', '')}"}

    # 图片/视频/文件等暂不支持
    log_kf_message(ext, "user", f"[unsupported:{msgtype}]")
    await send_kf_message(
        ext, "text",
        "👋 图片/视频等消息暂不支持，请直接文字或语音描述您的需求。",
    )
    return {"handled": True, "replied": True, "action": f"unsupported_{msgtype}"}


# ─────────────────────────────────────────────
# AI 应答 (v1.4.0: 接 QwenPaw 智能体)
# ─────────────────────────────────────────────

def _search_knowledge_base(query: str, top_k: int = 3) -> List[str]:
    """简单关键词匹配知识库（v1.4.1 兜底实现）

    未来可换成向量检索；当前用空格分词后的命中数排序。
    """
    kb_path = data_layer.DATA_DIR / "knowledge_base.md"
    if not kb_path.exists():
        return []
    try:
        text = kb_path.read_text(encoding="utf-8")
    except Exception:
        return []

    # 按 ## 分段
    sections = re.split(r"\n(?=##\s+)", text)
    query_words = [w for w in re.split(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", query.lower()) if w]
    if not query_words:
        return []

    scored = []
    for sec in sections:
        if not sec.strip():
            continue
        sec_lower = sec.lower()
        score = sum(1 for w in query_words if w in sec_lower)
        if score > 0:
            scored.append((score, sec.strip()))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]


async def ai_reply_for_guest(external_userid: str, content: str) -> str:
    """调 QwenPaw 智能体做 AI 应答, 失败返回空串 (调用方走兜底文案)

    - 每位客人独立会话 (user_id = kf-{external_userid}, 30min TTL 由
      chat_session_manager 维护)
    - 带上客人绑定上下文 (房间号/姓名), 让 AI 知道在跟谁说话
    - v1.4.1: 追加知识库上下文，FAQ/设施/政策有据可依
    - v1.5.1: 严格模式 — 只回答知识库范围内的问题，超出范围转人工
    - v2.3.0: 知识库快速回复优化 — 高置信度直接回复，不调Agent
    - 回复超 2000 字节截断 (企微客服文本上限 2048 字节)
    """
    agent_id = get_kf_ai_agent()
    customer = find_active_customer_by_external_userid(external_userid)
    context = ""
    if customer:
        room_no = customer.get("room_no", "")
        if not room_no:
            # 客人已退房（room_no 被清空），提示重新绑定
            return {
                "ok": True,
                "action": "rebind_required",
                "message": (
                    "⚠️ 您之前的房间已退房，系统记录已清理。\n"
                    "如需服务，请先重新绑定房间：\n"
                    "发送「绑定 <房间号> <姓名>」\n\n"
                    "例如：绑定 1310 刘"
                ),
            }
        context = (
            f"【已识别客人】房间 {room_no}，"
            f"姓名 {customer.get('guest_name', '')}。"
            f"不要要求客人重新绑定房间，直接处理需求。\n"
        )
    else:
        context = "【未绑定客人】如果客人在提需求，请先让客人绑定房间。如果是问问题，可以直接回答。\n"

    # v2.3.0: 知识库快速回复优化 — 高置信度直接回复，不调Agent
    kb_hits = _search_knowledge_base(content, top_k=5)
    if kb_hits:
        # 用知识库直接回答 (关键词命中 >= 2 个词才算有效匹配)
        query_words = [w for w in re.split(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", content.lower()) if w]
        top_score = sum(1 for w in query_words if w in kb_hits[0].lower()) if kb_hits else 0
        
        # 高置信度 (>=3 个关键词命中) 直接回复，不调Agent
        if top_score >= 3:
            return {
                "ok": True,
                "action": "kb_direct",
                "message": kb_hits[0].strip()[:800],
            }
        
        # 中等置信度 (>=2 个关键词命中) 也直接回复
        if top_score >= 2:
            return {
                "ok": True,
                "action": "kb_direct",
                "message": kb_hits[0].strip()[:800],
            }
        
        # 命中但置信度低, 带上下文调 agent
        kb_context = "【酒店知识库参考】\n" + "\n---\n".join(kb_hits) + "\n---\n请结合知识库回答, 知识库没有的再用你的知识补充。\n\n"
    else:
        kb_context = ""

    # v1.4.1: 注入客人档案上下文
    profile_context = ""
    if customer:
        profile_context = guest_profile.get_guest_profile_context(
            external_userid=external_userid,
            room_no=customer.get("room_no", ""),
            guest_name=customer.get("guest_name", ""),
        )
        if profile_context:
            profile_context += "请结合客人历史需求给出更贴心的回答。\n\n"

    try:
        # 直接 HTTP 调 QwenPaw 主进程 (避免 from routes.ai_assistants 的相对导入问题)
        import httpx
        port = os.environ.get("QWENPAW_PORT", "8889")
        base_url = f"http://127.0.0.1:{port}"

        # session 路由: 每个客人独立会话 (409 时 force_new 重试)
        sm = chat_session_manager.get_session_manager()
        session_id = "default"
        if sm is not None:
            session_info = sm.get_or_create(f"kf-{external_userid}", agent_id)
            session_id = session_info.get("session_id", "default")

        message = f"[微信客服] {context}{profile_context}{kb_context}{content}"
        req_body = {
            "input": [{"content": [{"type": "text", "text": message}]}],
            "user_id": f"kf-{external_userid}",
            "session_id": session_id,
        }
        text_parts = []

        async def _call_agent(sid: str):
            """调用 agent，返回 (status_code, text_parts)"""
            req_body["session_id"] = sid
            parts = []
            async with httpx.AsyncClient(timeout=65) as c:
                async with c.stream(
                    "POST",
                    f"{base_url}/api/agents/{agent_id}/console/chat",
                    json=req_body,
                ) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, []
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        try:
                            obj = json.loads(line[6:])
                        except Exception:
                            continue
                        if obj.get("type") == "text" and "text" in obj:
                            parts.append(obj["text"])
            return 200, parts

        status, text_parts = await _call_agent(session_id)

        # 409 = 上一个任务卡住，force_new 新会话重试
        if status == 409:
            if sm is not None:
                session_info = sm.get_or_create(f"kf-{external_userid}", agent_id, force_new=True)
                session_id = session_info.get("session_id", session_id)
            status, text_parts = await _call_agent(session_id)

        if status != 200:
            logger.warning("[kf] AI agent HTTP %d after retry", status)
            return ""

        reply = (text_parts[-1] if text_parts else "").strip()
        if reply and reply != "(无回复)":
            if len(reply.encode("utf-8")) > 2000:
                reply = reply[:650] + "\n…（回复过长已截断）"
            return reply
        return ""
    except Exception as e:
        logger.warning("[kf] AI 应答异常 (agent=%s): %s", agent_id, e)
    return ""


# ─────────────────────────────────────────────
# v2.2.2: 微信客服需求草稿闭环
# ─────────────────────────────────────────────

async def handle_draft_flow(external_userid: str, content: str) -> Optional[Dict[str, Any]]:
    """处理客人新建需求草稿流程

    返回非 None 表示已接管消息；返回 None 让上层继续走命令/AI 应答。
    """
    # 延迟 import 避免循环依赖
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import draft_store

    content = content.strip()

    # 菜单点击处理
    if content in ("确认无误", "确认", "提交"):
        draft = draft_store.get_draft(external_userid)
        if not draft or not draft_store.is_complete(draft):
            return {"ok": True, "action": "draft_incomplete", "message": "信息还不完整，无法提交。"}
        # 提交生成 Request + Tickets
        try:
            from .routes.dispatch import _create_request_from_payload
            payload = {
                "description": draft.get("description", ""),
                "contact_name": draft.get("contact_name", ""),
                "contact_phone": draft.get("contact_phone", ""),
                "room_no": draft.get("room_no", ""),
                "guest_userid": external_userid,
                "source": "kf_draft",
                "intents": [{
                    "work_type": _guess_work_type(draft.get("description", "")),
                    "description": draft.get("description", ""),
                    "room_no": draft.get("room_no", ""),
                    "target_dept": _guess_dept(draft.get("description", "")),
                }],
            }
            result = await _create_request_from_payload(payload, created_by=f"guest:{external_userid}")
            draft_store.clear_draft(external_userid)
            req = result.get("request", {})
            tickets = result.get("tickets", [])
            return {
                "ok": True,
                "action": "draft_submitted",
                "message": (
                    f"✅ 需求已提交\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"需求编号：{req.get('request_id', '')}\n"
                    f"工单编号：{', '.join(t.get('wo_id', '') for t in tickets)}\n"
                    f"我们会尽快安排处理，进度将通过此客服号通知您。"
                ),
            }
        except Exception as e:
            logger.warning("[kf] 提交草稿失败: %s", e)
            return {"ok": True, "action": "draft_submit_error", "message": f"提交失败，请重试或联系前台。错误：{e}"}

    if content in ("我要修改", "修改"):
        draft = draft_store.get_draft(external_userid)
        if not draft:
            return {"ok": True, "action": "draft_no_draft", "message": "当前没有待确认的需求。"}
        return {
            "ok": True,
            "action": "draft_modify",
            "message": (
                f"当前草稿：\n"
                f"需求：{draft.get('description', '')}\n"
                f"联系人：{draft.get('contact_name', '')}\n"
                f"电话：{draft.get('contact_phone', '')}\n"
                f"房号：{draft.get('room_no', '')}\n\n"
                f"请直接发送要修改的项目，例如：\n"
                f"房号 1301\n电话 13800138000"
            ),
        }

    if content in ("查看详情", "详情"):
        draft = draft_store.get_draft(external_userid)
        if not draft:
            return {"ok": True, "action": "draft_no_draft", "message": "当前没有待确认的需求。"}
        return {
            "ok": True,
            "action": "draft_detail",
            "message": (
                f"📋 需求草稿\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"需求：{draft.get('description', '')}\n"
                f"联系人：{draft.get('contact_name', '')}\n"
                f"电话：{draft.get('contact_phone', '')}\n"
                f"房号：{draft.get('room_no', '')}\n\n"
                f"回复「确认无误」提交，回复「我要修改」修改。"
            ),
        }

    # 更新草稿字段（简单规则）
    draft = draft_store.get_draft(external_userid)
    updates: Dict[str, str] = {}

    # 房号：4 位以内数字（如 1301）
    import re
    room_match = re.search(r"\b(\d{3,4})\b", content)
    phone_match = re.search(r"1[3-9]\d{9}", content.replace(" ", "").replace("-", ""))

    if not draft:
        # 新草稿，第一条消息作为需求描述
        updates["description"] = content
    else:
        # 根据当前缺失字段分配内容
        missing = draft_store.missing_fields(draft)
        if missing:
            # 房号最容易识别
            if "room_no" in missing and room_match:
                updates["room_no"] = room_match.group(1)
                missing.remove("room_no")
            # 电话
            if "contact_phone" in missing and phone_match:
                updates["contact_phone"] = phone_match.group(0)
                missing.remove("contact_phone")
            # 联系人：长度较短、无数字、非房号/电话
            if "contact_name" in missing and not room_match and not phone_match and len(content) <= 8:
                updates["contact_name"] = content
                missing.remove("contact_name")
            # 需求描述：未识别为其他字段时
            if "description" in missing and not updates:
                updates["description"] = content

    if updates:
        draft = draft_store.update_draft(external_userid, updates)
    elif not draft:
        draft = draft_store.update_draft(external_userid, {"description": content})

    # 补齐后发送确认菜单
    if draft_store.is_complete(draft):
        return {
            "ok": True,
            "action": "draft_complete",
            "message": (
                f"📋 请确认您的需求\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"需求：{draft.get('description', '')}\n"
                f"联系人：{draft.get('contact_name', '')}\n"
                f"电话：{draft.get('contact_phone', '')}\n"
                f"房号：{draft.get('room_no', '')}\n\n"
                f"回复「确认无误」提交；\n"
                f"回复「我要修改」修改；\n"
                f"回复「查看详情」再看一遍。"
            ),
        }

    # 还没齐，追问缺失项
    return {
        "ok": True,
        "action": "draft_ask",
        "message": draft_store.ask_for_missing(draft),
    }


def _guess_work_type(description: str) -> str:
    """简单关键词推断工单类型"""
    d = description.lower()
    if any(k in d for k in ["空调", "灯", "水管", "电", "维修", "坏", "故障", "马桶", "漏水", "门锁", "电视", "wifi", "网络", "热水", "暖气"]):
        return "工程维修"
    if any(k in d for k in ["毛巾", "牙刷", "洗漱", "打扫", "清洁", "垃圾", "床单", "被罩", "枕头", "被子", "拖鞋"]):
        return "客房服务"
    if any(k in d for k in ["送水", "矿泉水", "饮料", "餐", "吃", "外卖", "可乐", "果汁", "泡面", "零食"]):
        return "送餐送物"
    return "其他"


def _guess_dept(description: str) -> str:
    """简单关键词推断目标部门"""
    wt = _guess_work_type(description)
    return {
        "工程维修": "engineering",
        "客房服务": "housekeeping",
        "送餐送物": "housekeeping",
        "其他": "frontdesk",
    }.get(wt, "frontdesk")


# v1.4.1: 自然语言槽位抽取
_ROOM_PATTERN = re.compile(r"(?:房间?|住在?|住|room\s*)(\d{3,4})", re.IGNORECASE)
_BARE_ROOM_PATTERN = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")  # 3-4 位数字，前后无其他数字
_URGENCY_KEYWORDS = {
    "urgent": ["紧急", "急", "马上", "立刻", "赶紧", "赶紧来", "快"],
    "high": ["尽快", "比较急", "重要"],
}
_TYPE_KEYWORDS = {
    "送物": ["送", "要", "拿", "给", "需要", "来", "加", "多要", "再来"],
    "维修": ["坏了", "坏了", "不", "堵", "漏", "修", "故障", "有问题", "不能用", "不工作", "不亮", "不热", "不冷", "噪音"],
    "清洁": ["打扫", "清洁", "收拾", "清理", "换", "脏", "垃圾"],
    "送餐送物": ["吃的", "喝的", "餐", "饭", "水", "可乐", "饮料", "泡面", "零食", "外卖"],
}
_ITEM_KEYWORDS = [
    "拖鞋", "牙刷", "牙膏", "梳子", "剃须刀", "矿泉水", "毛巾", "枕头", "被子",
    "可乐", "果汁", "泡面", "零食", "外卖", "餐", "饭", "水",
    "洗衣液", "洗发水", "沐浴露", "卫生纸", "纸巾",
]


def _extract_slots(text: str, bound_room: str = "") -> Dict[str, Any]:
    """从自然语言中抽取槽位

    返回: {room_no, work_type, description, urgency, confidence}
    confidence: 0-1，越高越确定
    """
    result = {
        "room_no": bound_room,
        "work_type": "",
        "description": text,
        "urgency": "normal",
        "confidence": 0.0,
    }
    if not text:
        return result

    # 1. 抽取房间号
    m = _ROOM_PATTERN.search(text)
    if m:
        result["room_no"] = m.group(1)
        result["confidence"] += 0.3
    elif not bound_room:
        m = _BARE_ROOM_PATTERN.search(text)
        if m:
            candidate = m.group(1)
            # 排除明显不是房间号的数字（如1000以上的年份/价格）
            if 100 <= int(candidate) <= 1999:
                result["room_no"] = candidate
                result["confidence"] += 0.2

    # 2. 抽取紧急度
    for level, keywords in _URGENCY_KEYWORDS.items():
        if any(k in text for k in keywords):
            result["urgency"] = level
            result["confidence"] += 0.1
            break

    # 3. 推断需求类型
    type_scores = {}
    for wt, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for k in keywords if k in text)
        if score > 0:
            type_scores[wt] = score
    if type_scores:
        result["work_type"] = max(type_scores, key=type_scores.get)
        result["confidence"] += 0.3
    else:
        result["work_type"] = _guess_work_type(text)
        result["confidence"] += 0.1

    # 4. 抽取物品关键词（用于描述优化）
    items = [item for item in _ITEM_KEYWORDS if item in text]
    if items:
        result["confidence"] += 0.2

    # 5. 清理描述（去掉房间号前缀）
    desc = text
    if result["room_no"] and not bound_room:
        desc = _ROOM_PATTERN.sub("", desc)
        desc = re.sub(r"(?<!\d)" + re.escape(result["room_no"]) + r"(?!\d)", "", desc)
    desc = re.sub(r"^(我|我们|帮我|麻烦|请|能不能|可以)", "", desc).strip()
    if desc:
        result["description"] = desc

    result["confidence"] = min(result["confidence"], 1.0)
    return result


# ─────────────────────────────────────────────
# 客人文本消息处理 (命令模式 + AI 应答兜底)
# ─────────────────────────────────────────────

async def handle_kf_text_message(
    external_userid: str,
    content: str,
    open_kfid: str = "",
) -> Dict[str, Any]:
    """处理客人发来的文本消息

    支持的命令格式：
    - 绑定 <房间号> <姓名>  — 绑定房间
    - 解绑                   — 解绑房间
    - 状态                   — 查询当前房间状态
    - 报修 <描述>            — 快速报修（同步通知内部员工）
    - 其他文本               — QwenPaw 智能体 AI 应答（失败回帮助文案）
    """
    content = content.strip()

    # 绑定命令
    if content.startswith("绑定") or content.startswith("bind"):
        # 智能解析：去掉"绑定/bind/房间/号/姓名/名字/叫/是"等修饰词
        raw = content.replace("绑定", "").replace("bind", "").strip()
        # 去掉常见修饰词
        for word in ("房间", "号", "姓名", "名字", "叫", "是"):
            raw = raw.replace(word, " ")
        tokens = raw.split()
        # 提取房间号（第一个连续数字序列）
        room_no = ""
        guest_name = ""
        import re as _re
        for i, tok in enumerate(tokens):
            m = _re.search(r"\d+", tok)
            if m and not room_no:
                room_no = m.group()
                # 剩余部分可能在同一 token 里（如"1306张三"）
                remaining = tok[m.end():]
                if remaining:
                    guest_name = remaining
                # 后续 tokens 是姓名
                if not guest_name and i + 1 < len(tokens):
                    guest_name = "".join(tokens[i + 1:])
                break
        if not room_no and tokens:
            room_no = tokens[0]
            if len(tokens) > 1:
                guest_name = "".join(tokens[1:])
        if room_no and guest_name:
            # 校验房间是否存在
            from . import data_layer
            all_rooms = data_layer.load_table("rooms")
            room_exists = any(
                str(r.get("room_no", "")).strip() == room_no
                for r in all_rooms
            )
            if not room_exists:
                return {
                    "ok": True,
                    "action": "bind_room_not_found",
                    "message": f"⚠️ 房间 {room_no} 不存在，请检查房间号是否正确。\n\n"
                              f"例如：绑定 1306 刘",
                }
            customer = bind_customer(external_userid, room_no, guest_name)
            # 回访客人提示
            visits = customer.get("return_visits", 0)
            if visits > 0:
                return_msg = (
                    f"✅ 欢迎回来！已重新绑定房间 {room_no}（{guest_name}）\n"
                    f"这是您第 {visits + 1} 次入住，历史工单已自动保留。\n\n"
                    f"发送「历史」可查看历史需求记录。"
                )
            else:
                return_msg = (
                    f"✅ 已绑定房间 {room_no}（{guest_name}）\n\n"
                    f"后续报修和需求通知将通过此客服号推送给您。\n"
                    f"发送「状态」可查询房间状态，发送「解绑」可解除绑定。"
                )
            return {
                "ok": True,
                "action": "bind",
                "message": return_msg,
                "customer": customer,
            }
        else:
            return {
                "ok": True,
                "action": "bind_help",
                "message": "📝 绑定格式：绑定 <房间号> <姓名>\n\n"
                          f"例如：绑定 104 张四\n\n"
                          f"请提供您的房间号和入住姓名。",
            }

    # 解绑命令
    if content in ("解绑", "unbind", "取消绑定"):
        if unbind_customer(external_userid):
            return {
                "ok": True,
                "action": "unbind",
                "message": "✅ 已解除绑定，后续通知将不再推送。",
            }
        else:
            return {
                "ok": True,
                "action": "unbind_not_found",
                "message": "⚠️ 未找到绑定记录。",
            }

    # 状态查询
    if content in ("状态", "status", "查询", "查房"):
        customer = find_active_customer_by_external_userid(external_userid)
        if not customer:
            return {
                "ok": True,
                "action": "query_no_bind",
                "message": "⚠️ 您还未绑定房间，请先发送：绑定 <房间号> <姓名>",
            }
        room_no = customer.get("room_no", "")
        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        if not room:
            return {
                "ok": True,
                "action": "query_room_not_found",
                "message": f"⚠️ 房间 {room_no} 不存在，请检查绑定信息。",
            }
        wos = data_layer.load_table("work_orders")
        active_wos = [
            w for w in wos
            if w.get("room_no") == room_no and w.get("status") not in ("done", "rejected")
        ]
        msg = (
            f"🏨 房间 {room_no} 状态\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"状态：{room.get('status', '未知')}\n"
            f"房型：{room.get('room_type', '未知')}\n"
            f"楼层：{room.get('floor', '?')}F\n"
        )
        if active_wos:
            msg += f"\n📋 进行中的工单（{len(active_wos)} 条）：\n"
            for w in active_wos:
                status_desc = {
                    "pending_confirm": "等待处理",
                    "pending": "等待派单",
                    "assigned": "已派单",
                    "in_progress": "处理中",
                }.get(w.get("status", ""), w.get("status", ""))
                msg += f"  • [{w.get('work_type', '')}] {w.get('description', '')[:20]} — {status_desc}\n"
        else:
            msg += "\n✅ 暂无进行中的工单"
        return {"ok": True, "action": "query", "message": msg}

    # 历史工单查询
    if content in ("历史", "历史工单", "历史记录", "history"):
        customer = find_customer_by_external_userid(external_userid)
        if not customer:
            return {
                "ok": True,
                "action": "history_no_record",
                "message": "📋 暂无历史记录。\n\n发送「绑定 <房间号> <姓名>」开始使用服务。",
            }
        # 查找该客人的所有工单（通过 openid 或 external_userid）
        wos = data_layer.load_table("work_orders")
        openid = customer.get("openid", "")
        guest_name = customer.get("guest_name", "")
        my_wos = []
        for w in wos:
            # 匹配条件：同一 openid / 同一 external_userid 的客人
            w_gid = w.get("guest_id", "")
            if openid and w_gid == openid:
                my_wos.append(w)
            elif w.get("source_detail", "") == f"guest:{external_userid}":
                my_wos.append(w)
            elif w.get("reporter", "") == f"客人:{guest_name}" and guest_name:
                my_wos.append(w)
        if not my_wos:
            return {
                "ok": True,
                "action": "history_empty",
                "message": "📋 暂无历史需求记录。",
            }
        my_wos.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        msg = f"📋 历史需求记录（最近 {min(len(my_wos), 10)} 条）\n━━━━━━━━━━━━━━━━\n"
        for w in my_wos[:10]:
            status_desc = {
                "pending_confirm": "⏳ 待确认",
                "pending": "📋 待派单",
                "assigned": "👨‍🔧 已派单",
                "accepted": "✅ 已接单",
                "in_progress": "🔧 处理中",
                "completed_pending_guest": "✔️ 待确认完成",
                "done": "✅ 已完成",
                "rejected": "❌ 已取消",
            }.get(w.get("status", ""), w.get("status", ""))
            room = w.get("room_no", "?")
            msg += (
                f"\n• {w.get('work_type', '')} | 房间 {room}\n"
                f"  {w.get('description', '')[:30]}\n"
                f"  {status_desc} | {w.get('created_at', '')[:10]}\n"
            )
            rating = w.get("guest_rating")
            if rating:
                msg += f"  {'⭐' * int(rating)}\n"
        # 会话记录数
        sessions = get_kf_session_history(external_userid)
        if sessions:
            msg += f"\n💬 客服会话记录：{len(sessions)} 条消息"
        return {"ok": True, "action": "history", "message": msg}

    # 快速报修
    if content.startswith("报修") or content.startswith("维修"):
        customer = find_active_customer_by_external_userid(external_userid)
        if not customer:
            return {
                "ok": True,
                "action": "repair_no_bind",
                "message": "⚠️ 请先绑定房间：绑定 <房间号> <姓名>",
            }
        description = content.replace("报修", "").replace("维修", "").strip()
        if not description:
            return {
                "ok": True,
                "action": "repair_help",
                "message": "📝 报修格式：报修 <故障描述>\n\n例如：报修 空调不制冷",
            }
        # v1.6.1 统一收口: 委托 svc (智能表格同步 + 员工通知 + 客人回执 + 自动派单; guest_id 并入)
        from .routes.work_order_svc import svc_create_work_order
        _res = await svc_create_work_order(
            room_no=customer["room_no"],
            work_type="维修",
            description=description,
            priority="normal",
            reporter=f"客人:{customer.get('guest_name', '')}",
            target_dept="engineering",
            data_source="guest_kf",
            operator=f"guest:{external_userid}",
            extra=({"guest_id": customer["openid"]} if customer.get("openid") else None),
            auto_dispatch=False,
        )
        wo = _res["work_order"]
        # v1.4.1: 更新客人档案
        guest_profile.update_guest_profile(
            wo,
            external_userid=external_userid,
            room_no=customer["room_no"],
            guest_name=customer.get("guest_name", ""),
        )
        # 通知统一由 svc_create_work_order 发出 (员工企微卡片 + 客人回执), 不再走 notify_staff
        return {
            "ok": True,
            "action": "repair",
            "message": (
                f"✅ 报修已提交\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"房间：{customer['room_no']}\n"
                f"故障：{description}\n"
                f"工单号：{wo.get('wo_id', '')}\n\n"
                f"我们会尽快安排维修，维修进度将通过此客服号通知您。"
            ),
            "work_order": wo,
        }

    # 所有非命令消息 → AI 智能体应答 (有 guest_request_service 等工具，能处理报修/送物/打扫等)
    ai_reply = await ai_reply_for_guest(external_userid, content)
    if ai_reply:
        return {"ok": True, "action": "ai_reply", "message": ai_reply}

    # AI 不可用时的兜底文案
    return {
        "ok": True,
        "action": "message",
        "message": (
            "👋 您好！我是酒店客服助手。\n\n"
            "您可以发送以下指令：\n"
            "• 绑定 <房间号> <姓名> — 绑定房间\n"
            "• 状态 — 查询房间状态\n"
            "• 报修 <描述> — 快速报修\n"
            "• 历史 — 查看历史需求记录\n"
            "• 解绑 — 解除绑定\n\n"
            "或直接描述您的需求，我们会尽快回复。"
        ),
    }


# ─────────────────────────────────────────────
# 企微智能表格推送（已完成工单归档）
# ─────────────────────────────────────────────

async def push_to_done_table(wo: Dict[str, Any]) -> Dict[str, Any]:
    """推送已完成工单到企微智能表格

    TODO: 接入企微智能表格 work_orders_done

    Args:
        wo: 工单数据

    Returns:
        {"ok": True/False, "message": "..."}
    """
    logger.info("[kf] 推送工单 %s 到企微表格（待实现）", wo.get("wo_id"))
    return {
        "ok": False,
        "message": "企微智能表格推送功能待配置，请先创建 work_orders_done 表并提供 doc_id",
    }


# ───────────────────────────────
# v1.4.1: 客服会话质检 / 人工接管留痕
# ───────────────────────────────
def _kf_session_key(external_userid: str) -> str:
    return external_userid or "anonymous"


def log_kf_message(external_userid: str, role: str, content: str, extra: Dict[str, Any] = None) -> None:
    """记录一条客服会话消息

    role: user / ai / human
    """
    try:
        entry = {
            "ts": now(),
            "external_userid": external_userid,
            "role": role,
            "content": content[:_KF_SESSION_MAX_LEN],
        }
        if extra:
            entry["extra"] = extra
        with open(_KF_SESSION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[kf] 记录会话消息失败: %s", exc)


def log_kf_handoff(external_userid: str, from_role: str, to_role: str, reason: str = "") -> None:
    """记录一次 AI ↔ 人工接管事件"""
    log_kf_message(external_userid, "handoff", f"{from_role} → {to_role}", {
        "from": from_role,
        "to": to_role,
        "reason": reason,
    })


def get_kf_session_history(external_userid: str, limit: int = 50) -> List[Dict[str, Any]]:
    """读取单个客人的最近会话记录"""
    if not _KF_SESSION_LOG.exists():
        return []
    try:
        lines = _KF_SESSION_LOG.read_text(encoding="utf-8").strip().splitlines()
        records = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("external_userid") == external_userid:
                records.append(r)
                if len(records) >= limit:
                    break
        records.reverse()
        return records
    except Exception as exc:
        logger.warning("[kf] 读取会话历史失败: %s", exc)
        return []


def list_kf_sessions(limit: int = 100) -> List[Dict[str, Any]]:
    """列出最近有会话的客人列表（去重）"""
    if not _KF_SESSION_LOG.exists():
        return []
    try:
        lines = _KF_SESSION_LOG.read_text(encoding="utf-8").strip().splitlines()
        seen = set()
        sessions = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            uid = r.get("external_userid")
            if uid and uid not in seen:
                seen.add(uid)
                sessions.append({
                    "external_userid": uid,
                    "last_ts": r.get("ts"),
                    "last_role": r.get("role"),
                    "last_content": r.get("content"),
                })
                if len(sessions) >= limit:
                    break
        return sessions
    except Exception as exc:
        logger.warning("[kf] 读取会话列表失败: %s", exc)
        return []


# ───────────────────────────────
# 兜底：定时主动拉取客服消息
# ───────────────────────────────
_kf_sync_started = False


def start_kf_sync_loop(interval_s: int = 30) -> None:
    """启动后台定时拉取客服消息循环

    企微回调可能因网络/配置/超时等原因不实时到达，用主动拉取兜底，
    确保客人消息即使没回调也能在 30 秒内被处理。
    """
    global _kf_sync_started
    if _kf_sync_started:
        return
    _kf_sync_started = True

    async def _loop():
        # 首次等 5 秒让应用完全启动
        await asyncio.sleep(5)
        while True:
            try:
                await asyncio.sleep(interval_s)
                kf_id = get_kf_id()
                if not kf_id:
                    continue
                result = await sync_and_process("", kf_id)
                if result.get("pulled", 0) > 0:
                    logger.info("[kf] 定时拉取结果: pulled=%s processed=%s replied=%s",
                                result.get("pulled"), result.get("processed"), result.get("replied"))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[kf] 定时拉取异常: %s", exc)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_loop())
            logger.info("[kf] 定时拉取循环已挂到 event loop")
            return
        raise RuntimeError("event loop 未运行")
    except RuntimeError:
        import threading

        def _runner():
            try:
                asyncio.run(_loop())
            except Exception as exc:
                logger.error("[kf] 定时拉取线程退出: %s", exc)

        threading.Thread(target=_runner, daemon=True, name="kf-sync").start()
        logger.info("[kf] 定时拉取循环已在后台线程启动")
