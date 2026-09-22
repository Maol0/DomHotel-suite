"""
酒店房务工作台 v3.0 - 统一数据访问层

三表架构:
- events: 事件流（替代 work_orders + pending_actions + supplies + dispatch_log + auth_log + chats）
- entities: 实体（替代 rooms + departments + staff + room_types + floors + guests）
- relations: 关联（替代外键关联）
"""
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent
LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load(name: str) -> list:
    p = DATA_DIR / f"{name}.json"
    if not p.exists():
        return []
    with LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []


def _save(name: str, data: list) -> None:
    p = DATA_DIR / f"{name}.json"
    with LOCK:
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


# ============================================================================
# events 表 - 事件流
# ============================================================================

def list_events(event_type: str = None, subject_id: str = None,
                status: str = None, limit: int = 100) -> list:
    events = _load("events")
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]
    if subject_id:
        events = [e for e in events if e.get("subject_id") == subject_id]
    if status:
        events = [e for e in events if e.get("status") == status]
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return events[:limit]


def get_event(event_id: int) -> dict:
    for e in _load("events"):
        if e.get("event_id") == event_id:
            return e
    return {}


def create_event(event_type: str, subject_type: str, subject_id: str,
                status: str = "pending", data: dict = None,
                operator: str = "system", source: str = "manual") -> dict:
    events = _load("events")
    max_id = max((e.get("event_id", 0) for e in events), default=0)
    event = {
        "event_id": max_id + 1,
        "event_type": event_type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "status": status,
        "data": data or {},
        "operator": operator,
        "source": source,
        "created_at": _now(),
        "updated_at": _now()
    }
    events.append(event)
    _save("events", events)
    return event


def update_event(event_id: int, **kwargs) -> dict:
    events = _load("events")
    for e in events:
        if e.get("event_id") == event_id:
            old_status = e.get("status")
            e.update(kwargs)
            e["updated_at"] = _now()
            # 记录状态变更历史
            if "status" in kwargs and kwargs["status"] != old_status:
                history = e.setdefault("history", [])
                history.append({
                    "time": _now(),
                    "from": old_status,
                    "to": kwargs["status"],
                    "operator": kwargs.get("operator", e.get("operator", "system"))
                })
            _save("events", events)
            return e
    return {}


# 状态机：work_order 状态转换规则
WORK_ORDER_TRANSITIONS = {
    "pending_confirm": ["pending", "rejected"],
    "pending": ["assigned", "rejected"],
    "assigned": ["in_progress", "done", "rejected"],
    "in_progress": ["done", "rejected"],
    "done": [],
    "rejected": [],
}


def transition_work_order(wo_id: str, new_status: str, operator: str = "system") -> dict:
    """工单状态机转换"""
    events = _load("events")
    for e in events:
        if (e.get("event_type") == "work_order" and 
            e.get("data", {}).get("wo_id") == wo_id):
            old_status = e.get("status")
            allowed = WORK_ORDER_TRANSITIONS.get(old_status, [])
            if new_status not in allowed:
                return {
                    "ok": False,
                    "error": f"不能从 {old_status} 转换到 {new_status}，允许的目标: {allowed}"
                }
            e["status"] = new_status
            e["updated_at"] = _now()
            history = e.setdefault("history", [])
            history.append({
                "time": _now(),
                "from": old_status,
                "to": new_status,
                "operator": operator
            })
            _save("events", events)
            # 联动房态
            sync_room_from_work_order(wo_id)
            return {"ok": True, "event": e, "transition": f"{old_status} → {new_status}"}
    return {"ok": False, "error": f"工单 {wo_id} 不存在"}


def confirm_work_order(wo_id: str, confirmed_by: str, assignee: str = None) -> dict:
    """确认待确认的工单（pending_confirm → pending/assigned）"""
    events = _load("events")
    for e in events:
        if (e.get("event_type") == "work_order" and 
            e.get("data", {}).get("wo_id") == wo_id):
            old_status = e.get("status")
            if old_status != "pending_confirm":
                return {"ok": False, "error": f"工单 {wo_id} 状态是 {old_status}，不是 pending_confirm"}
            
            # 确认后转为 assigned（如果有 assignee）或 pending（等待派单）
            if assignee:
                e["data"]["assignee"] = assignee
                new_status = "assigned"
            else:
                # 自动派单：找部门员工
                auto_assignee = _find_department_employee(e["data"].get("target_dept"))
                if auto_assignee:
                    e["data"]["assignee"] = auto_assignee
                    new_status = "assigned"
                else:
                    new_status = "pending"
            
            e["status"] = new_status
            e["updated_at"] = _now()
            e["data"]["confirmed_by"] = confirmed_by
            e["data"]["confirmed_at"] = _now()
            history = e.setdefault("history", [])
            history.append({
                "time": _now(),
                "from": old_status,
                "to": new_status,
                "operator": confirmed_by,
                "note": f"确认{'派给 ' + assignee if assignee else '自动派单'}"
            })
            _save("events", events)
            
            # 联动房态
            sync_room_from_work_order(wo_id)
            return {"ok": True, "event": e, "transition": f"{old_status} → {new_status}"}
    return {"ok": False, "error": f"工单 {wo_id} 不存在"}


def _find_department_employee(dept_name: str) -> str:
    """查找部门员工（优先 manager，其次 employee）"""
    if not dept_name:
        return ""
    
    entities = _load("entities")
    staff_list = [e for e in entities if e.get("entity_type") == "staff"]
    
    # 优先找 manager
    for s in staff_list:
        data = s.get("data", {})
        if data.get("department_id") == f"dept-{dept_name}" and data.get("role") == "manager":
            return data.get("name", "")
    
    # 其次找 employee
    for s in staff_list:
        data = s.get("data", {})
        if data.get("department_id") == f"dept-{dept_name}" and data.get("role") == "employee":
            return data.get("name", "")
    
    return ""


def create_work_order_with_assign(room_no: str, work_type: str, description: str, 
                                   priority: str = "normal", target_dept: str = None,
                                   operator: str = "system", source: str = "manual") -> dict:
    """创建工单并自动派单"""
    # 生成工单 ID
    wo_id = f"WO-{_now().replace(' ', '-').replace(':', '-')}-{uuid.uuid4().hex[:3]}"
    
    # 自动派单
    assignee = ""
    status = "pending"
    if target_dept:
        assignee = _find_department_employee(target_dept)
        if assignee:
            status = "assigned"
    
    # 创建工单事件
    event = create_event(
        event_type="work_order",
        subject_type="room",
        subject_id=room_no,
        status=status,
        data={
            "wo_id": wo_id,
            "room_no": room_no,
            "work_type": work_type,
            "description": description,
            "priority": priority,
            "target_dept": target_dept,
            "assignee": assignee,
            "source": source,
        },
        operator=operator,
        source=source
    )
    
    # 创建关联
    create_relation(
        from_type="work_order", from_id=wo_id,
        to_type="room", to_id=room_no,
        relation_type="work_order_for_room"
    )
    
    if assignee:
        # 找到 staff entity_id
        staff_entity = None
        for e in _load("entities"):
            if e.get("entity_type") == "staff" and e.get("data", {}).get("name") == assignee:
                staff_entity = e
                break
        if staff_entity:
            create_relation(
                from_type="work_order", from_id=wo_id,
                to_type="staff", to_id=staff_entity["entity_id"],
                relation_type="assignee"
            )
    
    # 联动房态
    sync_room_from_work_order(wo_id)
    
    return {"ok": True, "event": event, "wo_id": wo_id, "assignee": assignee}


def sync_room_from_work_order(wo_id: str):
    """工单状态变更后，联动房态"""
    events = _load("events")
    relations = _load("relations")
    
    # 找工单事件
    wo_event = None
    for e in events:
        if e.get("event_type") == "work_order" and e.get("data", {}).get("wo_id") == wo_id:
            wo_event = e
            break
    
    if not wo_event:
        return
    
    wo_data = wo_event.get("data", {})
    wo_status = wo_event.get("status")
    room_no = wo_data.get("room_no")
    work_type = wo_data.get("work_type")
    
    if not room_no:
        return
    
    # 统一 room_no 格式：去掉前导零
    normalized_room = str(int(room_no)) if room_no.isdigit() else room_no
    
    # 找房态实体
    room_entity = None
    for ent in _load("entities"):
        if ent.get("entity_id") == f"room-{normalized_room}":
            room_entity = ent
            break
    
    if not room_entity:
        return
    
    # 创建房态事件
    new_status = None
    if work_type == "维修" and wo_status == "done":
        new_status = "空房"  # 维修完成 → 空房
    elif work_type == "清洁" and wo_status == "done":
        new_status = "空房"  # 清洁完成 → 空房
    
    if new_status and new_status != room_entity["data"].get("status"):
        room_entity["data"]["status"] = new_status
        room_entity["updated_at"] = _now()
        room_entity["data"].setdefault("history", []).append({
            "time": _now(),
            "from": room_entity["data"].get("status"),
            "to": new_status,
            "operator": "auto",
            "reason": f"工单 {wo_id} 完成"
        })
        # 保存
        entities = _load("entities")
        for ent in entities:
            if ent.get("entity_id") == room_entity["entity_id"]:
                ent.update(room_entity)
                break
        _save("entities", entities)


# ============================================================================
# entities 表 - 实体
# ============================================================================

ENTITY_TYPES = {"room", "staff", "department", "room_type", "floor", "guest"}


def list_entities(entity_type: str = None) -> list:
    entities = _load("entities")
    if entity_type:
        entities = [e for e in entities if e.get("entity_type") == entity_type]
    return entities


def get_entity(entity_id: str) -> dict:
    for e in _load("entities"):
        if e.get("entity_id") == entity_id:
            return e
    return {}


def create_entity(entity_type: str, data: dict, entity_id: str = None) -> dict:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity_type: {entity_type}")
    entities = _load("entities")
    # 生成 entity_id 如果没提供
    if not entity_id:
        if entity_type == "room":
            entity_id = f"room-{data.get('room_no')}"
        else:
            entity_id = f"{entity_type}-{len(entities) + 1}"
    entity = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "data": data,
        "created_at": _now(),
        "updated_at": _now()
    }
    entities.append(entity)
    _save("entities", entities)
    return entity


def update_entity(entity_id: str, **kwargs) -> dict:
    entities = _load("entities")
    for e in entities:
        if e.get("entity_id") == entity_id:
            e["data"].update(kwargs)
            e["updated_at"] = _now()
            _save("entities", entities)
            return e
    return {}


# ============================================================================
# relations 表 - 关联
# ============================================================================

RELATION_TYPES = {
    "work_order_for_room": ("work_order", "room"),
    "assignee": ("work_order", "staff"),
    "guest_in_room": ("guest", "room"),
}


def list_relations(from_type: str = None, from_id: str = None,
                  to_type: str = None, to_id: str = None,
                  relation_type: str = None) -> list:
    relations = _load("relations")
    if from_type:
        relations = [r for r in relations if r.get("from_type") == from_type]
    if from_id:
        relations = [r for r in relations if r.get("from_id") == from_id]
    if to_type:
        relations = [r for r in relations if r.get("to_type") == to_type]
    if to_id:
        relations = [r for r in relations if r.get("to_id") == to_id]
    if relation_type:
        relations = [r for r in relations if r.get("relation_type") == relation_type]
    return relations


def create_relation(from_type: str, from_id: str, to_type: str, to_id: str,
                    relation_type: str, data: dict = None) -> dict:
    relations = _load("relations")
    max_id = max((r.get("relation_id", 0) for r in relations), default=0)
    relation = {
        "relation_id": max_id + 1,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "relation_type": relation_type,
        "data": data or {},
        "created_at": _now()
    }
    relations.append(relation)
    _save("relations", relations)
    return relation


# ============================================================================
# 跨表查询（高级 API）
# ============================================================================

def get_room_full_status(room_no: str) -> dict:
    """获取房间完整状态：实体 + 进行中工单 + 待处理事件"""
    # 统一 room_no 格式：去掉前导零，如 "0101" → "101"
    normalized = str(int(room_no)) if room_no.isdigit() else room_no
    entity_id = f"room-{normalized}"
    room_entity = get_entity(entity_id)
    room_data = room_entity.get("data", {}) if room_entity else {}

    # 通过 relations 找工单
    room_relations = list_relations(relation_type="work_order_for_room")
    wo_ids = set(r["from_id"] for r in room_relations if r.get("to_id") in (room_no, normalized))

    open_work_orders = [
        e for e in list_events("work_order")
        if e.get("data", {}).get("wo_id") in wo_ids
        and e.get("status") not in ("done", "rejected", "cancelled")
    ]

    pending_events = [
        e for e in list_events(subject_id=room_no, status="pending")
    ]

    return {
        "room": room_entity,
        "open_work_orders": open_work_orders,
        "pending_events": pending_events
    }


def get_work_order_detail(wo_id: str) -> dict:
    """获取工单详情 + 关联的房间和分配人"""
    event = None
    for e in list_events("work_order"):
        if e.get("data", {}).get("wo_id") == wo_id:
            event = e
            break
    if not event:
        return {}

    room_rel = next(iter(list_relations(from_type="work_order", from_id=wo_id, relation_type="work_order_for_room")), None)
    room_no = room_rel["to_id"] if room_rel else None

    assignee_rel = next(iter(list_relations(from_type="work_order", from_id=wo_id, relation_type="assignee")), None)
    assignee_id = assignee_rel["to_id"] if assignee_rel else None

    # 获取分配人姓名
    assignee_name = ""
    if assignee_id:
        ent = get_entity(assignee_id)
        if ent:
            assignee_name = ent["data"].get("name", "")

    return {
        "event": event,
        "data": event.get("data", {}),
        "room_no": room_no,
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "room_status": get_room_full_status(room_no).get("room", {}).get("data", {}).get("status", "") if room_no else "",
        "history": event.get("history", [])
    }


def init_sample_data():
    """初始化示例数据"""
    import random

    # 清空旧数据
    _save("events", [])
    _save("entities", [])
    _save("relations", [])

    # 1. 部门
    dept_data = {
        "housekeeping": {"name": "客房", "desc": "客房清洁"},
        "engineering": {"name": "工程", "desc": "设备维修"},
        "frontdesk": {"name": "前台", "desc": "前台接待"},
        "restaurant": {"name": "餐厅", "desc": "餐饮服务"}
    }
    dept_ids = {}
    for i, (key, data) in enumerate(dept_data.items()):
        entity = create_entity("department", data, entity_id=f"dept-{key}")
        dept_ids[key] = entity["entity_id"]

    # 2. 房型
    room_types = [
        {"name": "单人间", "beds": 1, "area": 18, "price": 199},
        {"name": "双人间", "beds": 2, "area": 28, "price": 299},
        {"name": "套房", "beds": 1, "area": 45, "price": 599},
        {"name": "总统套房", "beds": 2, "area": 88, "price": 1888}
    ]
    for i, rt in enumerate(room_types):
        create_entity("room_type", rt, entity_id=f"rt-{i+1}")

    # 3. 楼层
    for i in range(1, 6):
        create_entity("floor", {"name": f"{i}楼", "order": i, "desc": f"第{i}层"}, entity_id=f"floor-{i}")

    # 4. 员工
    staff_data = [
        {"name": "domai", "phone": "domai", "department_id": "dept-frontdesk", "role": "super_admin"},
        {"name": "张小前台", "phone": "13800000001", "department_id": "dept-frontdesk", "role": "manager"},
        {"name": "李小前台", "phone": "13800000002", "department_id": "dept-frontdesk", "role": "employee"},
        {"name": "王经理", "phone": "13800000003", "department_id": "dept-housekeeping", "role": "manager"},
        {"name": "张阿姨", "phone": "13800000004", "department_id": "dept-housekeeping", "role": "employee"},
        {"name": "李阿姨", "phone": "13800000005", "department_id": "dept-housekeeping", "role": "employee"},
        {"name": "工程王经理", "phone": "13800000006", "department_id": "dept-engineering", "role": "manager"},
        {"name": "赵师傅", "phone": "13800000007", "department_id": "dept-engineering", "role": "employee"},
        {"name": "餐厅经理", "phone": "13800000008", "department_id": "dept-restaurant", "role": "manager"},
        {"name": "陈厨师", "phone": "13800000009", "department_id": "dept-restaurant", "role": "employee"}
    ]
    for staff in staff_data:
        entity = create_entity("staff", staff)
        # 关联到部门
        create_relation("staff", entity["entity_id"], "department",
                       staff["department_id"], "member_of_department")

    # 5. 房间（100间，分布在5层）
    room_statuses = ["空房", "空房", "空房", "空房", "空房", "空房", "在住", "在住", "待打扫", "维修中", "脏房"]
    for floor in range(1, 6):
        for num in range(1, 21):
            room_no = f"{floor:01d}{num:02d}"  # 4 位：0101, 0205, etc.
            status = random.choice(room_statuses)
            guest_name = ""
            guest_phone = ""
            checkin_time = ""
            checkin_days = 0
            if status == "在住":
                guests = ["王先生", "李小姐", "张总", "赵女士", "钱经理", "孙先生", "周小姐", "吴总"]
                guest_name = random.choice(guests)
                guest_phone = f"139{random.randint(10000000, 99999999)}"
                checkin_days = random.randint(1, 5)
                checkin_time = _now()

            room_data = {
                "room_no": room_no,
                "floor": floor,
                "room_type": random.choice(["单人间", "双人间", "套房", "总统套房"]),
                "status": status,
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "checkin_time": checkin_time,
                "checkin_days": checkin_days,
                "notes": "",
                "deleted": False
            }
            entity = create_entity("room", room_data, entity_id=f"room-{room_no}")

            # 关联到楼层
            create_relation(f"room-{room_no}", "floor", f"floor-{floor}",
                          f"room-{room_no}", "belongs_to_floor")

            # 如果有客人，关联客人
            if guest_name:
                create_relation("guest", f"guest-{room_no}", "room",
                              f"room-{room_no}", "guest_in_room",
                              {"name": guest_name})

    # 6. 示例工单（放在不同状态）
    sample_work_orders = [
        {"room_no": "0101", "work_type": "维修", "status": "assigned",
         "assignee": "赵师傅", "description": "空调外机故障", "priority": "high",
         "target_dept": "engineering"},
        {"room_no": "0205", "work_type": "清洁", "status": "pending_confirm",
         "assignee": "", "description": "退房后待派清洁", "priority": "normal",
         "target_dept": "housekeeping"},
        {"room_no": "0308", "work_type": "维修", "status": "in_progress",
         "assignee": "赵师傅", "description": "灯具更换", "priority": "low",
         "target_dept": "engineering"},
        {"room_no": "0403", "work_type": "补充消耗品", "status": "done",
         "assignee": "张阿姨", "description": "补货：矿泉水、牙刷", "priority": "normal",
         "target_dept": "housekeeping"},
    ]

    for idx, wo_data in enumerate(sample_work_orders):
        # 生成 WO ID
        wo_id = f"WO-{_now().replace(' ', '-').replace(':', '-')}-{idx:03d}"
        wo_data["wo_id"] = wo_id

        # 创建工单事件
        event = create_event(
            "work_order", "room", wo_data["room_no"],
            status=wo_data["status"],
            data=wo_data,
            operator="domai",
            source="manual"
        )

        # 关联到房间
        create_relation("work_order", wo_id,
                      "room", wo_data["room_no"], "work_order_for_room")

        # 关联到员工（如果有分配人）
        if wo_data.get("assignee"):
            # 通过名字找 staff
            for s in list_entities("staff"):
                if s["data"].get("name") == wo_data["assignee"]:
                    create_relation("work_order", wo_id,
                                  "staff", s["entity_id"], "assignee")
                    break

    print(f"初始化完成：")
    print(f"  entities: {len(_load('entities'))}")
    print(f"  events: {len(_load('events'))}")
    print(f"  relations: {len(_load('relations'))}")


if __name__ == "__main__":
    init_sample_data()
