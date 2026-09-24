# -*- coding: utf-8 -*-
"""DomHotel Suite — 酒店业务操作智能体工具 (v1.6.0)

核心理念: 对话即工作台。客人与员工在对话 (微信客服 / 部门群机器人 /
AI 助手) 中由 agent 完成所有业务操作, 看板与 H5 只是辅助选项。

工具分两组:
  客人侧 (guest_*, 凭 external_userid 查绑定房间, 只能动自己的房):
    guest_query_my_room / guest_request_service / guest_query_my_orders
    / guest_request_checkout / guest_room_complaint
  员工侧 (staff_*, 按部门 agent 分工, operator 记审计):
    staff_room_query / staff_room_checkin / staff_room_checkout
    / staff_room_status_set (带工单联动) / staff_room_change
    / staff_work_order_create / assign / complete / list
    / staff_today_overview

权限模型: 工具自身强制身份与房间归属校验, 不依赖调用方自觉。
房态联动: 维修中/待打扫 自动建单, 恢复空房 自动关单 (与看板行为一致)。
所有写操作幂等安全: 重复入住/重复建单/重复关单均有护栏。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("domhotel-suite.hotel_ops_tools")



# ─────────────────────────── 角色权限控制 (RBAC) ───────────────────────────

_ROLE_TOOLS = {
    "hotel-ai-guest-service": {
        "guest_query_my_room", "guest_request_service", "guest_query_my_orders",
        "guest_request_checkout", "guest_room_complaint", "guest_bind_room",  # v2.4-roomfix
        "staff_room_query", "staff_today_overview",
        "staff_schedule_query",
    },
    "hotel-ai-frontdesk": {
        "guest_query_my_room", "guest_request_service", "guest_query_my_orders",
        "guest_request_checkout", "guest_room_complaint", "guest_bind_room",  # v2.4-roomfix
        "staff_room_query", "staff_room_checkin", "staff_room_checkout",
        "staff_room_status_set", "staff_room_change",
        "staff_work_order_create", "staff_work_order_assign",
        "staff_work_order_complete", "staff_work_order_list",
        "staff_today_overview",
        "staff_request_create", "staff_request_list",
        "staff_ticket_list", "staff_ticket_assign", "staff_ticket_transition",
        "staff_pending_list", "staff_pending_confirm", "staff_pending_reject",
        "staff_schedule_query", "staff_schedule_set",
    },
    "hotel-ai-engineering": {
        "staff_room_query", "staff_today_overview",
        "staff_work_order_list", "staff_work_order_complete",
        "staff_work_order_create", "staff_work_order_assign",
        "staff_room_status_set",
        "staff_ticket_list", "staff_ticket_transition",
        "staff_pending_list", "staff_pending_confirm",
        "staff_schedule_query", "staff_schedule_set",
    },
    "hotel-ai-housekeeping": {
        "staff_room_query", "staff_today_overview",
        "staff_work_order_list", "staff_work_order_complete",
        "staff_work_order_create", "staff_work_order_assign",
        "staff_room_status_set",
        "staff_ticket_list", "staff_ticket_transition",
        "staff_pending_list", "staff_pending_confirm",
        "staff_schedule_query", "staff_schedule_set",
    },
}

_UNRESTRICTED_AGENTS = {"hotel-wecom-assistant", "default", "QwenPaw_QA_Agent_0.2"}


def _check_tool_access(tool_name: str):
    """RBAC: 检查当前 agent 是否有权限调用此工具。"""
    try:
        from qwenpaw.app.agent_context import get_current_agent_id
        agent_id = get_current_agent_id() or ""
    except Exception:
        return None
    if not agent_id or agent_id in _UNRESTRICTED_AGENTS:
        return None
    allowed = _ROLE_TOOLS.get(agent_id)
    if allowed is None:
        return None
    if tool_name not in allowed:
        return {
            "ok": False,
            "error": f"权限不足: agent ({agent_id}) 无权使用 {tool_name}，请通过 hotel-ai-frontdesk 协调。",
        }
    return None


# 服务类型 → (工单类型, 目标部门, 默认优先级)
_SERVICE_MAP = {
    # v2.4-intake: 客人请求统一 target_dept=frontdesk, 由前台收单再转派对应部门; work_type 仍保留供前台判断
    "维修": ("维修", "frontdesk", "high"),
    "送物": ("送物", "frontdesk", "normal"),
    "打扫": ("清洁", "frontdesk", "normal"),
    "补货": ("补货", "frontdesk", "normal"),
    "加被": ("送物", "frontdesk", "normal"),
    "其他": ("其他", "frontdesk", "normal"),
}

_VALID_ROOM_STATUS = ("空房", "在住", "待打扫", "维修中")


# ─────────────────────────────── 内部助手 ───────────────────────────────

def _ensure_backend():
    """确保 __domhotel_suite_backend__ 包在 sys.modules 中 (工具执行时可能丢失)"""
    import sys
    pkg = "__domhotel_suite_backend__"
    if pkg not in sys.modules:
        from pathlib import Path
        import importlib.util
        this_dir = Path(__file__).resolve().parent.parent  # backend/
        spec = importlib.util.spec_from_file_location(
            pkg, str(this_dir / "__init__.py"),
            submodule_search_locations=[str(this_dir)],
        )
        if spec:
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = pkg
            mod.__path__ = [str(this_dir)]
            mod.__file__ = str(this_dir / "__init__.py")
            sys.modules[pkg] = mod
            # 同时注册 routes 子包
            routes_dir = this_dir / "routes"
            routes_name = f"{pkg}.routes"
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

def _load_rooms() -> List[Dict[str, Any]]:
    _ensure_backend()
    from .. import data_layer
    return data_layer.load_table("rooms")


def _save_rooms(rooms: List[Dict[str, Any]]) -> None:
    _ensure_backend()
    from .. import data_layer
    data_layer.save_table("rooms", rooms)


def _find_room(rooms: List[Dict[str, Any]], room_no: str) -> Optional[Dict[str, Any]]:
    rn = (room_no or "").strip()
    for r in rooms:
        if str(r.get("room_no", "")).strip() == rn:
            return r
    return None


def _guest_room(external_userid: str) -> Dict[str, Any]:
    """客人身份 → 绑定房间。未绑定或已退房返回 error 结构。"""
    _ensure_backend()
    from ..wecom_kf import find_active_customer_by_external_userid
    ext = (external_userid or "").strip()
    if not ext:
        return {"_error": "缺少 external_userid (客人微信身份标识)。"}
    # 兼容 QwenPaw session 的 kf- 前缀格式
    if ext.startswith("kf-"):
        ext = ext[3:]
    customer = find_active_customer_by_external_userid(ext)
    if not customer:
        # 尝试查找历史客人（已退房）
        from ..wecom_kf import find_customer_by_external_userid
        hist = find_customer_by_external_userid(ext)
        if hist and hist.get("deleted"):
            return {"_error": "您已退房。如需服务请先重新绑定房间，发送「绑定 <房间号> <姓名>」即可。"}
        return {"_error": "未找到绑定记录。请先发送「绑定 <房间号> <姓名>」完成绑定。"}
    room_no = str(customer.get("room_no") or "").strip()
    if not room_no:
        return {"_error": "您已退房, 如需服务请先重新绑定房间。发送「绑定 <房间号> <姓名>」即可。"}
    # 校验房间是否还在住
    room = _find_room(_load_rooms(), room_no)
    if room and room.get("status") != "在住":
        return {"_error": f"{room_no} 当前「{room.get('status')}」, 您已退房。如需服务请重新绑定房间。"}
    return {"_customer": customer, "room_no": room_no}


def _open_orders_of(room_no: str) -> List[Dict[str, Any]]:
    _ensure_backend()
    from .. import data_layer
    out = []
    for w in data_layer.load_table("work_orders"):
        if w.get("deleted"):
            continue
        if w.get("status") in ("done", "rejected"):
            continue
        if str(w.get("room_no") or "").strip() == room_no:
            out.append(w)
    return out


def _auto_close_orders(room_no: str, work_types: List[str], operator: str) -> List[str]:
    """房间从维修中/待打扫恢复空房时, 自动关闭对应类型未结工单。返回关闭的 wo_id 列表。"""
    from . import _helpers
    _ensure_backend()
    from .. import data_layer
    closed: List[str] = []
    all_wos = data_layer.load_table("work_orders")
    changed = False
    for w in all_wos:
        if w.get("deleted"):
            continue
        if w.get("status") in ("done", "rejected"):
            continue
        if str(w.get("room_no") or "").strip() != room_no:
            continue
        if (w.get("work_type") or "") not in work_types:
            continue
        w["status"] = "done"
        w["completed_at"] = _helpers.now()
        w["completed_by"] = operator or "agent"
        w["resolution"] = (w.get("resolution") or "") + " [空房联动自动关单]"
        closed.append(w.get("wo_id", ""))
        changed = True
    if changed:
        data_layer.save_table("work_orders", all_wos)
    return closed


async def _create_order(room_no: str, work_type: str, description: str,
                  priority: str, reporter: str, target_dept: str,
                  operator: str, data_source: str = "agent") -> Dict[str, Any]:
    """创建工单 (走 service 层, 保证 safe_sync + 通知等完整链路)"""
    _ensure_backend()
    from .work_order_svc import svc_create_work_order
    result = await svc_create_work_order(
        room_no=room_no, work_type=work_type, description=description,
        priority=priority, reporter=reporter, target_dept=target_dept,
        data_source=data_source, operator=operator,
    )
    return result.get("work_order", {})


# ─────────────────────────────── 客人侧工具 ───────────────────────────────

async def guest_query_my_room(external_userid: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("guest_query_my_room")
    if _access is not None:
        return _access
    """客人查询自己房间的状态与在途工单。

    何时调用: 客人问「我房间现在什么状态」「我的报修有人处理了吗」
    「空调修好没有」等涉及自己房间与工单进度的问题时。
    参数: external_userid = 客人的微信身份标识 (external_userid, 必填)。
    自动解析客人绑定房间, 无需也不能指定其他房间。
    """
    g = _guest_room(external_userid)
    if "_error" in g:
        return {"ok": False, "error": g["_error"]}
    room_no = g["room_no"]
    rooms = _load_rooms()
    room = _find_room(rooms, room_no)
    if not room:
        return {"ok": False, "error": f"房间 {room_no} 不存在, 请核对绑定信息。"}
    orders = [
        {
            "wo_id": w.get("wo_id"),
            "work_type": w.get("work_type"),
            "status": w.get("status"),
            "priority": w.get("priority"),
            "assignee": w.get("assignee") or "待派单",
            "description": (w.get("description") or "")[:80],
            "created_at": w.get("created_at"),
        }
        for w in _open_orders_of(room_no)
    ]
    return {
        "ok": True,
        "room_no": room_no,
        "guest_name": g["_customer"].get("guest_name", ""),
        "room_status": room.get("status"),
        "room_type": room.get("room_type", ""),
        "active_orders": orders,
        "message": f"房间 {room_no} 当前「{room.get('status')}」, 在途工单 {len(orders)} 张。",
    }


async def guest_request_service(external_userid: str = "",
                                service_type: str = "",
                                description: str = "",
                                urgency: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("guest_request_service")
    if _access is not None:
        return _access
    """客人一句话提交服务请求 (报修/送物/打扫/补货等), 自动建工单。

    何时调用: 客人说「空调不制冷」「送两瓶水过来」「帮忙打扫一下」
    「毛巾不够」等服务请求时。
    参数:
      external_userid = 客人微信身份标识 (必填);
      service_type = 服务类型: 维修/送物/打扫/补货/加被/其他 (可留空让 agent 判断);
      description = 客人原话或整理后的需求描述 (建议保留原话);
      urgency = urgent/high/normal/low, 留空按服务类型默认。
    幂等: 同房间同类型 2 分钟内已有未结工单时返回已有工单而不重复建单。
    """
    g = _guest_room(external_userid)
    if "_error" in g:
        return {"ok": False, "error": g["_error"]}
    room_no = g["room_no"]
    st = (service_type or "").strip()
    if st not in _SERVICE_MAP:
        # agent 传了未知类型 → 归到「其他」并在描述里保留原话
        wo_type, dept, prio = "其他", "", "normal"
        desc = f"[{st or '服务'}] {description}" if st else (description or "客人服务请求")
    else:
        wo_type, dept, prio = _SERVICE_MAP[st]
        desc = description or f"客人{st}请求"
    if urgency in ("urgent", "high", "normal", "low"):
        prio = urgency
    # 2 分钟幂等护栏: 同房间同类型未结单直接复用
    from . import _helpers
    for w in _open_orders_of(room_no):
        if w.get("work_type") == wo_type and "agent" in str(w.get("data_source", "")):
            age = _helpers.now()
            created = str(w.get("created_at") or "")
            if created[:16] == age[:16] or created[:15] == age[:15]:
                return {"ok": True, "deduplicated": True, "wo_id": w.get("wo_id"),
                        "message": f"您的请求已在处理中 (工单 {w.get('wo_id')}), 无需重复提交。"}
    wo = await _create_order(room_no, wo_type, desc, prio,
                       reporter=f"guest:{g['_customer'].get('guest_name', '')}",
                       target_dept=dept, operator=f"guest-agent:{external_userid[:12]}",
                       data_source="guest_kf")
    # v2.0: 绑定 openid 到工单 guest_id 字段
    _openid = g['_customer'].get("openid", "")
    if _openid:
        _ensure_backend()
        from .. import data_layer
        _wos = data_layer.load_table("work_orders")
        for _w in _wos:
            if _w.get("wo_id") == wo.get("wo_id"):
                _w["guest_id"] = _openid
                break
        data_layer.save_table("work_orders", _wos)
    return {
        "ok": True,
        "wo_id": wo.get("wo_id"),
        "work_type": wo_type,
        "priority": wo.get("priority"),
        "message": f"✅ 已受理: 工单 {wo.get('wo_id')} ({wo_type}/{wo.get('priority')}), 已通知相关部门处理。",
    }


async def guest_query_my_orders(external_userid: str = "",
                                status: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("guest_query_my_orders")
    if _access is not None:
        return _access
    """客人查询自己房间的历史/在途工单列表。

    何时调用: 客人问「我之前报修的单子呢」「帮我看看都提交过什么」时。
    参数: external_userid 必填; status 可选过滤 pending/assigned/processing/done。
    """
    g = _guest_room(external_userid)
    if "_error" in g:
        return {"ok": False, "error": g["_error"]}
    room_no = g["room_no"]
    _ensure_backend()
    from .. import data_layer
    rows = []
    for w in data_layer.load_table("work_orders"):
        if w.get("deleted"):
            continue
        if str(w.get("room_no") or "").strip() != room_no:
            continue
        if status and w.get("status") != status:
            continue
        rows.append({
            "wo_id": w.get("wo_id"),
            "work_type": w.get("work_type"),
            "status": w.get("status"),
            "description": (w.get("description") or "")[:60],
            "created_at": w.get("created_at"),
            "completed_at": w.get("completed_at") or "",
        })
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"ok": True, "room_no": room_no, "total": len(rows),
            "orders": rows[:20],
            "message": f"房间 {room_no} 共 {len(rows)} 张工单 (显示最近 20 张)。"}


async def guest_request_checkout(external_userid: str = "",
                                 note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("guest_request_checkout")
    if _access is not None:
        return _access
    """客人申请退房。

    何时调用: 客人说「我要退房」「帮我办理退房」时。
    注意: 工具直接完成退房并自动派生清洁单 (与前台退房行为一致);
    如酒店流程要求人工确认, agent 应在调用前与客人确认账单/押金事项。
    幂等: 已空房的房间重复调用直接返回成功而不重复派单 (派生单有类型护栏)。
    """
    g = _guest_room(external_userid)
    if "_error" in g:
        return {"ok": False, "error": g["_error"]}
    return await _do_checkout(g["room_no"], operator=f"guest-agent:{external_userid[:12]}",
                        note=note or f"客人({g['_customer'].get('guest_name','')})对话申请退房")


async def guest_room_complaint(external_userid: str = "",
                               content: str = "",
                               urgency: str = "high") -> Dict[str, Any]:
    _access = _check_tool_access("guest_room_complaint")
    if _access is not None:
        return _access
    """客人投诉直达前台管理岗, 建高优先级工单。

    何时调用: 客人表达不满/投诉 (噪音/卫生/服务态度/收费争议) 时。
    参数: content = 投诉内容原话; urgency 默认 high, 严重可传 urgent。
    """
    g = _guest_room(external_userid)
    if "_error" in g:
        return {"ok": False, "error": g["_error"]}
    if not (content or "").strip():
        return {"ok": False, "error": "投诉内容不能为空。"}
    wo = await _create_order(
        g["room_no"], "投诉", f"客人投诉: {content.strip()[:300]}",
        urgency if urgency in ("urgent", "high") else "high",
        reporter=f"guest:{g['_customer'].get('guest_name', '')}",
        target_dept="frontdesk",
        operator=f"guest-agent:{external_userid[:12]}",
        data_source="guest_kf",
    )
    return {"ok": True, "wo_id": wo.get("wo_id"), "escalated": True,
            "message": f"非常重视您的反馈, 已转前台管理岗跟进 (工单 {wo.get('wo_id')})。"}

# v2.4-roomfix: 客人绑定/换房工具 (让 AI 能真正改绑而非空口承诺)
async def guest_bind_room(external_userid: str = "", room_no: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("guest_bind_room")
    if _access is not None:
        return _access
    """客人报房号/换房时更新绑定房间。

    何时调用: 客人说「我住1401」「换房到0505」「房号改成1401」「帮我登记1401」时。
    行为: 校验房间存在 → 保留原姓名/手机号 → bind_customer 改绑; 并与前台入住记录(t_checkins)对账。
    首次绑定缺姓名时不臆造, 引导客人发送完整「绑定 <房间号> <姓名>」。
    """
    _ensure_backend()
    from ..wecom_kf import bind_customer, find_customer_by_external_userid
    ext = (external_userid or "").strip()
    if ext.startswith("kf-"):
        ext = ext[3:]
    room_no = str(room_no or "").strip()
    if not ext:
        return {"ok": False, "error": "缺少 external_userid (客人身份标识)。"}
    if not room_no:
        return {"ok": False, "error": "缺少 room_no (房间号)。"}
    if not _find_room(_load_rooms(), room_no):
        return {"ok": False, "error": f"房间 {room_no} 不存在，请核对房号。"}
    existing = find_customer_by_external_userid(ext)
    gname = ((existing or {}).get("guest_name") or "").strip()
    gphone = (existing or {}).get("guest_phone") or ""
    if not gname:
        return {"ok": False,
                "error": f"首次绑定需登记姓名，请让客人发送：绑定 {room_no} <姓名>。"}
    cust = bind_customer(ext, room_no, gname, guest_phone=gphone)
    return {"ok": True, "room_no": room_no, "guest_name": gname,
            "customer_id": cust.get("customer_id", ""),
            "message": f"✅ 已绑定/更新房间 {room_no}（{gname}），后续服务以此房号为准。"}



# ─────────────────────────────── 员工侧工具 ───────────────────────────────

async def staff_room_query(operator: str = "", room_no: str = "",
                           status: str = "", floor: str = "",
                           room_type: str = "", keyword: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_room_query")
    if _access is not None:
        return _access
    """查询房态: 单房详情 / 按条件过滤列表 / 统计。

    何时调用: 员工问「现在空房有哪些」「0404 什么状态」「三楼的房都满了吗」
    「帮我找两间大床空房」时。
    参数均可选, 全留空返回全店统计概览。keyword 匹配房号/房型/客人姓名。
    """
    rooms = _load_rooms()
    keyword = (keyword or "").strip()
    if keyword:
        rooms = [r for r in rooms if keyword in str(r.get("room_no", ""))
                 or keyword in str(r.get("room_type", ""))
                 or keyword in str(r.get("guest_name", ""))]
    if (room_no or "").strip():
        rooms = [r for r in rooms if str(r.get("room_no", "")).strip() == room_no.strip()]
    if (status or "").strip():
        rooms = [r for r in rooms if r.get("status") == status.strip()]
    if (floor or "").strip():
        f = floor.strip()
        rooms = [r for r in rooms if f in str(r.get("room_no", ""))]
    if (room_type or "").strip():
        rooms = [r for r in rooms if room_type.strip() in str(r.get("room_type", ""))]
    brief = [
        {"room_no": r.get("room_no"), "status": r.get("status"),
         "room_type": r.get("room_type"), "guest_name": r.get("guest_name") or "",
         "active_orders": len(_open_orders_of(str(r.get("room_no", ""))))}
        for r in rooms[:100]
    ]
    stats: Dict[str, int] = {}
    for r in _load_rooms():
        s = str(r.get("status") or "未知")
        stats[s] = stats.get(s, 0) + 1
    return {"ok": True, "count": len(rooms), "rooms": brief, "all_stats": stats,
            "operator": operator or "agent"}


async def staff_room_checkin(operator: str = "", room_no: str = "",
                             guest_name: str = "", guest_phone: str = "",
                             days: int = 1, note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_room_checkin")
    if _access is not None:
        return _access
    """办理入住: 设置房间在住 + 登记客人。

    何时调用: 员工说「给 0404 办入住, 张三, 住两晚」「1802 客人来了, 李四」时。
    护栏: 非空房 (在住/维修中) 拒绝入住并提示先处理; 重复入住同客人视为续住确认。
    """
    if not (room_no or "").strip() or not (guest_name or "").strip():
        return {"ok": False, "error": "room_no 和 guest_name 必填。"}
    rooms = _load_rooms()
    room = _find_room(rooms, room_no)
    if not room:
        return {"ok": False, "error": f"房间 {room_no} 不存在。"}
    cur = room.get("status")
    if cur == "在住":
        if (room.get("guest_name") or "") == guest_name.strip():
            return {"ok": True, "deduplicated": True, "room_no": room_no,
                    "message": f"{room_no} 已是 {guest_name} 在住, 视为续住确认。"}
        return {"ok": False, "error": f"{room_no} 已有客人 {room.get('guest_name')} 在住, 不能重复入住。"}
    if cur == "维修中":
        return {"ok": False, "error": f"{room_no} 维修中, 不能入住。可先完成维修工单或改派房间。"}
    room["status"] = "在住"
    room["guest_name"] = guest_name.strip()[:30]
    room["guest_phone"] = (guest_phone or "").strip()[:20]
    room["checkin_at"] = _now_str()
    room["checkin_days"] = int(days or 1)
    if note:
        room["notes"] = note.strip()[:200]
    _save_rooms(rooms)
    return {"ok": True, "room_no": room_no, "guest_name": guest_name.strip(),
            "status": "在住", "checkin_days": int(days or 1),
            "message": f"✅ {room_no} 已办理入住: {guest_name.strip()}, {int(days or 1)} 晚。"}


def _now_str() -> str:
    from . import _helpers
    return _helpers.now()


async def _do_checkout(room_no: str, operator: str, note: str = "") -> Dict[str, Any]:
    """退房实现 (客人/员工工具共用): 清空房间 + 清理客人绑定 + 自动派生清洁单。"""
    rooms = _load_rooms()
    room = _find_room(rooms, room_no)
    if not room:
        return {"ok": False, "error": f"房间 {room_no} 不存在。"}
    if room.get("status") != "在住":
        return {"ok": False, "error": f"{room_no} 当前「{room.get('status')}」非在住, 无法退房。"}
    guest = room.get("guest_name") or ""
    room["status"] = "空房"
    room["guest_name"] = ""
    room["guest_phone"] = ""
    room["checkin_at"] = ""
    room["checkin_days"] = 0
    if note:
        room["notes"] = note[:200]
    _save_rooms(rooms)

    # 清理客人绑定关系 (t_customers: room_no 保留历史, 仅标记退房)
    try:
        from .. import data_layer
        customers = data_layer.load_table("customers")
        changed = False
        for c in customers:
            if c.get("room_no") == room_no and not c.get("deleted"):
                c["deleted"] = True  # 标记当前住店会话结束
                c["room_no_checkout"] = room_no  # 保留退房前的房号用于历史查询
                c["guest_name_checkout"] = c.get("guest_name", "")
                c["unbound_at"] = _now_str()
                c["checkout_at"] = _now_str()
                c["updated_at"] = _now_str()
                # 注意: room_no 和 openid 保留, 下次绑定时可识别为回访客人
                changed = True
        if changed:
            data_layer.save_table("customers", customers)
            logger.info("[checkout] 已标记 %s 的客人会话结束 (记录保留)", room_no)
    except Exception as exc:
        logger.warning("[checkout] 清理客人绑定失败(不影响退房): %s", exc)

    wo = await _create_order(room_no, "清洁", "退房清洁 (退房联动自动创建)",
                       "normal", reporter=f"checkout:{guest}", target_dept="housekeeping",
                       operator=operator)
    return {"ok": True, "room_no": room_no, "previous_guest": guest,
            "status": "空房", "cleaning_wo_id": wo.get("wo_id"),
            "message": f"✅ {room_no} 已退房 (原客人 {guest or '未登记'}), 清洁单 {wo.get('wo_id')} 已自动派给客房部。"}


async def staff_room_checkout(operator: str = "", room_no: str = "",
                              note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_room_checkout")
    if _access is not None:
        return _access
    """办理退房: 房间转空 + 自动派生清洁单。

    何时调用: 员工说「0404 退房」「帮 1802 结账退房」时。
    幂等: 非在住房间拒绝并提示当前状态。
    """
    if not (room_no or "").strip():
        return {"ok": False, "error": "room_no 必填。"}
    return await _do_checkout(room_no.strip(), operator=operator or "agent", note=note)


async def staff_room_status_set(operator: str = "", room_no: str = "",
                                status: str = "", note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_room_status_set")
    if _access is not None:
        return _access
    """设置房态 (空房/在住/待打扫/维修中), 自动联动建单/关单。

    何时调用: 员工说「把 0404 设为维修中」「0303 打扫完置空房」「0505 待打扫」时。
    联动规则 (与房态看板一致):
      → 维修中 (原空房): 自动建维修单 (engineering);
      → 待打扫 (原空房): 自动建清洁单 (housekeeping);
      → 空房 (原维修中/待打扫): 自动关闭对应未结单。
    在住房设置维修中/待打扫会拒绝 (需先退房)。
    """
    if not (room_no or "").strip() or not (status or "").strip():
        return {"ok": False, "error": "room_no 和 status 必填。"}
    status = status.strip()
    if status not in _VALID_ROOM_STATUS:
        return {"ok": False, "error": f"status 必须是 {_VALID_ROOM_STATUS} 之一。"}
    rooms = _load_rooms()
    room = _find_room(rooms, room_no)
    if not room:
        return {"ok": False, "error": f"房间 {room_no} 不存在。"}
    old = room.get("status")
    if old == status:
        return {"ok": True, "deduplicated": True, "room_no": room_no,
                "message": f"{room_no} 已是「{status}」, 无需变更。"}
    if old == "在住" and status != "空房":
        return {"ok": False, "error": f"{room_no} 在住中。请先退房再置 {status}。"}
    room["status"] = status
    if status == "空房":
        room["guest_name"] = ""
        room["guest_phone"] = ""
    if note:
        room["notes"] = note.strip()[:200]
    _save_rooms(rooms)

    linked_wo = ""
    closed: List[str] = []
    if status == "维修中" and old == "空房":
        wo = await _create_order(room_no, "维修", note or "房态置维修中 (联动自动创建)",
                           "normal", reporter="staff", target_dept="engineering",
                           operator=operator or "agent")
        linked_wo = wo.get("wo_id", "")
    elif status == "待打扫" and old == "空房":
        wo = await _create_order(room_no, "清洁", note or "房态置待打扫 (联动自动创建)",
                           "normal", reporter="staff", target_dept="housekeeping",
                           operator=operator or "agent")
        linked_wo = wo.get("wo_id", "")
    elif status == "空房" and old in ("维修中", "待打扫"):
        types = ["维修"] if old == "维修中" else ["清洁"]
        closed = _auto_close_orders(room_no, types, operator or "agent")
    msg = f"✅ {room_no}: {old} → {status}。"
    if linked_wo:
        msg += f" 已自动建单 {linked_wo}。"
    if closed:
        msg += f" 已自动关单: {', '.join(closed)}。"
    return {"ok": True, "room_no": room_no, "old_status": old, "status": status,
            "linked_wo_id": linked_wo, "closed_wo_ids": closed, "message": msg}


async def staff_room_change(operator: str = "", from_room: str = "",
                            to_room: str = "", note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_room_change")
    if _access is not None:
        return _access
    """换房: 原房退房(派清洁单) + 目标房入住, 客人信息整体迁移。

    何时调用: 客人要求换房, 员工说「把 0404 的客人换到 0505」时。
    护栏: 原房必须在住; 目标房必须空房。两步任一失败自动回滚。
    """
    if not (from_room or "").strip() or not (to_room or "").strip():
        return {"ok": False, "error": "from_room 和 to_room 必填。"}
    rooms = _load_rooms()
    src = _find_room(rooms, from_room)
    dst = _find_room(rooms, to_room)
    if not src:
        return {"ok": False, "error": f"原房间 {from_room} 不存在。"}
    if not dst:
        return {"ok": False, "error": f"目标房间 {to_room} 不存在。"}
    if src.get("status") != "在住":
        return {"ok": False, "error": f"{from_room} 非在住 ({src.get('status')}), 无法换房。"}
    if dst.get("status") != "空房":
        return {"ok": False, "error": f"{to_room} 非空房 ({dst.get('status')}), 不能入住。"}
    guest = src.get("guest_name") or ""
    phone = src.get("guest_phone") or ""
    days = src.get("checkin_days") or 1
    src["status"] = "空房"
    src["guest_name"] = src["guest_phone"] = src["checkin_at"] = ""
    src["checkin_days"] = 0
    src["notes"] = f"换出至 {to_room}" + (f" ({note})" if note else "")
    dst["status"] = "在住"
    dst["guest_name"] = guest
    dst["guest_phone"] = phone
    dst["checkin_days"] = days
    dst["checkin_at"] = _now_str()
    dst["notes"] = f"自 {from_room} 换入" + (f" ({note})" if note else "")
    _save_rooms(rooms)
    wo = await _create_order(from_room, "清洁", f"换房清洁 (客人换至 {to_room})",
                       "normal", reporter="staff", target_dept="housekeeping",
                       operator=operator or "agent")
    return {"ok": True, "from_room": from_room, "to_room": to_room, "guest": guest,
            "cleaning_wo_id": wo.get("wo_id"),
            "message": f"✅ 已换房: {guest} 从 {from_room} → {to_room}; 原房清洁单 {wo.get('wo_id')}。"}


async def staff_work_order_create(operator: str = "", room_no: str = "",
                                  work_type: str = "", description: str = "",
                                  priority: str = "normal",
                                  assignee: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_work_order_create")
    if _access is not None:
        return _access
    """创建任意类型工单 (维修/清洁/送物/补货/投诉/其他)。

    何时调用: 员工口头派活, 如「记一下, 0808 马桶漏水, 高优先级派给刘工」。
    work_type 自由文本, 常用: 维修/清洁/送物/补货/投诉/其他。
    """
    if not (room_no or "").strip() or not (work_type or "").strip():
        return {"ok": False, "error": "room_no 和 work_type 必填。"}
    if not _find_room(_load_rooms(), room_no):
        return {"ok": False, "error": f"房间 {room_no} 不存在, 请核对。"}
    from .work_order_svc import svc_create_work_order, svc_assign_work_order
    result = await svc_create_work_order(
        room_no=room_no.strip(), work_type=work_type.strip()[:10],
        description=(description or f"{work_type}工单").strip()[:300],
        priority=priority if priority in ("urgent", "high", "normal", "low") else "normal",
        reporter="staff", target_dept="", data_source="agent",
        operator=operator or "agent",
    )
    wo = result.get("work_order", {})
    if (assignee or "").strip():
        assign_result = await svc_assign_work_order(
            wo_id=wo.get("wo_id"), assignee=assignee.strip(),
            operator=operator or "agent",
        )
        if assign_result.get("ok"):
            wo = assign_result.get("work_order", wo)
    return {"ok": True, "wo_id": wo.get("wo_id"), "status": wo.get("status"),
            "assignee": wo.get("assignee") or "",
            "message": f"✅ 工单 {wo.get('wo_id')} 已创建 ({work_type}/{wo.get('priority')})"
                       + (f", 已派 {assignee}" if assignee else ", 待派单。")}


async def staff_work_order_assign(operator: str = "", wo_id: str = "",
                                  assignee: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_work_order_assign")
    if _access is not None:
        return _access
    """把工单派给指定员工。

    何时调用: 员工说「把刚才那张单派给刘工」「维修单转给工程部小王」时。
    """
    if not (wo_id or "").strip() or not (assignee or "").strip():
        return {"ok": False, "error": "wo_id 和 assignee 必填。"}
    from .work_order_svc import svc_assign_work_order
    result = await svc_assign_work_order(
        wo_id=wo_id.strip(), assignee=assignee.strip(),
        operator=operator or "agent",
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "wo_id": wo_id, "assignee": assignee.strip(),
            "message": f"✅ 工单 {wo_id} 已派给 {assignee.strip()}。"}


async def staff_work_order_complete(operator: str = "", wo_id: str = "",
                                    note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_work_order_complete")
    if _access is not None:
        return _access
    """完成工单。维修/清洁单完成且房间非在住时, 自动把房间还原为空房。

    何时调用: 员工说「马桶修好了, 关单」「0404 打扫完成」时。
    在住房完成维修/清洁单不动房态 (在住保护)。
    """
    if not (wo_id or "").strip():
        return {"ok": False, "error": "wo_id 必填。"}
    from .work_order_svc import svc_complete_work_order
    result = await svc_complete_work_order(
        wo_id=wo_id.strip(), operator=operator or "agent",
        result_note=note,
    )
    if not result.get("ok"):
        return result
    room_msg = result.get("room_msg", "")
    return {"ok": True, "wo_id": wo_id, "status": "done",
            "message": f"✅ 工单 {wo_id} 已完成。" + room_msg}


async def staff_work_order_list(operator: str = "", status: str = "",
                                room_no: str = "", assignee: str = "",
                                work_type: str = "",
                                today_only: bool = False) -> Dict[str, Any]:
    _access = _check_tool_access("staff_work_order_list")
    if _access is not None:
        return _access
    """查询工单: 按状态/房间/负责人/类型/今日过滤。

    何时调用: 员工问「今天还有哪些维修单」「刘工手上有几张单」
    「0404 的工单都完成了吗」时。全留空返回今日在途汇总。
    """
    _ensure_backend()
    from .. import data_layer
    from . import _helpers
    today = _helpers.today()
    rows = []
    for w in data_layer.load_table("work_orders"):
        if w.get("deleted"):
            continue
        if status and w.get("status") != status:
            continue
        if room_no and str(w.get("room_no") or "").strip() != room_no.strip():
            continue
        if assignee and assignee.strip() not in str(w.get("assignee") or ""):
            continue
        if work_type and work_type.strip() not in str(w.get("work_type") or ""):
            continue
        if today_only and not str(w.get("created_at") or "").startswith(today):
            continue
        rows.append({
            "wo_id": w.get("wo_id"), "room_no": w.get("room_no"),
            "work_type": w.get("work_type"), "status": w.get("status"),
            "priority": w.get("priority"), "assignee": w.get("assignee") or "",
            "description": (w.get("description") or "")[:60],
            "created_at": w.get("created_at"),
        })
    rows.sort(key=lambda x: (x.get("status") in ("done", "rejected"),
                             x.get("created_at") or ""))
    return {"ok": True, "total": len(rows), "orders": rows[:50],
            "message": f"共 {len(rows)} 张匹配工单 (显示 50 张)。"}


async def staff_today_overview(operator: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_today_overview")
    if _access is not None:
        return _access
    """今日经营概览: 入住率/房态分布/在途工单/今日新增/今日完成。

    何时调用: 员工或管理者问「今天酒店情况怎么样」「入住率多少」
    「还有多少没处理的单」时; 也是每日晨会/交接班的快速数据源。
    """
    _ensure_backend()
    from .. import data_layer
    from . import _helpers
    today = _helpers.today()
    rooms = _load_rooms()
    stats: Dict[str, int] = {}
    for r in rooms:
        s = str(r.get("status") or "未知")
        stats[s] = stats.get(s, 0) + 1
    total = len(rooms) or 1
    open_orders: Dict[str, int] = {}
    today_new = today_done = 0
    for w in data_layer.load_table("work_orders"):
        if w.get("deleted"):
            continue
        st = str(w.get("status") or "")
        if str(w.get("created_at") or "").startswith(today):
            today_new += 1
        if st in ("done", "rejected"):
            if st == "done" and str(w.get("completed_at") or "").startswith(today):
                today_done += 1
            continue
        key = f"{w.get('work_type')}/{st}"
        open_orders[key] = open_orders.get(key, 0) + 1
    occupied = stats.get("在住", 0)
    return {
        "ok": True,
        "date": today,
        "rooms_total": len(rooms),
        "room_stats": stats,
        "occupancy_rate": round(occupied / total * 100, 1),
        "occupied": occupied,
        "open_orders": open_orders,
        "today_new_orders": today_new,
        "today_done_orders": today_done,
        "message": (f"今日概览: 房间 {len(rooms)} 间, 在住 {occupied} 间 "
                    f"({round(occupied / total * 100, 1)}%); 在途工单 {sum(open_orders.values())} 张, "
                    f"今日新增 {today_new}, 今日完成 {today_done}。"),
    }


# ──────────────────────────── Request/Ticket 工具 ────────────────────────────

async def staff_request_create(operator: str = "", room_no: str = "",
                               description: str = "", contact_name: str = "",
                               contact_phone: str = "",
                               work_type: str = "其他",
                               priority: str = "normal") -> Dict[str, Any]:
    _access = _check_tool_access("staff_request_create")
    if _access is not None:
        return _access
    """创建需求单 (XQ) 并自动生成工单 (Ticket)。

    何时调用: 前台接到客人复杂需求（含多个工单）时，如「客人要维修+清洁」。
    比 staff_work_order_create 更适合多意图场景。
    """
    if not (room_no or "").strip():
        return {"ok": False, "error": "room_no 必填。"}
    if not (description or "").strip():
        return {"ok": False, "error": "description 必填。"}
    _ensure_backend()
    from .dispatch import _create_request_from_payload
    try:
        result = await _create_request_from_payload({
            "room_no": room_no.strip(),
            "description": description.strip()[:300],
            "contact_name": (contact_name or operator or "staff").strip(),
            "contact_phone": (contact_phone or "").strip(),
            "intents": [{
                "work_type": work_type,
                "description": description.strip()[:300],
                "room_no": room_no.strip(),
                "priority": priority,
            }],
            "source": "agent_tool",
        }, created_by=operator or "agent")
        req = result.get("request", {})
        tickets = result.get("tickets", [])
        return {"ok": True,
                "request_id": req.get("request_id"),
                "tickets": [t.get("wo_id") for t in tickets],
                "message": f"✅ 需求单 {req.get('request_id')} 已创建, 含 {len(tickets)} 张工单。"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def staff_request_list(operator: str = "", status: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_request_list")
    if _access is not None:
        return _access
    """查询需求单 (XQ) 列表。

    何时调用: 员工问「有哪些未完成的需求」「今天的需求单」时。
    """
    _ensure_backend()
    from .dispatch import _load_requests
    reqs = _load_requests()
    if status:
        reqs = [r for r in reqs if r.get("status") == status]
    rows = [{"request_id": r.get("request_id"), "room_no": r.get("room_no"),
             "description": (r.get("description") or "")[:60],
             "status": r.get("status"), "created_at": r.get("created_at"),
             "contact_name": r.get("contact_name", "")}
            for r in reqs[:50]]
    return {"ok": True, "total": len(rows), "requests": rows}


async def staff_ticket_list(operator: str = "", status: str = "",
                            mine: bool = False) -> Dict[str, Any]:
    _access = _check_tool_access("staff_ticket_list")
    if _access is not None:
        return _access
    """查询工单 (Ticket) 列表。

    何时调用: 员工问「我手上有哪些单」「未分配的工单」时。
    mine=True 只返回分配给自己的。
    """
    _ensure_backend()
    from .dispatch import _load_tickets
    tickets = _load_tickets()
    tickets = [t for t in tickets if not t.get("deleted")]
    if status:
        tickets = [t for t in tickets if t.get("status") == status]
    if mine:
        tickets = [t for t in tickets if t.get("assignee") == operator]
    rows = [{"wo_id": t.get("wo_id"), "request_id": t.get("request_id"),
             "work_type": t.get("work_type"), "room_no": t.get("room_no"),
             "status": t.get("status"), "assignee": t.get("assignee", ""),
             "priority": t.get("priority", ""),
             "description": (t.get("description") or "")[:60]}
            for t in tickets[:50]]
    return {"ok": True, "total": len(rows), "tickets": rows}


async def staff_ticket_assign(operator: str = "", ticket_id: str = "",
                              assignee: str = "", target_dept: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_ticket_assign")
    if _access is not None:
        return _access
    """指派工单 (Ticket) 给员工或部门。

    何时调用: 前台调度说「把这张单派给工程部」时。
    """
    if not (ticket_id or "").strip():
        return {"ok": False, "error": "ticket_id 必填。"}
    _ensure_backend()
    from .. import data_layer
    from .dispatch import _load_tickets, _sync_request_status, _notify_department_assign
    tickets = _load_tickets()
    ticket = next((t for t in tickets if t.get("wo_id") == ticket_id), None)
    if not ticket:
        return {"ok": False, "error": f"工单 {ticket_id} 不存在"}
    if ticket.get("deleted"):
        return {"ok": False, "error": f"工单 {ticket_id} 已删除"}
    if target_dept:
        ticket["target_dept"] = target_dept
    if assignee:
        ticket["assignee"] = assignee
    ticket["status"] = "assigned"
    ticket["updated_at"] = _now_str()
    ticket["assigned_by"] = operator or "agent"
    data_layer.save_table("work_orders", tickets)
    try:
        await _sync_request_status(ticket.get("request_id", ""))
    except Exception:
        pass
    try:
        await _notify_department_assign(ticket)
    except Exception:
        pass
    return {"ok": True, "ticket": ticket,
            "message": f"✅ 工单 {ticket_id} 已指派给 {assignee or target_dept}"}


async def staff_ticket_transition(operator: str = "", ticket_id: str = "",
                                  action: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_ticket_transition")
    if _access is not None:
        return _access
    """工单 (Ticket) 状态流转。

    action: accept / in_progress / complete / cancel / escalate
    何时调用: 员工说「接单」「开始维修」「维修完成」时。
    """
    if not (ticket_id or "").strip() or not (action or "").strip():
        return {"ok": False, "error": "ticket_id 和 action 必填。"}
    action = action.lower()
    valid = {"accept", "in_progress", "complete", "cancel", "escalate"}
    if action not in valid:
        return {"ok": False, "error": f"action 必须是 {', '.join(valid)} 之一"}
    _ensure_backend()
    from .. import data_layer
    from .dispatch import _load_tickets, _sync_request_status, TICKET_STATUS, VALID_TRANSITIONS
    tickets = _load_tickets()
    ticket = next((t for t in tickets if t.get("wo_id") == ticket_id), None)
    if not ticket:
        return {"ok": False, "error": f"工单 {ticket_id} 不存在"}
    if ticket.get("deleted"):
        return {"ok": False, "error": f"工单 {ticket_id} 已删除"}
    mapping = {
        "accept": ("assigned", "accepted"),
        "in_progress": ("accepted", "in_progress"),
        "complete": ("in_progress", "completed_pending_guest"),
        "cancel": (ticket.get("status", ""), "cancelled"),
        "escalate": (ticket.get("status", ""), "escalated"),
    }
    _from, _to = mapping[action]
    current = ticket.get("status", "")
    if action not in ("cancel", "escalate") and current != _from:
        return {"ok": False, "error": f"当前状态 {current}, 需要 {_from} 才能 {action}"}
    ticket["status"] = _to
    ticket["updated_at"] = _now_str()
    ticket[f"{action}_by"] = operator or "agent"
    ticket[f"{action}_at"] = _now_str()
    if _to == "completed":
        ticket["completed_at"] = _now_str()
    data_layer.save_table("work_orders", tickets)
    try:
        await _sync_request_status(ticket.get("request_id", ""))
    except Exception:
        pass
    return {"ok": True, "ticket": ticket,
            "message": f"✅ 工单 {ticket_id} 状态: {current} → {_to}"}


# ──────────────────────────── 排班工具 ────────────────────────────

async def staff_schedule_query(operator: str = "", date: str = "",
                               dept: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_schedule_query")
    if _access is not None:
        return _access
    """查询排班表：今天/指定日期谁在岗，哪个部门。

    何时调用: 员工问「今天谁上班」「工程部今天谁在」时。
    date: YYYY-MM-DD, 空=今天。
    dept: engineering/housekeeping/frontdesk, 空=全部。
    同时返回排班数据和自动推断的在岗人员。
    """
    if not date:
        date = _now_str()[:10]
    _ensure_backend()
    from .work_order_svc import _load_schedules, get_on_shift_staff, _DEPT_TO_DEPT_IDS

    # 排班表数据
    schedules = _load_schedules(date, dept)

    # 各部门在岗推断
    depts = [dept] if dept else ["engineering", "housekeeping", "frontdesk"]
    on_shift = {}
    for d in depts:
        staff_list = get_on_shift_staff(d, date)
        if staff_list:
            on_shift[d] = [{"name": n, "source": src} for n, src in staff_list]

    return {
        "ok": True,
        "date": date,
        "schedules": schedules,
        "on_shift": on_shift,
        "message": f"排班查询 {date}: " + "; ".join(
            f"{d} → {', '.join(s['name'] for s in staff)}"
            for d, staff in on_shift.items()
        ) if on_shift else f"{date} 暂无排班数据",
    }


async def staff_schedule_set(operator: str = "", week_start: str = "",
                             dept: str = "", staff_name: str = "",
                             shifts: str = "",
                             floor_zones: str = "",
                             skills: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_schedule_set")
    if _access is not None:
        return _access
    """设置某人一周排班（酒店7天轮班制）。

    何时调用:
      工程主管: "我本周周一到周五早班，周六日休息，1-3楼，水电空调"
        → staff_schedule_set(dept=engineering, staff_name="Lee",
            shifts="早早早早早休休", floor_zones="1-3", skills="水电,空调")
      客房主管: "李阿姨本周中班，周三周四休息，4-6楼清洁"
        → staff_schedule_set(dept=housekeeping, staff_name="李逍遥",
            shifts="中中休休中中中", floor_zones="4-6", skills="客房清洁")

    参数:
      week_start: 周一日期 YYYY-MM-DD (空=本周)
      dept: engineering/housekeeping/frontdesk (必填)
      staff_name: 员工姓名 (必填)
      shifts: 7个班次码，周一到周日，如 "早早早早早休休" 或 "MMMMMR R"
             早/M=早班(06-14) 中/A=中班(14-22) 晚/N=晚班(22-06) 休/R=休息
      floor_zones: 负责楼层 (可选)
      skills: 技能标签 (可选)
    """
    if not dept:
        return {"ok": False, "error": "dept 必填"}
    if not (staff_name or "").strip():
        return {"ok": False, "error": "staff_name 必填"}
    if not (shifts or "").strip():
        return {"ok": False, "error": "shifts 必填 (7个班次码，如 '早早早早早休休')"}

    _ensure_backend()
    from .schedule_mgmt import _week_start as ws, SHIFT_CODES, SHIFT_DEFS

    if not week_start:
        week_start = ws(_now_str()[:10])

    # 解析 shifts 字符串 → 7个标准码
    raw = shifts.strip()
    parsed = []
    for ch in raw:
        ch = ch.strip()
        if not ch:
            continue
        code = SHIFT_CODES.get(ch, "")
        if code:
            parsed.append(code)
        elif ch in ("M", "A", "N", "R"):
            parsed.append(ch)

    if len(parsed) != 7:
        return {"ok": False, "error": f"shifts 需要7个班次码，当前解析到{len(parsed)}个: {parsed}"}

    # 调用 schedule_mgmt 的 upsert 逻辑
    from .. import data_layer
    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    existing = None
    for s in all_scheds:
        if (s.get("week_start") == week_start and s.get("dept") == dept
                and s.get("staff_name") == staff_name.strip() and not s.get("deleted")):
            existing = s
            break

    if existing:
        existing["shifts"] = parsed
        existing["floor_zones"] = floor_zones.strip()
        existing["skills"] = skills.strip()
        existing["updated_at"] = _now_str()
    else:
        all_scheds.append({
            "week_start": week_start,
            "dept": dept,
            "staff_name": staff_name.strip(),
            "shifts": parsed,
            "floor_zones": floor_zones.strip(),
            "skills": skills.strip(),
            "created_at": _now_str(),
            "created_by": operator or "agent",
        })

    data_layer.save_table("schedules", all_scheds)

    # 可读摘要
    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    summary = " ".join(f"{d}:{SHIFT_DEFS.get(c,{}).get('name',c)}" for d, c in zip(day_names, parsed))
    extras = []
    if floor_zones:
        extras.append(f"楼层:{floor_zones}")
    if skills:
        extras.append(f"技能:{skills}")
    extra_msg = f" ({', '.join(extras)})" if extras else ""

    return {
        "ok": True,
        "week_start": week_start,
        "dept": dept,
        "staff_name": staff_name.strip(),
        "shifts": parsed,
        "message": f"✅ {staff_name} {week_start}周排班: {summary}{extra_msg}",
    }


# ──────────────────────────── Pending 工具 ────────────────────────────

async def staff_pending_list(operator: str = "", status: str = "pending",
                             action_type: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_pending_list")
    if _access is not None:
        return _access
    """查询待确认操作队列。

    何时调用: 员工问「有哪些要确认的」「待确认列表」时。
    status: pending(默认) / confirmed / rejected
    """
    _ensure_backend()
    from .. import data_layer
    actions = data_layer.load_table("pending_actions")
    result = []
    for a in actions:
        if status and a.get("status") != status:
            continue
        if action_type and a.get("action_type") != action_type:
            continue
        result.append({
            "action_id": a.get("action_id"),
            "action_type": a.get("action_type"),
            "target_table": a.get("target_table"),
            "status": a.get("status"),
            "assigned_to": a.get("assigned_to", ""),
            "created_at": a.get("created_at", ""),
            "description": str(a.get("target_record", {}))[:100],
        })
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"ok": True, "total": len(result), "actions": result[:30]}


async def staff_pending_confirm(operator: str = "", action_id: str = "",
                                note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_pending_confirm")
    if _access is not None:
        return _access
    """确认一条待确认操作（真正落地执行）。

    何时调用: 员工确认 AI 建议的操作时，如「确认入住」「确认派单」。
    """
    if not (action_id or "").strip():
        return {"ok": False, "error": "action_id 必填。"}
    _ensure_backend()
    from .. import data_layer
    actions = data_layer.load_table("pending_actions")
    for a in actions:
        if a.get("action_id") == action_id:
            if a.get("status") != "pending":
                return {"ok": False, "error": f"状态 {a.get('status')}, 不能确认"}
            a["status"] = "confirmed"
            a["confirmed_by"] = operator or "agent"
            a["confirmed_at"] = _now_str()
            a["confirm_note"] = note
            data_layer.save_table("pending_actions", actions)
            return {"ok": True, "action_id": action_id,
                    "message": f"✅ 已确认操作 {action_id}"}
    return {"ok": False, "error": f"操作 {action_id} 不存在"}


async def staff_pending_reject(operator: str = "", action_id: str = "",
                               note: str = "") -> Dict[str, Any]:
    _access = _check_tool_access("staff_pending_reject")
    if _access is not None:
        return _access
    """拒绝一条待确认操作。

    何时调用: 员工认为 AI 建议的操作不合理时。
    """
    if not (action_id or "").strip():
        return {"ok": False, "error": "action_id 必填。"}
    _ensure_backend()
    from .. import data_layer
    actions = data_layer.load_table("pending_actions")
    for a in actions:
        if a.get("action_id") == action_id:
            if a.get("status") != "pending":
                return {"ok": False, "error": f"状态 {a.get('status')}, 不能拒绝"}
            a["status"] = "rejected"
            a["rejected_by"] = operator or "agent"
            a["rejected_at"] = _now_str()
            a["reject_note"] = note
            data_layer.save_table("pending_actions", actions)
            return {"ok": True, "action_id": action_id,
                    "message": f"❌ 已拒绝操作 {action_id}"}
    return {"ok": False, "error": f"操作 {action_id} 不存在"}


# ─────────────────────────────── 注册 ───────────────────────────────

_TOOLS = [
    # (函数, 工具名, 描述, icon)
    (guest_query_my_room, "guest_query_my_room",
     "客人查自己房间状态与在途工单。何时调用: 客人问「我房间什么状态」「我的报修进展」时。"
     "external_userid 必填, 自动解析绑定房间。", "🛎️"),
    (guest_request_service, "guest_request_service",
     "客人一句话提交服务请求(报修/送物/打扫/补货)自动建单。何时调用: 客人说「空调不制冷」"
     "「送两瓶水」「帮忙打扫」时。2分钟内同类型未结单自动去重。", "🔧"),
    (guest_query_my_orders, "guest_query_my_orders",
     "客人查自己房间的工单历史。何时调用: 客人问「我之前报修的单子呢」时。", "📋"),
    (guest_request_checkout, "guest_request_checkout",
     "客人对话申请退房, 自动派生清洁单。何时调用: 客人说「我要退房」时。"
     "若酒店要求人工确认账单, 调用前先与客人确认。", "🚪"),
    (guest_room_complaint, "guest_room_complaint",
     "客人投诉直达前台管理岗, 建高优先级工单。何时调用: 客人表达投诉/不满时。"
     "content 保留客人原话。", "⚠️"),
    # v2.4-roomfix
    (guest_bind_room, "guest_bind_room",
     "客人报房号/换房时更新绑定房间。何时调用: 客人说「我住1401」「换房到0505」"
     "「房号改成1401」「帮我登记1401」时。自动校验房间并保留姓名/手机, 与前台入住记录对账。", "🔑"),
    (staff_room_query, "staff_room_query",
     "查询房态: 单房/条件过滤/全店统计。何时调用: 员工问「空房有哪些」「0404 状态」"
     "「帮我找间大床房」时。全留空返回统计概览。", "🔍"),
    (staff_room_checkin, "staff_room_checkin",
     "办理入住。何时调用: 员工说「给0404办入住,张三,住两晚」时。"
     "非空房拒绝; 重复入住同客人视为续住确认。", "🛏️"),
    (staff_room_checkout, "staff_room_checkout",
     "办理退房, 自动派生清洁单。何时调用: 员工说「0404 退房」时。", "🧾"),
    (staff_room_status_set, "staff_room_status_set",
     "设置房态并自动联动建/关工单。何时调用: 员工说「把0404设为维修中」"
     "「0303打扫完置空房」时。在住房仅允许置空房。", "🏷️"),
    (staff_room_change, "staff_room_change",
     "换房: 原房退房派清洁 + 目标房入住, 客人信息迁移。何时调用: 「把0404客人换到0505」时。", "🔄"),
    (staff_work_order_create, "staff_work_order_create",
     "创建任意类型工单。何时调用: 员工口头派活「0808马桶漏水,高优,派刘工」时。", "➕"),
    (staff_work_order_assign, "staff_work_order_assign",
     "工单改派。何时调用: 员工说「把那张单派给刘工」「转给工程部小王」时。", "📌"),
    (staff_work_order_complete, "staff_work_order_complete",
     "完成工单, 维修/清洁单自动还原空房(在住房不动)。何时调用: 「修好了关单」时。", "✅"),
    (staff_work_order_list, "staff_work_order_list",
     "查询工单。何时调用: 员工问「今天还有哪些维修单」「刘工手上几张单」时。", "📄"),
    (staff_today_overview, "staff_today_overview",
     "今日经营概览(入住率/房态/在途工单/今日新增完成)。何时调用: 「今天酒店情况怎么样」"
     "「入住率多少」时; 晨会交接班快速数据源。", "📊"),
    (staff_request_create, "staff_request_create",
     "创建需求单(XQ)并自动生成工单。何时调用: 多意图场景如「客人要维修+清洁」时。", "📝"),
    (staff_request_list, "staff_request_list",
     "查询需求单列表。何时调用: 「有哪些未完成的需求」时。", "📑"),
    (staff_ticket_list, "staff_ticket_list",
     "查询Ticket工单列表(需求单拆分的子工单)。何时调用: 「我手上有哪些单」时。", "🎫"),
    (staff_ticket_assign, "staff_ticket_assign",
     "指派Ticket工单给员工或部门。何时调用: 前台调度派单时。", "🎯"),
    (staff_ticket_transition, "staff_ticket_transition",
     "Ticket状态流转(accept/in_progress/complete/cancel/escalate)。何时调用: 「接单」「开始维修」「完成」时。", "🔄"),
    (staff_pending_list, "staff_pending_list",
     "查询待确认操作队列。何时调用: 「有哪些要确认的」时。", "⏳"),
    (staff_pending_confirm, "staff_pending_confirm",
     "确认一条待确认操作(真正落地执行)。何时调用: 员工确认AI建议的操作时。", "✔️"),
    (staff_pending_reject, "staff_pending_reject",
     "拒绝一条待确认操作。何时调用: 员工认为AI建议不合理时。", "❌"),
    (staff_schedule_query, "staff_schedule_query",
     "查询排班表：今天/指定日期谁在岗。何时调用: 「今天谁上班」「工程部今天谁在」时。", "📅"),
    (staff_schedule_set, "staff_schedule_set",
     "设置排班。何时调用: 前台说「今天工程部Lee上班」「明天客房李逍遥当班」时。", "📋"),
]


def register_tools(app) -> int:
    """routes/__init__.py 的 register_all 阶段调用: 注册酒店业务智能体工具"""
    for fn, name, desc, icon in _TOOLS:
        app.tool(name, description=desc, icon=icon)(fn)
    return len(_TOOLS)
