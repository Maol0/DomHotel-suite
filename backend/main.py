# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 启动钩子 (v2.0 自包含)

为什么必须合并:
  PawApp 的 register(api) 会用 prefix='/hotel-frontdesk-pawapp' 给每个
  单独的 router register_http_router。如果有 7 个独立 router,会 register
  7 次,前 6 次会因 prefix 已注册而报错。
  合并为 1 个 router 后,register(api) 只 register 1 次,prefix 只占 1 次。
"""
from __future__ import annotations

import logging

from . import app, data_layer
from . import chat_session_manager

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@app.on_launch
async def on_launch_hook() -> None:
    """PAWAPP 启动时打 ready 日志(所有路由在模块加载时已注册完毕)

    v2.1.3 新增: on_launch 时自动跑一次 4-agent 部署
    - 让新装插件的用户无需手动操作, 4 个 AI 助手自动可用
    - 用 .lock 文件防止重复 (1h 内不重复)
    - 失败不致命, 日志写 qwenpaw.log
    """
    logger.info("[hotel-frontdesk-pawapp] on_launch: ready")
    info = data_layer.version_info()
    logger.info(
        "[hotel-frontdesk-pawapp] data_dir=%s files=%d",
        info.get("data_dir"),
        len(info.get("files", [])),
    )

    # v2.1.9 初始化 chat session manager (持久化到 data_dir/chat_sessions.json)
    try:
        from pathlib import Path

        data_dir_path = Path(info.get("data_dir") or "/var/lib/hotel-frontdesk-pawapp/data")
        chat_session_manager.init_session_manager(data_dir_path)
    except Exception as exc:
        logger.warning(f"[hotel-frontdesk-pawapp] session manager init 失败: {exc}")

    # v2.1.3 自动部署 4 个 AI 助手 (新装即用)
    try:
        from .routes.ai_deploy import auto_deploy_on_plugin_load
        await auto_deploy_on_plugin_load()
    except Exception as exc:
        logger.warning(f"[hotel-frontdesk-pawapp] on_launch auto_deploy 失败: {exc}")
