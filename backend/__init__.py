# -*- coding: utf-8 -*-
"""DomHotel Suite PawApp — SDK 模式入口 (整合版)

整合三大模块: HUB 配置向导 + 前台接待 + 房态工作台
实例化 PawApp,让 PluginLoader 在加载时调用 app.register(api)。
所有路由/工具/命令都在 main.py 里用 @app.route / @app.tool 注册。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from qwenpaw.pawapp import PawApp

import logging
logger = logging.getLogger(__name__)

# 加载 main.py 并把它注入为本模块同名属性。PluginLoader 只把整个 plugin 目录加为
# search path,不会把 backend/ 视为 package,因此不能 'from . import main'。
# 这里用 importlib 直接 spec_file,并把 main 注册为 backend package 的 submodule,
# 让 main.py 内部的 'from . import app' 也能解析。
_THIS_DIR = Path(__file__).resolve().parent

# app_id 必须和 plugin.json 的 id 一致,这样路由前缀才是 /domhotel-suite/*
app = PawApp(name="domhotel-suite", app_id="domhotel-suite")

# PluginLoader 期望模块导出名为 'plugin' 的对象(SDK 模式约定)
plugin = app
_BACKEND_PKG_NAME = "__domhotel_suite_backend__"
_MAIN_FILE = _THIS_DIR / "main.py"
_DATA_DIR_FILE = _THIS_DIR / "data_layer.py"

# 1) 预先注册 backend package（让 main.py 可以 'from . import app'）
if _BACKEND_PKG_NAME not in sys.modules:
    _pkg_spec = importlib.util.spec_from_file_location(
        _BACKEND_PKG_NAME,
        str(_THIS_DIR / "__init__.py"),
        submodule_search_locations=[str(_THIS_DIR)],
    )
    if _pkg_spec:
        _pkg_mod = importlib.util.module_from_spec(_pkg_spec)
        _pkg_mod.__package__ = _BACKEND_PKG_NAME
        _pkg_mod.__path__ = [str(_THIS_DIR)]
        sys.modules[_BACKEND_PKG_NAME] = _pkg_mod
        # 复用本模块已构造的 app / plugin,避免重复实例化
        _pkg_mod.app = app
        _pkg_mod.plugin = plugin

# 2) 加载 data_layer.py
if _DATA_DIR_FILE.exists():
    _dl_spec = importlib.util.spec_from_file_location(
        f"{_BACKEND_PKG_NAME}.data_layer",
        str(_DATA_DIR_FILE),
    )
    if _dl_spec and _dl_spec.loader:
        _dl_mod = importlib.util.module_from_spec(_dl_spec)
        _dl_mod.__package__ = _BACKEND_PKG_NAME
        sys.modules[f"{_BACKEND_PKG_NAME}.data_layer"] = _dl_mod
        _dl_spec.loader.exec_module(_dl_mod)
        data_layer = _dl_mod

# 2.5) 预加载 wecom_sync / appchat / wecom_kf（让 routes 里的相对导入能解析）
try:
    import importlib as _importlib
    _importlib.import_module(f"{_BACKEND_PKG_NAME}.wecom_sync")
    _importlib.import_module(f"{_BACKEND_PKG_NAME}.appchat")
    _importlib.import_module(f"{_BACKEND_PKG_NAME}.wecom_kf")
    logger.info("[backend] wecom_sync / appchat / wecom_kf 预加载完成")
except Exception as exc:
    logger.warning("[backend] 预加载 wecom_sync / appchat / wecom_kf 失败: %s", exc)

# 3) 加载 backend/routes/ 子包（业务路由集合,必须在 main.py 之前 import,这样
#    main.py 里的 `from .routes import rooms as rooms_routes` 才能解析）
_ROUTES_DIR = _THIS_DIR / "routes"
if _ROUTES_DIR.is_dir():
    _routes_spec = importlib.util.spec_from_file_location(
        f"{_BACKEND_PKG_NAME}.routes",
        str(_ROUTES_DIR / "__init__.py"),
        submodule_search_locations=[str(_ROUTES_DIR)],
    )
    if _routes_spec:
        _routes_mod = importlib.util.module_from_spec(_routes_spec)
        _routes_mod.__package__ = f"{_BACKEND_PKG_NAME}.routes"
        _routes_mod.__path__ = [str(_ROUTES_DIR)]
        sys.modules[f"{_BACKEND_PKG_NAME}.routes"] = _routes_mod
        _routes_spec.loader.exec_module(_routes_mod)
        # 立刻把所有业务路由挂到 app 上
        if hasattr(_routes_mod, "register_all"):
            _routes_mod.register_all(app)
            logger.info(f"[backend] 业务路由 register_all 完成")

# 4) 加载 main.py 作为 backend package 的 submodule
if _MAIN_FILE.exists():
    _main_spec = importlib.util.spec_from_file_location(
        f"{_BACKEND_PKG_NAME}.main",
        str(_MAIN_FILE),
    )
    if _main_spec and _main_spec.loader:
        _main_mod = importlib.util.module_from_spec(_main_spec)
        _main_mod.__package__ = _BACKEND_PKG_NAME
        sys.modules[f"{_BACKEND_PKG_NAME}.main"] = _main_mod
        _main_spec.loader.exec_module(_main_mod)
        main = _main_mod  # type: ignore[attr-defined]

# 5) 启动企微同步后台重试循环 (Phase 5 新增)
#    - 每 60s 跑一次 retry_pending, 失败队列里的任务会重试
#    - 后台 daemon, 不阻塞主流程
#    - 注意: 仅在 app 启动时调用一次, 热加载时已经在跑的 loop 不会重启 (避免重复)
#    - v1.2.0: PluginLoader 把本文件加载为 plugin_domhotel_suite (search path
#      是插件根目录), `from . import wecom_sync` 会找不到 backend/ 下的模块
#      → 改用 _BACKEND_PKG_NAME (其 __path__ 已指向 backend/) 可靠加载
try:
    import importlib as _importlib
    wecom_sync = _importlib.import_module(f"{_BACKEND_PKG_NAME}.wecom_sync")
    wecom_sync.start_retry_loop(interval_s=60)
    logger.info("[backend] 企微同步后台重试循环已启动 (60s)")
except Exception as exc:
    logger.warning("[backend] 启动企微同步后台循环失败: %s", exc)

# 5.5) v1.4.1: 客服消息定时拉取兜底循环 — 已禁用
# 原因: 定时拉取 (30s) 与企微回调同时触发 sync_and_process,
# 导致同一条消息被处理两次, 客人收到两条重复回复。
# 回调链路已可靠工作, 不再需要主动拉取兜底。
# 如果未来回调不稳定需要重新启用, 必须先在 sync_and_process 中
# 加 asyncio.Lock + 事件级去重 (enter_session 等无 msgid 的事件)。
# wecom_kf.start_kf_sync_loop(interval_s=30)

# 5.6) v1.4.1: 启动工单超时未接单提醒循环
try:
    work_orders_mod = _importlib.import_module(f"{_BACKEND_PKG_NAME}.routes.work_orders")
    work_orders_mod.start_timeout_reminder_loop(interval_s=600)
    logger.info("[backend] 工单超时提醒循环已启动 (600s)")
except Exception as exc:
    logger.warning("[backend] 启动工单超时提醒循环失败: %s", exc)

# 6) v2.1.3: 自动部署 4 个 AI 助手由 main.py 的 @app.on_launch 钩子负责
#    (PawApp lifecycle: plugin 启动时调用 on_launch, 失败不致命)
#    这里不再用 schedule_auto_deploy, 避免双重调用

# 7) v2.1.5: 注入 HOTEL_PAWAPP_BASE_URL 给 SOUL.md 用
#    agent 在自己的 SOUL.md 里用 $HOTEL_PAWAPP_BASE_URL 调 API
#    plugin 加载时尝试从 QWENPAW_BASE_URL / QWENPAW_HOST / 默认 127.0.0.1:8889 推断
if not os.environ.get("HOTEL_PAWAPP_BASE_URL"):
    _base = os.environ.get("QWENPAW_BASE_URL", "")
    if not _base:
        _host = os.environ.get("QWENPAW_HOST", "127.0.0.1")
        _port = os.environ.get("QWENPAW_PORT", "8889")
        _base = f"http://{_host}:{_port}"
    os.environ["HOTEL_PAWAPP_BASE_URL"] = _base
    logger.info(f"[backend] HOTEL_PAWAPP_BASE_URL={_base}")

# 8) v2.2.0: 插件启动时自动归档昨日已完成工单
#    逻辑：将 status=done 且 completed_at 不是今天的工单移入 work_orders_archive.json
#    企微表格未配置时直接归档到本地（不丢数据），已配置时先推送再归档
try:
    from datetime import datetime as _dt
    _today = _dt.now().strftime("%Y-%m-%d")
    _wos = data_layer.load_table("work_orders")
    _to_archive = []
    _to_keep = []
    for _w in _wos:
        if _w.get("status") == "done":
            _completed = _w.get("completed_at", "")[:10]
            if _completed and _completed != _today:
                _to_archive.append(_w)
            else:
                _to_keep.append(_w)
        else:
            _to_keep.append(_w)
    if _to_archive:
        _archive = []
        try:
            _archive = data_layer.load_table("work_orders_archive")
        except Exception:
            pass
        _archive.extend(_to_archive)
        data_layer.save_table("work_orders_archive", _archive)
        data_layer.save_table("work_orders", _to_keep)
        logger.info("[backend] 启动自动归档: %d 条昨日工单 → archive, 本地保留 %d 条", len(_to_archive), len(_to_keep))
    else:
        logger.info("[backend] 启动自动归档: 无需归档（本地只有活跃+今日已完成）")
except Exception as _arch_exc:
    logger.warning("[backend] 启动自动归档失败（不影响正常运行）: %s", _arch_exc)