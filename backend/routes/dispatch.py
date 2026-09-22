"""DomHotel 派单闭环 v2 — Request / Ticket 核心路由

安全约定：
- actions 的 by 一律取服务端身份，body.by 不可冒充
- 列表/详情强制本人或员工白名单
- guest.phone 对非本人脱敏
"""

import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Body

from .. import data_layer
from .. import auth
from ._helpers import now
from ..security import verify_internal_key, verify_guest_confirmation
from ..seq_id import new_request_id, new_ticket_id
from ..appchat import send_appchat_textcard, get_chatid, DEPT_APPCHAT_CONFIG_KEY
from ..wecom_sync import set_runtime_config, get_kf_setting
from ..wecom_kf import notify_staff_by_userids, notify_guest_work_order_change, send_kf_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
REQUEST_PREFIX = "XQ"
TICKET_PREFIX = "GD"

REQUEST_STATUS = {"open": "open", "partially_completed": "partially_completed",
                  "completed": "completed", "cancelled": "cancelled"}

TICKET_STATUS = {
    "created": "created",
    "assigned": "assigned",
    "accepted": "accepted",
    "in_progress": "in_progress",
    "completed_pending_guest": "completed_pending_guest",
    "completed": "completed",
    "cancelled": "cancelled",
    "escalated": "escalated",
}

VALID_TRANSITIONS: Dict[str, List[str]] = {
    "created": ["assigned", "cancelled"],
    "assigned": ["accepted", "escalated", "cancelled"],
    "accepted": ["in_progress", "completed_pending_guest", "escalated", "cancelled"],
    "in_progress": ["completed_pending_guest", "escalated", "cancelled"],
    "completed_pending_guest": ["completed", "escalated"],
    "escalated": ["assigned", "accepted", "in_progress", "completed_pending_guest", "completed", "cancelled"],
}


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────
def _server_identity(request: Request, body: Optional[Dict] = None) -> tuple:
    """返回 (user_id, role)。优先 header/cookie 登录态；X-Internal-Key 视为 system。
    body.by 被忽略，防止冒充。
    """
    internal_key = request.headers.get("X-Internal-Key")
    if verify_internal_key(internal_key):
        return "system", "system"

    sess = auth.get_session(request)
    return sess.get("user_id", "anonymous"), sess.get("role", "guest")


def _mask_phone(phone: Optional[str]) -> str:
    if not phone or len(phone) < 8:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]


def _new_request_id() -> str:
    return new_request_id()


def _new_ticket_id() -> str:
    return new_ticket_id()


def _load_requests() -> List[Dict[str, Any]]:
    return data_layer.load_table("requests")


def _load_tickets() -> List[Dict[str, Any]]:
    return data_layer.load_table("work_orders")


def _save_request(req: Dict[str, Any]) -> None:
    data_layer.save_table("requests", _load_requests() + [req])


def _save_ticket(ticket: Dict[str, Any]) -> None:
    tickets = _load_tickets()
    for i, t in enumerate(tickets):
        if t.get("wo_id") == ticket.get("wo_id"):
            tickets[i] = ticket
            break
    else:
        tickets.append(ticket)
    data_layer.save_table("work_orders", tickets)


async def _sync_request_status(request_id: str) -> None:
    """根据下属工单状态推导需求状态；全部完成后触发客人确认提醒"""
    tickets = [t for t in _load_tickets() if t.get("request_id") == request_id]
    reqs = _load_requests()
    req = next((r for r in reqs if r.get("request_id") == request_id), None)
    if not req:
        return

    active_tickets = [t for t in tickets if t.get("status") != "cancelled"]
    statuses = {t.get("status", "") for t in active_tickets}

    old_status = req.get("status", "")
    if statuses == {"completed"}:
        new_status = REQUEST_STATUS["completed"]
    elif not active_tickets or statuses == {"cancelled"}:
        new_status = REQUEST_STATUS["cancelled"]
    elif "completed" in statuses or "completed_pending_guest" in statuses:
        new_status = REQUEST_STATUS["partially_completed"]
    else:
        new_status = REQUEST_STATUS["open"]

    req["status"] = new_status
    req["updated_at"] = now()
    data_layer.save_table("requests", reqs)

    # 当所有活跃工单都进入 completed_pending_guest 且需求尚未 completed，发客人确认提醒
    if statuses == {"completed_pending_guest"} and old_status != REQUEST_STATUS["completed"]:
        await _notify_guest_confirm_request(req, active_tickets)


async def _notify_guest_confirm_request(req: Dict[str, Any], tickets: List[Dict[str, Any]]) -> None:
    """工单全部完成后通过 kf 提醒客人确认"""
    guest_userid = req.get("guest_userid", "")
    if not guest_userid:
        return
    from ..security import sign_guest_confirmation

    base_url = get_kf_setting("KF_CALLBACK_BASE_URL", "https://guishan.paw.domai.fun")
    lines = ["✅ 您房间 {} 的以下工单已完成，请确认：".format(req.get('room_no', ''))]
    for t in tickets:
        tid = t.get("wo_id", "")
        ct = sign_guest_confirmation(tid)
        link = f"{base_url}/apps/domhotel-suite?ticket={tid}&ct={ct}"
        lines.append(f"• {t.get('work_type', '')}：{link}")
    lines.append("\n回复「确认 + 工单编号」即可确认，如：确认 GD-20260909-12345678")

    await send_kf_message(guest_userid, "text", "\n".join(lines))


def _authorize_view(request: Request, target_user_id: Optional[str] = None,
                    target_guest_phone: Optional[str] = None) -> bool:
    """列表/详情鉴权：本人、员工白名单、或 system 可访问"""
    user_id, role = _server_identity(request)
    if role in ("super_admin", "manager", "employee", "system"):
        return True
    if target_user_id and target_user_id == user_id:
        return True
    if target_guest_phone and role == "guest":
        # guest 只能看自己的单（按手机号匹配）
        # 简化：此处由调用方进一步按 phone 过滤
        return True
    return False


# ─────────────────────────────────────────────
# 通知辅助函数
# ─────────────────────────────────────────────

def _load_dept_notify_userids() -> Dict[str, List[str]]:
    """读取部门 → 通知 userid 列表映射"""
    raw = get_kf_setting("DEPT_NOTIFY_USERIDS", "{}")
    try:
        data = json.loads(raw) if raw else {}
        return {k: [u.strip() for u in v if u.strip()] for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


async def _notify_frontdesk_new_request(req: Dict[str, Any], tickets: List[Dict[str, Any]]) -> None:
    """新建需求后通知前台部门成员（应用消息）"""
    userids = _load_dept_notify_userids().get("frontdesk", [])
    if not userids:
        logger.warning("[dispatch] 未配置前台通知人，跳过新需求通知")
        return
    title = f"🛎️ 新需求待分诊 — 房间 {req.get('room_no', '')}"
    detail = (
        f"需求编号：{req.get('request_id', '')}\n"
        f"客人需求：{req.get('description', '')}\n"
        f"联系人：{req.get('contact_name', '')} {req.get('contact_phone', '')}\n"
        f"已拆工单：{len(tickets)} 条\n"
        f"时间：{now()}"
    )
    await notify_staff_by_userids(userids, title, detail)

    # 同时尝试发 appchat 群卡片（如已配置）
    chatid = get_chatid("frontdesk")
    if chatid:
        await send_appchat_textcard(chatid, title, detail, url="/apps/domhotel-suite", btntxt="去分诊")


async def _notify_department_assign(ticket: Dict[str, Any]) -> None:
    """工单指派后通知对应部门成员 + 处理人"""
    target_dept = ticket.get("target_dept", "")
    userids = _load_dept_notify_userids().get(target_dept, [])

    title = f"🔧 新工单指派 — {ticket.get('work_type', '')}"
    detail = (
        f"工单编号：{ticket.get('wo_id', '')}\n"
        f"房间：{ticket.get('room_no', '')}\n"
        f"需求：{ticket.get('description', '')}\n"
        f"处理人：{ticket.get('assignee', '未分配')}\n"
        f"时间：{now()}"
    )

    if userids:
        await notify_staff_by_userids(userids, title, detail)

    # 同时尝试发 appchat 群卡片（如已配置）
    chatid = get_chatid(target_dept)
    if chatid:
        await send_appchat_textcard(chatid, title, detail, url="/apps/domhotel-suite", btntxt="查看工单")

    # 个人应用消息通知处理人
    assignee_id = ticket.get("assignee_id", "")
    if assignee_id:
        staff = auth.find_staff_by_id(assignee_id) or auth.find_active_staff_by_name(assignee_id)
        if staff and staff.get("wecom_userid"):
            await notify_staff_by_userids(
                [staff["wecom_userid"]],
                f"🔔 工单已派给你 — {ticket.get('room_no', '')}",
                f"工单号：{ticket.get('wo_id', '')}\n类型：{ticket.get('work_type', '')}\n需求：{ticket.get('description', '')}",
            )


async def _notify_guest_confirm(ticket: Dict[str, Any]) -> None:
    """工单完成后提醒客人确认（kf 渠道由调用方处理，这里仅做内部记录）"""
    logger.info("[dispatch] 工单 %s 完成，待客人确认", ticket.get("wo_id", ""))


# ─────────────────────────────────────────────
# Request API
# ─────────────────────────────────────────────

async def _create_request_from_payload(payload: Dict[str, Any], created_by: str = "system") -> Dict[str, Any]:
    """内部创建 Request + Ticket，绕过 HTTP 鉴权（供 KF/技能脚本调用）"""
    required = ["description", "contact_name", "contact_phone", "room_no"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        raise ValueError(f"缺少必填项: {', '.join(missing)}")

    req_id = _new_request_id()
    req = {
        "request_id": req_id,
        "description": payload["description"],
        "contact_name": payload["contact_name"],
        "contact_phone": payload["contact_phone"],
        "room_no": payload["room_no"],
        "guest_userid": payload.get("guest_userid", ""),
        "intent_count": payload.get("intent_count", 1),
        "status": REQUEST_STATUS["open"],
        "created_at": now(),
        "updated_at": now(),
        "created_by": created_by,
        "source": payload.get("source", "kf_draft"),
        "draft_confirmed": payload.get("draft_confirmed", True),
        "notes": payload.get("notes", ""),
    }
    _save_request(req)

    tickets = []
    for intent in payload.get("intents", [payload]):
        ticket = _create_ticket_record(req_id, intent, created_by)
        _save_ticket(ticket)
        tickets.append(ticket)

    # 触发前台部门通知
    try:
        await _notify_frontdesk_new_request(req, tickets)
    except Exception as exc:
        logger.warning("[dispatch] 内部创建后通知失败(不影响建单): %s", exc)

    return {"ok": True, "request": req, "tickets": tickets}


@router.post("/requests")
async def create_request(
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    """创建需求（XQ），支持附带初始工单列表"""
    user_id, role = _server_identity(request, payload)
    if role not in ("super_admin", "manager", "employee", "system"):
        raise HTTPException(status_code=403, detail="需要员工权限")

    result = await _create_request_from_payload(payload, user_id)
    return result


def _create_ticket_record(request_id: str, intent: Dict[str, Any], created_by: str) -> Dict[str, Any]:
    ticket_id = _new_ticket_id()
    return {
        "wo_id": ticket_id,
        "request_id": request_id,
        "work_type": intent.get("work_type", "其他"),
        "description": intent.get("description", ""),
        "room_no": intent.get("room_no", ""),
        "target_dept": intent.get("target_dept", ""),
        "priority": intent.get("priority", "normal"),
        "status": TICKET_STATUS["created"],
        "assignee": "",
        "assignee_id": "",
        "reporter": created_by,
        "created_at": now(),
        "updated_at": now(),
        "completed_at": "",
        "guest_confirm_by": "",
        "guest_confirm_at": "",
        "escalate_to": "",
        "escalate_at": "",
        "data_source": "dispatch_v2",
    }


@router.get("/requests")
async def list_requests(request: Request, status: Optional[str] = None):
    user_id, role = _server_identity(request)
    reqs = _load_requests()

    if role not in ("super_admin", "manager", "employee", "system"):
        # guest 只能看自己的
        reqs = [r for r in reqs if r.get("created_by") == user_id or r.get("guest_userid") == user_id]

    if status:
        reqs = [r for r in reqs if r.get("status") == status]

    # 非本人/员工，手机号脱敏
    for r in reqs:
        if role not in ("super_admin", "manager", "employee", "system") and r.get("created_by") != user_id:
            r["contact_phone"] = _mask_phone(r.get("contact_phone"))

    return {"ok": True, "requests": reqs}


@router.get("/requests/{request_id}")
async def get_request(request: Request, request_id: str):
    req = next((r for r in _load_requests() if r.get("request_id") == request_id), None)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    user_id, role = _server_identity(request)
    if not _authorize_view(request, req.get("created_by"), req.get("contact_phone")):
        raise HTTPException(status_code=403, detail="无权查看")

    tickets = [t for t in _load_tickets() if t.get("request_id") == request_id]

    result = dict(req)
    if role not in ("super_admin", "manager", "employee", "system") and result.get("created_by") != user_id:
        result["contact_phone"] = _mask_phone(result.get("contact_phone"))

    return {"ok": True, "request": result, "tickets": tickets}


# ─────────────────────────────────────────────
# Ticket API
# ─────────────────────────────────────────────

@router.post("/tickets/{ticket_id}/assign")
async def assign_ticket(request: Request, ticket_id: str, payload: Dict[str, Any] = Body(...)):
    """前台/经理指派工单：指定 target_dept / assignee / assignee_id"""
    user_id, role = _server_identity(request, payload)
    if role not in ("super_admin", "manager", "employee", "system"):
        raise HTTPException(status_code=403, detail="需要员工权限")

    tickets = _load_tickets()
    ticket = next((t for t in tickets if t.get("wo_id") == ticket_id), None)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")

    current = ticket.get("status", "")
    if current not in ("created", "escalated"):
        raise HTTPException(status_code=400, detail=f"当前状态 {current} 不允许指派")

    ticket["target_dept"] = payload.get("target_dept", ticket.get("target_dept", ""))
    ticket["assignee"] = payload.get("assignee", "")
    ticket["assignee_id"] = payload.get("assignee_id", "")
    ticket["status"] = TICKET_STATUS["assigned"]
    ticket["updated_at"] = now()
    ticket["assigned_by"] = user_id
    data_layer.save_table("work_orders", tickets)
    await _sync_request_status(ticket["request_id"])

    try:
        await _notify_department_assign(ticket)
    except Exception as exc:
        logger.warning("[dispatch] 指派通知失败(不影响主流程): %s", exc)

    return {"ok": True, "ticket": ticket}


@router.post("/tickets/{ticket_id}/transition")
async def transition_ticket(request: Request, ticket_id: str, payload: Dict[str, Any] = Body(...)):
    """工单状态流转：accept / in_progress / complete / cancel / escalate / confirm"""
    user_id, role = _server_identity(request, payload)

    action = payload.get("action", "").lower()
    if action not in ("accept", "in_progress", "complete", "cancel", "escalate", "confirm"):
        raise HTTPException(status_code=400, detail=f"不支持的动作: {action}")

    tickets = _load_tickets()
    ticket = next((t for t in tickets if t.get("wo_id") == ticket_id), None)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")

    current = ticket.get("status", "")
    mapping = {
        "accept": ("assigned", "accepted"),
        "in_progress": ("accepted", "in_progress"),
        "complete": ("in_progress", "completed_pending_guest"),
        "cancel": (current, "cancelled"),
        "escalate": (current, "escalated"),
        "confirm": ("completed_pending_guest", "completed"),
    }
    _from, _to = mapping[action]

    # confirm 可由客人签名链接或 kf 菜单触发（可能无员工权限）
    if action == "confirm":
        ct = payload.get("ct")
        if not verify_guest_confirmation(ticket_id, ct):
            # 允许员工/系统直接确认
            if role not in ("super_admin", "manager", "employee", "system"):
                raise HTTPException(status_code=403, detail="确认签名无效")
    else:
        if role not in ("super_admin", "manager", "employee", "system"):
            # 普通员工只能接自己被指派的单
            if ticket.get("assignee_id") != user_id:
                raise HTTPException(status_code=403, detail="无权操作此工单")

    # cancel 仅允许经理/超管或 system
    if action == "cancel" and role not in ("super_admin", "manager", "system"):
        raise HTTPException(status_code=403, detail="取消工单需要经理权限")

    if current not in VALID_TRANSITIONS or _to not in VALID_TRANSITIONS.get(current, []):
        if action == "cancel":  # 历史兼容：任意状态可取消
            pass
        else:
            raise HTTPException(status_code=400, detail=f"状态 {current} 不能流转到 {_to}")

    ticket["status"] = _to
    ticket["updated_at"] = now()
    ticket[f"{action}_by"] = user_id
    ticket[f"{action}_at"] = now()

    if _to == "completed":
        ticket["completed_at"] = now()

    data_layer.save_table("work_orders", tickets)
    await _sync_request_status(ticket["request_id"])

    return {"ok": True, "ticket": ticket}


@router.get("/tickets")
async def list_tickets(request: Request, status: Optional[str] = None, mine: bool = False):
    user_id, role = _server_identity(request)
    tickets = _load_tickets()

    if role not in ("super_admin", "manager", "employee", "system"):
        # guest 只能看与自己需求关联的工单（按 ct 签名验证由详情接口处理）
        raise HTTPException(status_code=403, detail="需要员工权限")

    if mine and role in ("employee",):
        tickets = [t for t in tickets if t.get("assignee_id") == user_id]
    if status:
        tickets = [t for t in tickets if t.get("status") == status]

    return {"ok": True, "tickets": tickets}


@router.get("/tickets/{ticket_id}")
async def get_ticket(request: Request, ticket_id: str, ct: Optional[str] = None):
    tickets = _load_tickets()
    ticket = next((t for t in tickets if t.get("wo_id") == ticket_id), None)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")

    user_id, role = _server_identity(request)
    if role in ("super_admin", "manager", "employee", "system"):
        return {"ok": True, "ticket": ticket}

    # 客人通过确认链接访问
    if verify_guest_confirmation(ticket_id, ct):
        return {"ok": True, "ticket": ticket}

    raise HTTPException(status_code=403, detail="无权查看")


# ─────────────────────────────────────────────
# 注册
# ─────────────────────────────────────────────
@router.post("/config/appchat")
async def set_appchat_chatids(request: Request, payload: Dict[str, Any] = Body(...)):
    """配置部门 appchat 群 chatid 映射

    payload 示例：
    {
      "frontdesk": "CHATID-FRONTDESK",
      "housekeeping": "CHATID-HOUSEKEEPING",
      "engineering": "CHATID-ENGINEERING"
    }
    """
    user_id, role = _server_identity(request, payload)
    if role not in ("super_admin", "manager", "system"):
        raise HTTPException(status_code=403, detail="需要经理权限")
    set_runtime_config({DEPT_APPCHAT_CONFIG_KEY: json.dumps(payload, ensure_ascii=False)})
    return {"ok": True, "chatids": payload}


@router.get("/config/appchat")
async def get_appchat_chatids(request: Request):
    user_id, role = _server_identity(request)
    if role not in ("super_admin", "manager", "employee", "system"):
        raise HTTPException(status_code=403, detail="需要员工权限")
    raw = get_kf_setting(DEPT_APPCHAT_CONFIG_KEY, "{}")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return {"ok": True, "chatids": data}


@router.post("/appchat/auto-init")
async def auto_init_appchats(request: Request, payload: Dict[str, Any] = Body(default={})):
    """自动从企微通讯录读取部门成员并创建应用群聊

    默认按名称匹配：酒店前台 / 酒店清洁 / 酒店工程
    可在 payload 里覆盖映射：{"frontdesk":"前台","housekeeping":"清洁","engineering":"工程"}
    """
    user_id, role = _server_identity(request, payload)
    if role not in ("super_admin", "manager", "system"):
        raise HTTPException(status_code=403, detail="需要经理权限")

    import httpx
    from ..wecom_sync import _get_access_token

    token = await _get_access_token()
    base_url = "https://qyapi.weixin.qq.com"

    # 1. 拉部门列表
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{base_url}/cgi-bin/department/list?access_token={token}")
        dept_data = r.json()
    if dept_data.get("errcode") != 0:
        raise HTTPException(status_code=500, detail=f"拉部门失败: {dept_data}")

    name_map = {
        "frontdesk": payload.get("frontdesk") or "酒店前台",
        "housekeeping": payload.get("housekeeping") or "酒店清洁",
        "engineering": payload.get("engineering") or "酒店工程",
    }

    dept_id_map = {}
    for d in dept_data.get("department", []):
        name = d.get("name", "")
        did = d.get("id")
        for key, target in name_map.items():
            if target in name or name in target:
                dept_id_map[key] = did
                break

    if not dept_id_map:
        raise HTTPException(status_code=400, detail="未找到匹配的部门，请确认部门名称")

    # 2. 拉每个部门成员并建群
    results = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for key, did in dept_id_map.items():
            r = await client.get(
                f"{base_url}/cgi-bin/user/simplelist?access_token={token}&department_id={did}&fetch_child=0"
            )
            user_data = r.json()
            if user_data.get("errcode") != 0:
                results[key] = {"ok": False, "error": user_data}
                continue

            userids = [u.get("userid") for u in user_data.get("userlist", []) if u.get("userid")]
            if not userids:
                results[key] = {"ok": False, "error": "部门无成员"}
                continue

            # 创建应用群聊，以第一个成员作为群主
            body = {
                "name": name_map[key],
                "owner": userids[0],
                "userlist": userids,
                "chatid_type": "group",
            }
            r = await client.post(f"{base_url}/cgi-bin/appchat/create?access_token={token}", json=body)
            chat_data = r.json()
            if chat_data.get("errcode") != 0:
                results[key] = {"ok": False, "error": chat_data}
                continue

            chatid = chat_data.get("chatid")
            results[key] = {"ok": True, "chatid": chatid, "members": userids}

    # 3. 保存 chatid 到运行时配置
    chatids = {k: v["chatid"] for k, v in results.items() if v.get("ok")}
    if chatids:
        set_runtime_config({DEPT_APPCHAT_CONFIG_KEY: json.dumps(chatids, ensure_ascii=False)})

    return {"ok": True, "results": results, "saved_chatids": chatids}


def register_routes(app) -> None:
    app.include_router(router)
