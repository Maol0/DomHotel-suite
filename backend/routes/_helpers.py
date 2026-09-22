# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 共享 helper

  - now()    (原 _now)
  - today()  (原 _today)
  - new_id(prefix)  (原 _new_id)
  - create_work_order(...)  (原 _create_work_order)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from .. import data_layer


def now() -> str:
    # v1.4.0: 固定北京时间 (+8), 不受容器系统时区影响
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def build_work_order(
    room_no: str,
    work_type: str,
    description: str,
    priority: str = "normal",
    reporter: str = "guest",
    target_dept: str = "",
    data_source: str = "manual",
    operator: str = "",
) -> dict:
    """构建工单对象（不保存）

    v2026-09-04: 拆分构建和保存，避免双重保存导致的竞态条件
    """
    if not target_dept:
        mapping = {
            "清洁": "housekeeping",
            "维修": "engineering",
            "补充消耗品": "housekeeping",
            "换房": "frontdesk",
            "送物": "frontdesk",
        }
        target_dept = mapping.get(work_type, "frontdesk")

    wo = {
        "wo_id": new_id("WO"),
        "room_no": room_no,
        "work_type": work_type,
        "description": description,
        "priority": priority,
        "status": "pending",  # 默认 pending，调用方可改
        "reporter": reporter,
        "target_dept": target_dept,
        "assignee": "",
        "assignee_id": "",
        "created_at": now(),
        "updated_at": now(),
        "completed_at": "",
        "data_source": data_source,
        "source_detail": operator,
    }
    return wo


def save_work_order(wo: dict) -> None:
    """保存工单到 work_orders.json

    v2026-09-04: 统一保存入口，避免多处重复 load/save
    """
    wos = data_layer.load_table("work_orders")
    # 检查是否已存在（更新）
    for i, w in enumerate(wos):
        if w.get("wo_id") == wo.get("wo_id"):
            wos[i] = wo
            data_layer.save_table("work_orders", wos)
            return
    # 不存在则追加
    wos.append(wo)
    data_layer.save_table("work_orders", wos)


def create_work_order(
    room_no: str,
    work_type: str,
    description: str,
    priority: str = "normal",
    reporter: str = "guest",
    target_dept: str = "",
    data_source: str = "manual",
    operator: str = "",
) -> dict:
    """建工单并保存（向后兼容）

    注意：新代码建议用 build_work_order + save_work_order 分两步，避免双重保存
    """
    wo = build_work_order(
        room_no=room_no,
        work_type=work_type,
        description=description,
        priority=priority,
        reporter=reporter,
        target_dept=target_dept,
        data_source=data_source,
        operator=operator,
    )
    save_work_order(wo)
    return wo
