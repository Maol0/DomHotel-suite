# -*- coding: utf-8 -*-
"""v3 桥接层 — 把旧API接口映射到v3data

前端代码不动，后端把 /rooms, /work_orders, /pending 等旧端点
转发到 v3data 的 entities/events/relations 查询。
"""
from __future__ import annotations
import sys
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# 添加 v3data 到路径
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3data as v3

# ============================================================================
# 房间
# ============================================================================

def load_rooms() -> List[Dict[str, Any]]:
    """兼容旧 /rooms 接口"""
    rooms = v3.list_entities("room")
    return [_entity_to_room(e) for e in rooms]


def get_room(room_no: str) -> Optional[Dict[str, Any]]:
    """兼容旧 /rooms/{room_no}"""
    normalized = str(int(room_no)) if room_no.isdigit() else room_no
    entity = v3.get_entity(f"room-{normalized}")
    if not entity:
        return None
    return _entity_to_room(entity)


# ============================================================================
# 工单
# ============================================================================

def load_work_orders(status: str = None) -> List[Dict[str, Any]]:
    """兼容旧 /work_orders"""
    events = v3.list_events("work_order", status=status)
    return [_event_to_wo(e) for e in events]


def get_work_order(wo_id: str) -> Optional[Dict[str, Any]]:
    """兼容旧 /work_orders/{wo_id}"""
    detail = v3.get_work_order_detail(wo_id)
    if not detail:
        return None
    return _detail_to_wo(detail)


# ============================================================================
# 待确认
# ============================================================================

def load_pending_actions(assigned_to: str = None, status: str = None) -> List[Dict[str, Any]]:
    """兼容旧 /pending"""
    events = v3.list_events("work_order")
    pending = [e for e in events if e.get("status") == "pending_confirm"]
    if assigned_to:
        pending = [e for e in pending if e.get("data", {}).get("assignee") == assigned_to]
    return [_event_to_pending(e) for e in pending]


# ============================================================================
# 员工
# ============================================================================

def load_staff() -> List[Dict[str, Any]]:
    """兼容旧 /admin/staff"""
    staff = v3.list_entities("staff")
    return [_entity_to_staff(e) for e in staff]


def get_staff(staff_id: str) -> Optional[Dict[str, Any]]:
    """获取单个员工"""
    entity = v3.get_entity(staff_id)
    if not entity:
        return None
    return _entity_to_staff(entity)


# ============================================================================
# 部门
# ============================================================================

def load_departments() -> List[Dict[str, Any]]:
    """兼容旧 /admin/departments"""
    depts = v3.list_entities("department")
    return [_entity_to_dept(e) for e in depts]


# ============================================================================
# 统计
# ============================================================================

def load_stats() -> Dict[str, Any]:
    """兼容旧 /stats"""
    rooms = v3.list_entities("room")
    events = v3.list_events("work_order")
    
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


def _entity_to_staff(entity: Dict[str, Any]) -> Dict[str, Any]:
    """v3 entity → 旧 staff 格式"""
    data = entity.get("data", {})
    return {
        "staff_id": entity.get("entity_id"),
        "name": data.get("name"),
        "phone": data.get("phone"),
        "department_id": data.get("department_id"),
        "role": data.get("role"),
    }


def _entity_to_dept(entity: Dict[str, Any]) -> Dict[str, Any]:
    """v3 entity → 旧 department 格式"""
    data = entity.get("data", {})
    return {
        "dept_id": entity.get("entity_id"),
        "name": data.get("name"),
        "desc": data.get("desc"),
    }


# ============================================================================
# 兼容旧接口：load_table / save_table
# ============================================================================

def load_table(table: str) -> List[Dict[str, Any]]:
    """兼容旧 data_layer.load_table 接口"""
    if table == "rooms":
        return load_rooms()
    elif table == "work_orders":
        return load_work_orders()
    elif table == "staff":
        return load_staff()
    elif table == "departments":
        return load_departments()
    elif table == "pending_actions":
        return load_pending_actions()
    else:
        # 其他表返回空列表
        return []


def save_table(table: str, rows: List[Dict[str, Any]]) -> None:
    """兼容旧 data_layer.save_table 接口（暂不实现）"""
    pass
