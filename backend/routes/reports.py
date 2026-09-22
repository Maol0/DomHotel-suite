# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 报告/统计路由 (3 endpoints)

  - /handover  交接班仪表盘
  - /guests    在住客人列表
  - /stats     全局统计
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from .. import data_layer
from ..wecom_sync import queue_status
from ._helpers import now, today

logger = logging.getLogger(__name__)

# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()


def register_routes(app) -> None:
    """注册报告路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/handover")
    async def get_handover(ctx=Depends(get_ctx), date: str = "") -> Dict[str, Any]:
        if not date:
            date = today()
        rooms = data_layer.load_table("rooms")
        wos = data_layer.load_table("work_orders")
        today_wos = [w for w in wos if w.get("created_at", "").startswith(date)]

        status_count: Dict[str, int] = {}
        for r in rooms:
            s = r.get("status", "未知")
            status_count[s] = status_count.get(s, 0) + 1

        pending = [w for w in today_wos if w.get("status") in ("pending", "assigned", "in_progress")]
        done = [w for w in today_wos if w.get("status") == "done"]
        abnormal = [r for r in rooms if r.get("status") in ("维修中", "已锁房")]

        return {
            "date": date,
            "total_rooms": len(rooms),
            "status_summary": status_count,
            "today_work_orders": {
                "total": len(today_wos),
                "pending": len(pending),
                "done": len(done),
            },
            "abnormal_rooms": abnormal,
            "dirty_rooms_need_clean": [r for r in rooms if r.get("status") == "脏房"],
            "occupied_rooms": [r for r in rooms if r.get("status") == "在住"],
        }

    @router.get("/guests")
    async def list_guests(ctx=Depends(get_ctx)) -> List[Dict[str, Any]]:
        rooms = data_layer.load_table("rooms")
        return [r for r in rooms if r.get("status") == "在住"]

    @router.get("/stats")
    async def get_stats(ctx=Depends(get_ctx)) -> Dict[str, Any]:
        rooms = data_layer.load_table("rooms")
        wos = data_layer.load_table("work_orders")
        t = today()

        status_count: Dict[str, int] = {}
        for r in rooms:
            s = r.get("status", "未知")
            status_count[s] = status_count.get(s, 0) + 1

        today_wos = [w for w in wos if w.get("created_at", "").startswith(t)]
        pending_wos = [w for w in wos if w.get("status") in ("pending", "assigned", "in_progress")]

        return {
            "total_rooms": len(rooms),
            "status_summary": status_count,
            "today_work_orders": len(today_wos),
            "pending_work_orders": len(pending_wos),
            "abnormal_count": status_count.get("维修中", 0) + status_count.get("已锁房", 0),
            "wecom_sync": queue_status(),
        }

    app.include_router(router)
    logger.info("[routes/reports] 已注册 3 个报告/统计路由")
