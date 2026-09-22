# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 数据导出路由 (1 endpoint)

"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import data_layer
from ._helpers import now

logger = logging.getLogger(__name__)

# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()


def register_routes(app) -> None:
    """注册导出路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/export")
    async def export_data(ctx=Depends(get_ctx)) -> Dict[str, Any]:
        return {
            "rooms": data_layer.load_table("rooms"),
            "work_orders": data_layer.load_table("work_orders"),
            "supplies": data_layer.load_table("supplies"),
            "exported_at": now(),
        }

    app.include_router(router)
    logger.info("[routes/export] 已注册 1 个导出路由")
