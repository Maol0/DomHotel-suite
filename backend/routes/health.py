# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — health 端点

单独成文件是为了让 routes/__init__.py 的"合并 router"机制统一处理它,
避免 main.py 单独 include_router 后,PluginLoader register(api) 时
和业务 router 共享同一 prefix 报"prefix already registered"错误。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter

from .. import data_layer

router = APIRouter()


def _read_plugin_version() -> str:
    """从 plugin.json 读取真实 version(避免硬编码跟 changelog 不同步)"""
    try:
        p = Path(__file__).resolve().parents[2] / "plugin.json"
        return json.loads(p.read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        return "unknown"


@router.get("/health")
async def health() -> Dict[str, Any]:
    """PAWAPP 健康检查 + 数据目录探测"""
    data_dir = data_layer.DATA_DIR
    rooms_count = len(data_layer.load_table("rooms"))
    return {
        "ok": True,
        "pawapp": "domhotel-suite",
        "version": _read_plugin_version(),
        "data_dir": str(data_dir),
        "data_dir_exists": data_dir.is_dir(),
        "rooms_count": rooms_count,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/data/version")
async def data_version() -> Dict[str, Any]:
    """整合版: 全局数据版本号 (轻量, 供前端轮询)

    前台接待/房态看板/工单/向导任何模块写表都会让版本号变化,
    前端发现变化后静默刷新所有面板 → 多端 (前台电脑 + 企微手机) 全局同步。
    """
    return {
        "ok": True,
        "version": data_layer.get_data_version(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
