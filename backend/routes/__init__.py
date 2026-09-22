# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — SDK 路由集合

Phase 2: 把所有业务模块的 endpoints 合并到 **1 个 FastAPI APIRouter**,
        然后 app.include_router(merged_router) 只调一次。

为什么必须合并?
  PawApp 的 register(api) 会用 prefix='/domhotel-suite' 给每个
  sub-router register_http_router,所以 6 个模块 6 次注册 → 5 次
  "prefix already registered" 错误,plugin 加载失败。

  合并后只有 1 个 router,register(api) 调用 1 次,前缀只占用 1 次。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

_BACKEND_PKG_NAME = "__domhotel_suite_backend__"
_THIS_DIR = Path(__file__).resolve().parent

# 共享的合并 router — 所有 routes/*.py 的 endpoint 都 add_route 到这里
merged_router = APIRouter()


def _load_module_from_file(mod_name: str, file_path: Path) -> None:
    """用 importlib 把单文件模块挂到 sys.modules[name]"""
    if not file_path.exists():
        logger.warning(f"[routes] 文件不存在: {file_path}")
        return
    spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = f"{_BACKEND_PKG_NAME}.routes"
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)


_PHASE2_MODULES = [
    ("health", "health.py"),
    ("rooms", "rooms.py"),
    ("work_orders", "work_orders.py"),
    ("reports", "reports.py"),
    ("auth", "auth.py"),              # v2.1.16-hotfix4: 必须先注册,确保 /admin/auth/* 走在 admin.py 的通配之前
    ("admin", "admin.py"),
    ("export", "export.py"),
    ("ai_assistants", "ai_assistants.py"),
    ("ai_deploy", "ai_deploy.py"),    # Phase 7: 一键部署 4 个内置 agent 模板
    ("wecom_diag", "wecom_diag.py"),  # Phase 5: 企微诊断端点
    ("pending", "pending.py"),        # Phase 6: 待确认动作队列
    ("ui", "ui.py"),                  # 静态 UI serve (修复 iframe 白屏)
    ("v3api", "../v3api.py"),         # v3.0: 统一 API 网关
    # ("v3bridge", "v3bridge.py"),    # v3.0: 旧 API 兼容桥接 (暂不启用，避免与旧路由冲突)
    ("staff_portal", "staff_portal.py"),  # v2.2.0: 员工端 API (客房阿姨/工程师傅)
    ("guest_portal", "guest_portal.py"),  # v2.2.0: 客人端 API (报修/需求/评价)
    ("kf", "kf.py"),                      # v2.2.0: 企微客服 API (客人消息回调/绑定/推送)
    ("wizard", "../wizard.py"),            # 整合版: HUB 配置向导 (酒店名称/房态初始化)
    ("checkin", "../checkin.py"),          # 整合版: 前台接待登记 (客人入住登记/退房/导出)
    ("wecom_bot", "wecom_bot.py"),         # 企微群机器人回调 (部门 AI 助手)
    ("dispatch", "dispatch.py"),           # v2.2.2: 派单闭环 Request/Ticket
    ("schedule_mgmt", "schedule_mgmt.py"), # 排班管理 API (楼层/时段/技能)
]

# 1) 加载 _helpers(其他模块依赖它的 now/create_work_order/new_id)
_load_module_from_file(
    f"{_BACKEND_PKG_NAME}.routes._helpers",
    _THIS_DIR / "_helpers.py",
)

# 2) 加载所有业务模块 + 调用它们的 register_routes(None) 触发 @router.get 装饰器
# (每个 routes/*.py 的 endpoint 定义都在 register_routes(app) 函数体内,
#  必须调一次才能让 endpoint 被注册到模块顶层 router)
for short_name, filename in _PHASE2_MODULES:
    full_name = f"{_BACKEND_PKG_NAME}.routes.{short_name}"
    _load_module_from_file(full_name, _THIS_DIR / filename)
    mod = sys.modules.get(full_name)
    if mod and hasattr(mod, "register_routes"):
        # 用一个临时 fake app 让函数能跑(register_routes 末尾会 app.include_router(router))
        # 我们的目的是触发 @router.get 装饰器 — 用一个 dummy object 即可
        class _DummyApp:
            def include_router(self, _router):
                pass  # no-op
        mod.register_routes(_DummyApp())
        logger.info(f"[routes] 已触发 {short_name}.register_routes(),收集 endpoint")

# 3) 把每个模块的内部 router 的 routes 全部迁到 merged_router
for short_name, _ in _PHASE2_MODULES:
    mod_name = f"{_BACKEND_PKG_NAME}.routes.{short_name}"
    mod = sys.modules.get(mod_name)
    if not mod or not hasattr(mod, "router"):
        logger.error(f"[routes] {mod_name} 无 router 属性")
        continue
    src_router: APIRouter = mod.router
    n = 0
    for r in src_router.routes:
        merged_router.routes.append(r)
        n += 1
    logger.info(f"[routes] {short_name}: 合并 {n} 个 endpoint 到 merged_router")


def register_all(app) -> None:
    """把 merged_router 一次性挂到 app。PluginLoader 调 register(api) 时
    PawApp 会遍历 self._routers(只有这 1 个),register_http_router 调 1 次,
    prefix 只占用 1 次。

    v1.3.0: 同时加载 wecom_tools 并注册智能体配置工具 (agent tools)。
    必须在真 PawApp 上调 (app.tool 装饰器), 模块加载阶段的 _DummyApp 没有
    tool 方法, 所以不进 _PHASE2_MODULES 循环 (那里会被 DummyApp 触发)。
    v1.3.2: 追加 hotel_tools (hotel_rooms_setup 智能体生成房间)。
    """
    app.include_router(merged_router)
    logger.info(
        f"[routes] merged_router 已挂载,共 {len(merged_router.routes)} 个 endpoint"
    )
    # v1.3.0: 智能体配置工具 (wecom_status / wecom_config_set / wecom_retry_queue)
    _tools_mod_name = f"{_BACKEND_PKG_NAME}.routes.wecom_tools"
    _load_module_from_file(_tools_mod_name, _THIS_DIR / "wecom_tools.py")
    _tools_mod = sys.modules.get(_tools_mod_name)
    if _tools_mod and hasattr(_tools_mod, "register_tools"):
        try:
            n_tools = _tools_mod.register_tools(app)
            logger.info(f"[routes] 智能体配置工具注册完成: {n_tools} 个 agent tools")
        except Exception as exc:
            logger.warning(f"[routes] 智能体配置工具注册失败(不影响路由): {exc}")

    # v1.3.2: 酒店初始化智能体工具 (hotel_rooms_setup)
    _hotel_tools_name = f"{_BACKEND_PKG_NAME}.routes.hotel_tools"
    _load_module_from_file(_hotel_tools_name, _THIS_DIR / "hotel_tools.py")
    _hotel_mod = sys.modules.get(_hotel_tools_name)
    if _hotel_mod and hasattr(_hotel_mod, "register_tools"):
        try:
            n_hotel = _hotel_mod.register_tools(app)
            logger.info(f"[routes] 酒店初始化工具注册完成: {n_hotel} 个 agent tools")
        except Exception as exc:
            logger.warning(f"[routes] 酒店初始化工具注册失败(不影响路由): {exc}")

    # v1.5.1: 酒店业务操作智能体工具 (对话即工作台: 客人/员工 15 个业务工具)
    _ops_tools_name = f"{_BACKEND_PKG_NAME}.routes.hotel_ops_tools"
    _load_module_from_file(_ops_tools_name, _THIS_DIR / "hotel_ops_tools.py")
    _ops_mod = sys.modules.get(_ops_tools_name)
    if _ops_mod and hasattr(_ops_mod, "register_tools"):
        try:
            n_ops = _ops_mod.register_tools(app)
            logger.info(f"[routes] 酒店业务工具注册完成: {n_ops} 个 agent tools")
        except Exception as exc:
            logger.warning(f"[routes] 酒店业务工具注册失败(不影响路由): {exc}")

    # v1.5.1: 客人自助服务 API (H5页面提交服务请求)
    _guest_svc_name = f"{_BACKEND_PKG_NAME}.routes.guest_service"
    _load_module_from_file(_guest_svc_name, _THIS_DIR / "guest_service.py")
    _guest_mod = sys.modules.get(_guest_svc_name)
    if _guest_mod and hasattr(_guest_mod, "router"):
        try:
            merged_router.include_router(_guest_mod.router)
            logger.info("[routes] 客人自助服务 API 注册完成")
        except Exception as exc:
            logger.warning(f"[routes] 客人自助服务 API 注册失败: {exc}")


__all__ = ["register_all", "merged_router"]
