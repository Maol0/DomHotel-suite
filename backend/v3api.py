"""
酒店房务工作台 v3.0 - API 网关

统一 API 接口，所有前端请求通过这个路由
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import sys
import os

# 添加 v3data 到路径
sys.path.insert(0, os.path.dirname(__file__))
from v3data import (
    list_events, get_event, create_event, update_event,
    list_entities, get_entity, create_entity, update_entity,
    list_relations, create_relation,
    get_room_full_status, get_work_order_detail,
    transition_work_order, confirm_work_order, create_work_order_with_assign, _now
)
import v3wecom

router = APIRouter()


# ============================================================================
# 事件 API
# ============================================================================

class EventCreate(BaseModel):
    event_type: str
    subject_type: str
    subject_id: str
    status: str = "pending"
    data: Dict[str, Any] = {}
    operator: str = "system"
    source: str = "manual"


class EventUpdate(BaseModel):
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    operator: Optional[str] = None


@router.get("/api/events")
def api_list_events(
    event_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """查询事件流"""
    events = list_events(event_type, subject_id, status, limit)
    return {"ok": True, "events": events, "count": len(events)}


@router.post("/api/events")
def api_create_event(event: EventCreate):
    """创建事件"""
    new_event = create_event(
        event_type=event.event_type,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        status=event.status,
        data=event.data,
        operator=event.operator,
        source=event.source
    )
    return {"ok": True, "event": new_event}


@router.put("/api/events/{event_id}")
def api_update_event(event_id: int, update: EventUpdate):
    """更新事件"""
    kwargs = {}
    if update.status is not None:
        kwargs["status"] = update.status
    if update.data is not None:
        kwargs["data"] = update.data
    if update.operator is not None:
        kwargs["operator"] = update.operator
    
    updated = update_event(event_id, **kwargs)
    if not updated:
        raise HTTPException(status_code=404, detail="event not found")
    return {"ok": True, "event": updated}


# ============================================================================
# 工单状态机 API
# ============================================================================

class WorkOrderTransition(BaseModel):
    new_status: str
    operator: str = "system"
    note: str = ""


@router.post("/api/work-orders/{wo_id}/transition")
def api_transition_work_order(wo_id: str, transition: WorkOrderTransition):
    """工单状态转换"""
    result = transition_work_order(wo_id, transition.new_status, transition.operator)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "transition failed"))
    return result


class WorkOrderConfirm(BaseModel):
    confirmed_by: str
    assignee: Optional[str] = None


@router.post("/api/work-orders/{wo_id}/confirm")
def api_confirm_work_order(wo_id: str, confirm: WorkOrderConfirm):
    """确认待确认的工单"""
    result = confirm_work_order(wo_id, confirm.confirmed_by, confirm.assignee)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "confirm failed"))
    return result


class WorkOrderCreate(BaseModel):
    room_no: str
    work_type: str
    description: str
    priority: str = "normal"
    target_dept: Optional[str] = None
    operator: str = "system"
    source: str = "manual"


@router.post("/api/work-orders")
def api_create_work_order(wo: WorkOrderCreate):
    """创建工单并自动派单"""
    result = create_work_order_with_assign(
        room_no=wo.room_no,
        work_type=wo.work_type,
        description=wo.description,
        priority=wo.priority,
        target_dept=wo.target_dept,
        operator=wo.operator,
        source=wo.source
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "create failed"))
    return result


# ============================================================================
# 实体 API
# ============================================================================

class EntityCreate(BaseModel):
    entity_type: str
    data: Dict[str, Any]
    entity_id: Optional[str] = None


class EntityUpdate(BaseModel):
    data: Dict[str, Any]


@router.get("/api/entities/{entity_type}")
def api_list_entities(entity_type: str):
    """查询实体列表"""
    entities = list_entities(entity_type)
    return {"ok": True, "entities": entities, "count": len(entities)}


@router.post("/api/entities")
def api_create_entity(entity: EntityCreate):
    """创建实体"""
    new_entity = create_entity(entity.entity_type, entity.data, entity.entity_id)
    return {"ok": True, "entity": new_entity}


@router.put("/api/entities/{entity_id}")
def api_update_entity(entity_id: str, update: EntityUpdate):
    """更新实体"""
    updated = update_entity(entity_id, **update.data)
    if not updated:
        raise HTTPException(status_code=404, detail="entity not found")
    return {"ok": True, "entity": updated}


# ============================================================================
# 关联 API
# ============================================================================

class RelationCreate(BaseModel):
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relation_type: str
    data: Dict[str, Any] = {}


@router.get("/api/relations")
def api_list_relations(
    from_type: Optional[str] = None,
    from_id: Optional[str] = None,
    to_type: Optional[str] = None,
    to_id: Optional[str] = None,
    relation_type: Optional[str] = None
):
    """查询关联"""
    relations = list_relations(from_type, from_id, to_type, to_id, relation_type)
    return {"ok": True, "relations": relations, "count": len(relations)}


@router.post("/api/relations")
def api_create_relation(relation: RelationCreate):
    """创建关联"""
    new_relation = create_relation(
        relation.from_type, relation.from_id,
        relation.to_type, relation.to_id,
        relation.relation_type, relation.data
    )
    return {"ok": True, "relation": new_relation}


# ============================================================================
# 跨表查询 API
# ============================================================================

@router.get("/api/query/room-status/{room_no}")
def api_get_room_status(room_no: str):
    """获取房间完整状态"""
    status = get_room_full_status(room_no)
    return {"ok": True, "data": status}


@router.get("/api/query/work-order-detail/{wo_id}")
def api_get_work_order_detail(wo_id: str):
    """获取工单详情"""
    detail = get_work_order_detail(wo_id)
    if not detail:
        raise HTTPException(status_code=404, detail="work order not found")
    return {"ok": True, "data": detail}


# ============================================================================
# 高级查询 API
# ============================================================================

class AdvancedQuery(BaseModel):
    query_type: str  # 'room_with_orders' / 'staff_workload' / 'daily_report'
    params: Dict[str, Any] = {}


@router.post("/api/query/advanced")
def api_advanced_query(query: AdvancedQuery):
    """高级查询"""
    if query.query_type == "room_with_orders":
        room_no = query.params.get("room_no")
        if not room_no:
            raise HTTPException(status_code=400, detail="room_no required")
        return {"ok": True, "data": get_room_full_status(room_no)}
    
    elif query.query_type == "staff_workload":
        staff_id = query.params.get("staff_id")
        if not staff_id:
            raise HTTPException(status_code=400, detail="staff_id required")
        
        # 查找该员工的工单
        staff_relations = list_relations(to_type="staff", to_id=staff_id, relation_type="assignee")
        wo_ids = [r["from_id"] for r in staff_relations]
        
        work_orders = [
            e for e in list_events("work_order")
            if e.get("data", {}).get("wo_id") in wo_ids
        ]
        
        return {"ok": True, "data": {"staff_id": staff_id, "work_orders": work_orders}}
    
    elif query.query_type == "daily_report":
        date = query.params.get("date", _now()[:10])
        
        # 统计当天的事件
        events = list_events()
        today_events = [e for e in events if e.get("created_at", "").startswith(date)]
        
        report = {
            "date": date,
            "total_events": len(today_events),
            "by_type": {},
            "by_status": {}
        }
        
        for e in today_events:
            t = e.get("event_type", "unknown")
            s = e.get("status", "unknown")
            report["by_type"][t] = report["by_type"].get(t, 0) + 1
            report["by_status"][s] = report["by_status"].get(s, 0) + 1
        
        return {"ok": True, "data": report}
    
    else:
        raise HTTPException(status_code=400, detail=f"unknown query_type: {query.query_type}")
