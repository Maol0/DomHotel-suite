# -*- coding: utf-8 -*-
"""v3 兼容层 — 把旧 API 接口映射到 v3data

前端代码不动，后端把 /rooms, /work_orders, /pending 等旧端点
转发到 v3data 的 entities/events/relations 查询。
"""
from __future__ import annotations
import sys, os
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3data import (
    list_entities, get_entity, create_entity, update_entity,
    list_events, get_event, create_event, update_event,
    list_relations, create_relation,
    get_room_full_status, get_work_order_detail,
    transition_work_order, _now
)


# ============================================================================
# 旧接口兼容
# ============================================================================

def load_rooms() -> List[Dict[str, Any]]:
    """兼容旧 /rooms 接口"""
    rooms = list_entities("room")
    return [_entity_to_room(e) for e in rooms]


def get_room(room_no: str) -> Optional[Dict[str, Any]]:
    """兼容旧 /rooms/{room_no}"""
    normalized = str(int(room_no)) if room_no.isdigit() else room_no
    entity = get_entity(f"room-{normalized}")
    if not entity:
        return None
    return _entity_to_room(entity)


def load_work_orders(status: str = None) -> List[Dict[str, Any]]:
    """兼容旧 /work_orders"""
    events = list_events("work_order", status=status)
    return [_event_to_wo(e) for e in events]


def get_work_order(wo_id: str) -> Optional[Dict[str, Any]]:
    """兼容旧 /work_orders/{wo_id}"""
    detail = get_work_order_detail(wo_id)
    if not detail:
        return None
    return _detail_to_wo(detail)


def load_pending_actions(assigned_to: str = None, status: str = None) -> List[Dict[str, Any]]:
    """兼容旧 /pending"""
    filters = {"status": status or "pending"}
    events = list_events("work_order", **filters)
    # 过滤 pending_confirm 状态的工单
    pending = [e for e in events if e.get("status") == "pending_confirm"]
    if assigned_to:
        pending = [e for e in pending if e.get("data", {}).get("assignee") == assigned_to]
    return [_event_to_pending(e) for e in pending]


def load_stats() -> Dict[str, Any]:
    """兼容旧 /stats"""
    rooms = list_entities("room")
    events = list_events("work_order")
    
    room_stats = {}
    for r in rooms:
        status = r.get("data", {}).get("status", "未知")
        room_stats[status] = room_stats.get(status, 0) + 1
    
    wo_stats = {}
    for e in events:
        status = e.get("status", "未知")
        wo_stats[status] = wo_stats.get(status, 0) + 1
    
    return {
        "total_rooms": len(rooms),
        "room_stats": room_stats,
        "total_work_orders": len(events),
        "work_order_stats": wo_stats,
        "pending_actions": len([e for e in events if e.get("status") == "pending_confirm"])
    }


# ============================================================================
# 数据格式转换
# ============================================================================

def _entity_to_room(entity: Dict[str, Any]) -> Dict[str, Any]:
    """v3 entity → 旧 room 格式"""
    data = entity.get("data", {})
    return {
        "room_no": data.get("room_no", entity.get("entity_id", "").replace("room-", "")),
        "floor": data.get("floor"),
        "room_type": data.get("room_type"),
        "status": data.get("status"),
        "guest_name": data.get("guest_name"),
        "guest_phone": data.get("guest_phone"),
        "checkin_time": data.get("checkin_time"),
        "checkin_days": data.get("checkin_days"),
        "notes": data.get("notes"),
        "created_at": entity.get("created_at"),
        "updated_at": entity.get("updated_at"),
    }


def _event_to_wo(event: Dict[str, Any]) -> Dict[str, Any]:
    """v3 event → 旧 work_order 格式"""
    data = event.get("data", {})
    return {
        "wo_id": data.get("wo_id"),
        "room_no": data.get("room_no"),
        "work_type": data.get("work_type"),
        "status": event.get("status"),
        "assignee": data.get("assignee"),
        "description": data.get("description"),
        "priority": data.get("priority"),
        "target_dept": data.get("target_dept"),
        "created_at": event.get("created_at"),
        "updated_at": event.get("updated_at"),
        "history": event.get("history", []),
    }


def _detail_to_wo(detail: Dict[str, Any]) -> Dict[str, Any]:
    """v3 detail → 旧 work_order 格式"""
    event = detail.get("event", {})
    data = event.get("data", {})
    return {
        "wo_id": data.get("wo_id"),
        "room_no": detail.get("room_no"),
        "work_type": data.get("work_type"),
        "status": event.get("status"),
        "assignee": data.get("assignee"),
        "assignee_name": detail.get("assignee_name"),
        "description": data.get("description"),
        "priority": data.get("priority"),
        "target_dept": data.get("target_dept"),
        "room_status": detail.get("room_status"),
        "created_at": event.get("created_at"),
        "updated_at": event.get("updated_at"),
        "history": detail.get("history", []),
    }


def _event_to_pending(event: Dict[str, Any]) -> Dict[str, Any]:
    """v3 event → 旧 pending_action 格式"""
    data = event.get("data", {})
    return {
        "id": event.get("event_id"),
        "action_type": "assign",
        "status": event.get("status"),
        "target_record": {
            "room_no": data.get("room_no"),
            "wo_id": data.get("wo_id"),
        },
        "assigned_to": data.get("assignee"),
        "description": f"{data.get('work_type')}工单 - {data.get('description', '')}",
        "created_at": event.get("created_at"),
        "updated_at": event.get("updated_at"),
    }
