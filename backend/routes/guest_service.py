"""客人自助服务 API

提供客人通过 H5 页面提交服务请求的接口。
客人通过企微客服链接进入，URL 带 kf_id 参数识别身份。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from .. import data_layer
from .. import wecom_kf
from ..wecom_kf import find_customer_by_external_userid, find_active_customer_by_external_userid
from ._helpers import now, create_work_order
from .work_order_svc import svc_create_work_order

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest", tags=["guest-service"])


def _guess_work_type(service_type: str, description: str) -> str:
    """根据服务类型和描述推断工单类型"""
    if service_type == "repair":
        return "维修"
    elif service_type == "delivery":
        return "送物"
    elif service_type == "consult":
        return "其他"
    else:
        return "其他"


def _guess_dept(service_type: str, description: str) -> str:
    """根据服务类型推断目标部门"""
    if service_type == "repair":
        return "engineering"
    elif service_type == "delivery":
        return "housekeeping"
    else:
        return "frontdesk"


@router.get("/room")
async def get_guest_room(kf_id: str = Query(..., description="客人企微客服 ID")):
    """获取客人房间信息
    
    通过 kf_id（企微客服 external_userid）查找客人绑定的房间信息。
    """
    if not kf_id:
        raise HTTPException(400, "kf_id 不能为空")

    customer = find_active_customer_by_external_userid(kf_id)
    if not customer:
        # 检查是否是回访客人（有历史但已退房）
        hist = find_customer_by_external_userid(kf_id)
        if hist and hist.get("deleted"):
            return {
                "ok": False,
                "returning": True,
                "error": "您已退房，欢迎回来！请在客服中发送「绑定 <房间号> <姓名>」重新绑定房间。"
            }
        return {
            "ok": False,
            "error": "未找到客人信息，请先在企微客服中绑定房间"
        }

    room_no = customer.get("room_no", "")
    if not room_no:
        return {
            "ok": False,
            "error": "未绑定房间，请先在企微客服中绑定房间"
        }

    # 查询房间信息
    rooms = data_layer.load_table("rooms")
    room = next((r for r in rooms if r.get("room_no") == room_no), None)
    
    if not room:
        return {
            "ok": False,
            "error": f"房间 {room_no} 不存在"
        }

    # 查询在途工单
    work_orders = data_layer.load_table("work_orders")
    active_orders = [
        wo for wo in work_orders
        if wo.get("room_no") == room_no
        and wo.get("status") not in ("done", "rejected")
    ]

    return {
        "ok": True,
        "room": {
            "room_no": room_no,
            "room_type": room.get("room_type", ""),
            "floor": room.get("floor", ""),
            "status": room.get("status", "空房"),
            "guest_name": customer.get("guest_name", ""),
            "active_orders": len(active_orders)
        }
    }


@router.post("/service")
async def submit_guest_service(
    payload: Dict[str, Any] = Body(...)
):
    """提交客人服务请求
    
    客人通过 H5 页面提交服务请求（报修/送物/咨询等）。
    
    Body:
        kf_id: 客人企微客服 ID（必填）
        service_type: 服务类型 repair/delivery/consult（必填）
        description: 需求描述（必填）
        urgency: 紧急程度 normal/urgent（可选，默认 normal）
        delivery_time: 送物时间 asap/morning/afternoon（可选）
    """
    kf_id = payload.get("kf_id", "").strip()
    service_type = payload.get("service_type", "").strip()
    description = payload.get("description", "").strip()
    urgency = payload.get("urgency", "normal").strip()
    delivery_time = payload.get("delivery_time", "").strip()

    # 参数校验
    if not kf_id:
        raise HTTPException(400, "kf_id 不能为空")
    if not service_type:
        raise HTTPException(400, "service_type 不能为空")
    if not description:
        raise HTTPException(400, "description 不能为空")

    # 查找客人
    customer = find_active_customer_by_external_userid(kf_id)
    if not customer:
        raise HTTPException(404, "未找到客人信息或已退房，请先在企微客服中绑定房间")

    room_no = customer.get("room_no", "")
    if not room_no:
        raise HTTPException(400, "未绑定房间，请先在企微客服中绑定房间")

    # 校验房间存在
    rooms = data_layer.load_table("rooms")
    room = next((r for r in rooms if r.get("room_no") == room_no), None)
    if not room:
        raise HTTPException(404, f"房间 {room_no} 不存在")

    # 构建工单描述
    full_description = description
    if service_type == "delivery" and delivery_time:
        time_map = {
            "asap": "尽快送达",
            "morning": "明天早上",
            "afternoon": "明天下午"
        }
        full_description += f"（送物时间：{time_map.get(delivery_time, delivery_time)}）"

    # 创建工单
    work_type = _guess_work_type(service_type, description)
    target_dept = "frontdesk"  # v2.4-intake: 客人请求统一前台收单
    priority = urgency if urgency in ("urgent", "high", "normal", "low") else "normal"

    # v1.6.1 统一收口: 委托 service (建单 + 派单 + 企微表格同步 + 员工卡片 + 客人回执)
    _openid = customer.get("openid", "")
    result = await svc_create_work_order(
        room_no=room_no,
        work_type=work_type,
        description=full_description,
        priority=priority,
        reporter=f"客人:{customer.get('guest_name', '')}",
        target_dept=target_dept,
        data_source="guest_h5",
        operator=f"guest:{kf_id}",
        extra=({"guest_id": _openid} if _openid else None),
        auto_dispatch=False,
    )
    wo = result.get("work_order", {})

    # 记录日志
    logger.info(
        "[guest-service] 客人服务请求已提交: kf_id=%s room_no=%s type=%s wo_id=%s",
        kf_id, room_no, service_type, wo.get("wo_id")
    )

    return {
        "ok": True,
        "message": "服务请求已提交",
        "order_id": wo.get("wo_id"),
        "room_no": room_no,
        "work_type": work_type,
        "target_dept": target_dept
    }


@router.get("/orders")
async def get_guest_orders(
    kf_id: str = Query(..., description="客人企微客服 ID"),
    status: Optional[str] = Query(None, description="工单状态过滤")
):
    """获取客人工单列表
    
    通过 kf_id 查找客人绑定房间的所有工单。
    """
    if not kf_id:
        raise HTTPException(400, "kf_id 不能为空")

    customer = find_customer_by_external_userid(kf_id)
    if not customer:
        return {
            "ok": False,
            "error": "未找到客人信息"
        }

    room_no = customer.get("room_no", "")
    openid = customer.get("openid", "")

    # 查询工单：优先用 openid（跨住次），回退到 room_no
    work_orders = data_layer.load_table("work_orders")
    if openid:
        orders = [wo for wo in work_orders if wo.get("guest_id") == openid]
    elif room_no:
        orders = [wo for wo in work_orders if wo.get("room_no") == room_no]
    else:
        orders = []

    # 按状态过滤
    if status:
        orders = [wo for wo in orders if wo.get("status") == status]

    # 按创建时间倒序
    orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return {
        "ok": True,
        "room_no": room_no,
        "total": len(orders),
        "orders": orders[:20]  # 最多返回20条
    }


@router.post("/kf/contact_way")
async def get_kf_contact_way(payload: Dict[str, Any] = Body(...)):
    """获取带场景值的客服链接
    
    Body:
        scene: 场景值（如 room_1206）
    """
    scene = payload.get("scene", "").strip()
    if not scene:
        raise HTTPException(400, "scene 不能为空")

    try:
        # 获取access_token（延迟导入避免循环依赖）
        from .. import wecom_sync
        token = await wecom_sync.get_access_token()
        
        # 调用企微API获取客服链接
        import httpx
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/add_contact_way?access_token={token}"
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json={
                "open_kfid": "wkeDH_JwAAysRCsnaAwO7PCdUq_gdyqw",
                "scene": scene
            })
            data = r.json()
        
        if data.get("errcode") == 0:
            return {
                "ok": True,
                "url": data.get("url"),
                "scene": scene
            }
        else:
            logger.warning("[kf] 获取客服链接失败: %s", data)
            return {
                "ok": False,
                "error": data.get("errmsg", "获取客服链接失败")
            }
    except Exception as e:
        logger.warning("[kf] 获取客服链接异常: %s", e)
        return {
            "ok": False,
            "error": str(e)
        }
