# -*- coding: utf-8 -*-
"""v3 兼容桥接路由 — 旧 API 端点映射到 v3data

前端代码不动，这些端点保持旧的 URL 和数据格式，
内部全部转发到 v3data 层。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_compat():
    """延迟导入 v3compat 模块"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import v3compat
    return v3compat


# ============================================================================
# 房态
# ============================================================================

@router.get("/rooms")
async def list_rooms():
    """列出所有房间"""
    compat = _get_compat()
    rooms = compat.load_rooms()
    return {"ok": True, "rooms": rooms, "count": len(rooms)}


@router.get("/rooms/{room_no}")
async def get_room(room_no: str):
    """获取单个房间详情"""
    compat = _get_compat()
    room = compat.get_room(room_no)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    
    # 获取关联工单
    from v3data import get_room_full_status
    status = get_room_full_status(room_no)
    room["work_orders"] = status.get("open_work_orders", [])
    
    return {"ok": True, "room": room}


# ============================================================================
# 工单
# ============================================================================

@router.get("/work_orders")
async def list_work_orders(status: Optional[str] = None):
    """列出工单"""
    compat = _get_compat()
    orders = compat.load_work_orders(status)
    return {"ok": True, "work_orders": orders, "count": len(orders)}


@router.get("/work_orders/{wo_id}")
async def get_work_order(wo_id: str):
    """获取工单详情"""
    compat = _get_compat()
    order = compat.get_work_order(wo_id)
    if not order:
        raise HTTPException(status_code=404, detail="work order not found")
    return {"ok": True, "work_order": order}


# ============================================================================
# 待确认
# ============================================================================

@router.get("/pending")
async def list_pending(
    assigned_to: Optional[str] = None,
    status: Optional[str] = None
):
    """列出待确认动作"""
    compat = _get_compat()
    actions = compat.load_pending_actions(assigned_to, status)
    return {"ok": True, "pending_actions": actions, "count": len(actions)}


@router.get("/pending/stats")
async def pending_stats():
    """待确认统计"""
    compat = _get_compat()
    stats = compat.load_stats()
    return {"ok": True, "stats": stats}


# ============================================================================
# 统计
# ============================================================================

@router.get("/stats")
async def get_stats():
    """获取统计信息"""
    compat = _get_compat()
    stats = compat.load_stats()
    return {"ok": True, **stats}


# ============================================================================
# 工单状态转换 (兼容旧接口)
# ============================================================================

@router.post("/work_orders/{wo_id}/transition")
async def transition_work_order(wo_id: str, body: Dict[str, Any]):
    """工单状态转换"""
    from v3data import transition_work_order
    new_status = body.get("new_status") or body.get("status")
    operator = body.get("operator", "system")
    
    if not new_status:
        raise HTTPException(status_code=400, detail="new_status required")
    
    result = transition_work_order(wo_id, new_status, operator)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "transition failed"))
    
    return result


def register_routes(app) -> None:
    """注册路由"""
    pass  # 路由已在模块级定义
