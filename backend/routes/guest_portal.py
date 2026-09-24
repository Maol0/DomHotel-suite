# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 客人端 API (guest_portal)

给住店客人使用，通常通过企业微信小程序、H5 页面或扫码访问。

路由清单:
  POST /guest/repair              — 客人报修
  GET  /guest/repair/{wo_id}      — 查报修进度
  POST /guest/repair/{wo_id}/rate — 评价服务
  POST /guest/request             — 客人需求（毛巾/牙刷/加床等）
  GET  /guest/my-room             — 查我的房间信息（按房间号+姓名验证）
  GET  /guest/hotel-info          — 酒店基本信息（公开）

鉴权:
  - 客人通过 room_no + guest_name 验证身份（轻量级，无需登录）
  - 企微小程序可通过 openid 绑定房间
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import RedirectResponse

from .. import data_layer
from .. import auth
from .. import guest_profile
from ..wecom_sync import safe_sync
from ._helpers import now, new_id
from .work_order_svc import svc_create_work_order, svc_complete_work_order

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_guest(room_no: str, guest_name: str) -> Dict[str, Any]:
    """验证客人身份: 房间号 + 姓名匹配在住客人

    Returns: room dict if valid, raises HTTPException if not
    """
    if not room_no or not guest_name:
        raise HTTPException(status_code=400, detail="房间号和姓名必填")

    rooms = data_layer.load_table("rooms")
    for r in rooms:
        if r.get("room_no") == room_no:
            if r.get("status") != "在住":
                raise HTTPException(status_code=400, detail=f"房间 {room_no} 当前不是在住状态")
            # 姓名模糊匹配（支持只填姓氏）
            stored_name = r.get("guest_name", "")
            if not stored_name:
                raise HTTPException(status_code=400, detail=f"房间 {room_no} 未登记客人信息")
            if guest_name not in stored_name and stored_name not in guest_name:
                raise HTTPException(status_code=403, detail="姓名与房间登记信息不符")
            return r
    raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")


def register_routes(app) -> None:
    """注册客人端路由"""

    # ─────────────────────────────────────────
    # POST /guest/repair — 客人报修
    # ─────────────────────────────────────────

    @router.post("/guest/repair")
    async def guest_repair(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """客人提交报修

        Body:
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
          category: 故障类别（可选：空调/电视/卫浴/门锁/照明/网络/其他）
          description: 故障描述（必填）
          urgency: 紧急程度（可选：urgent/normal，默认 normal）
          phone: 联系电话（可选）
          photos: 照片 URL 列表（可选）
        """
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()
        category = payload.get("category", "其他")
        description = payload.get("description", "").strip()
        urgency = payload.get("urgency", "normal")
        phone = payload.get("phone", "")
        photos = payload.get("photos", [])

        if not description:
            raise HTTPException(status_code=400, detail="故障描述必填")

        # 验证客人身份
        room = _verify_guest(room_no, guest_name)

        # 构建完整描述
        full_desc = f"[{category}] {description}"
        if phone:
            full_desc += f" (联系电话: {phone})"

        # v1.6.1 统一收口: 委托 service (同步+通知+客人回执+自动派单; photos 走 extra 落库)
        _res = await svc_create_work_order(
            room_no=room_no,
            work_type="维修",
            description=full_desc,
            priority=urgency if urgency in ("urgent", "high", "normal", "low") else "normal",
            reporter=f"客人:{guest_name}",
            target_dept="frontdesk",  # v2.4-intake: 客人报修统一前台收单
            data_source="guest",
            operator=f"guest:{room_no}:{guest_name}",
            extra=({"photo_urls": photos} if photos else None),
            auto_dispatch=False,
        )
        wo = _res["work_order"]

        # v1.4.1: 更新客人档案
        guest_profile.update_guest_profile(wo, room_no=room_no, guest_name=guest_name)

        return {
            "ok": True,
            "work_order": {
                "wo_id": wo["wo_id"],
                "room_no": room_no,
                "category": category,
                "description": description,
                "status": wo["status"],
                "created_at": wo["created_at"],
            },
            "message": "报修已提交，我们会尽快安排维修",
        }

    # ─────────────────────────────────────────
    # GET /guest/repair/{wo_id} — 查报修进度
    # ─────────────────────────────────────────

    @router.get("/guest/repair/{wo_id}")
    async def guest_repair_status(
        wo_id: str = "",
        room_no: str = "",
        guest_name: str = "",
    ) -> Dict[str, Any]:
        """查询报修进度

        Query params:
          room_no: 房间号（必填，用于验证身份）
          guest_name: 客人姓名（必填）
        """
        if not room_no or not guest_name:
            raise HTTPException(status_code=400, detail="room_no 和 guest_name 必填")

        # 验证客人身份
        _verify_guest(room_no, guest_name)

        # 查工单
        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                # 安全检查: 只能查自己房间的工单
                if w.get("room_no") != room_no:
                    raise HTTPException(status_code=403, detail="无权查看其他房间的工单")

                # 状态映射为客人友好的描述
                status_desc = {
                    "pending_confirm": "已提交，等待处理",
                    "pending": "已确认，等待派单",
                    "assigned": "已派单，维修人员即将到达",
                    "in_progress": "维修中",
                    "done": "已完成",
                    "rejected": "已取消",
                }.get(w.get("status", ""), w.get("status", ""))

                result = {
                    "wo_id": w["wo_id"],
                    "room_no": w["room_no"],
                    "work_type": w.get("work_type", ""),
                    "description": w.get("description", ""),
                    "status": w.get("status", ""),
                    "status_desc": status_desc,
                    "created_at": w.get("created_at", ""),
                    "assignee": w.get("assignee", ""),
                }
                if w.get("status") == "done":
                    result["completed_at"] = w.get("completed_at", "")
                    result["result_note"] = w.get("result_note", "")

                return {"ok": True, "repair": result}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /guest/repair/{wo_id}/rate — 评价服务
    # ─────────────────────────────────────────

    @router.post("/guest/repair/{wo_id}/rate")
    async def rate_repair(
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """评价维修服务

        Body:
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
          rating: 评分 1-5（必填）
          comment: 评价内容（可选）
        """
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()
        rating = payload.get("rating", 0)
        comment = payload.get("comment", "")

        if not room_no or not guest_name:
            raise HTTPException(status_code=400, detail="room_no 和 guest_name 必填")
        if not isinstance(rating, (int, float)) or rating < 1 or rating > 5:
            raise HTTPException(status_code=400, detail="评分必须是 1-5 的整数")

        # 验证客人身份
        _verify_guest(room_no, guest_name)

        # 查工单
        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                if w.get("room_no") != room_no:
                    raise HTTPException(status_code=403, detail="无权评价其他房间的工单")
                if w.get("status") != "done":
                    raise HTTPException(status_code=400, detail="只能评价已完成的工单")
                if w.get("guest_rating"):
                    raise HTTPException(status_code=400, detail="该工单已评价，不能重复评价")

                w["guest_rating"] = int(rating)
                w["guest_comment"] = comment
                w["rated_at"] = now()
                w["updated_at"] = now()
                w.setdefault("history", []).append({
                    "time": now(),
                    "operator": f"客人:{guest_name}",
                    "note": f"评价: {'⭐' * int(rating)} {comment}" if comment else f"评价: {'⭐' * int(rating)}",
                })
                data_layer.save_table("work_orders", wos)
                await safe_sync("work_orders", w)
                return {"ok": True, "message": "感谢您的评价！"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /guest/request — 客人需求
    # ─────────────────────────────────────────

    @router.post("/guest/request")
    async def guest_request(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """客人提交需求（毛巾/牙刷/加床/送物等）

        Body:
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
          request_type: 需求类型（必填：送物/换房/加床/其他）
          items: 物品列表，如 ["毛巾x2", "矿泉水x3"]（送物时必填）
          description: 详细描述（可选）
          phone: 联系电话（可选）
        """
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()
        request_type = payload.get("request_type", "").strip()
        items = payload.get("items", [])
        description = payload.get("description", "")
        phone = payload.get("phone", "")

        if not request_type:
            raise HTTPException(status_code=400, detail="需求类型必填")

        # 验证客人身份
        room = _verify_guest(room_no, guest_name)

        # 构建描述
        if request_type == "送物" and items:
            items_str = ", ".join(items) if isinstance(items, list) else str(items)
            full_desc = f"送物需求: {items_str}"
        else:
            full_desc = f"[{request_type}] {description}" if description else f"{request_type}需求"
        if phone:
            full_desc += f" (联系电话: {phone})"

        # 根据类型决定工单类型和部门
        work_type_map = {
            "送物": ("送物", "frontdesk"),
            "换房": ("换房", "frontdesk"),
            "加床": ("送物", "frontdesk"),
        }
        work_type, target_dept = work_type_map.get(request_type, ("送物", "frontdesk"))

        # v1.6.1 统一收口: 委托 service (同步+通知+客人回执+自动派单)
        _res = await svc_create_work_order(
            room_no=room_no,
            work_type=work_type,
            description=full_desc,
            priority="normal",
            reporter=f"客人:{guest_name}",
            target_dept=target_dept,
            data_source="guest",
            operator=f"guest:{room_no}:{guest_name}",
            auto_dispatch=False,
        )
        wo = _res["work_order"]

        # v1.4.1: 更新客人档案
        guest_profile.update_guest_profile(wo, room_no=room_no, guest_name=guest_name)

        return {
            "ok": True,
            "work_order": {
                "wo_id": wo["wo_id"],
                "room_no": room_no,
                "request_type": request_type,
                "status": wo["status"],
                "created_at": wo["created_at"],
            },
            "message": "需求已提交，我们会尽快处理",
        }

    # ─────────────────────────────────────────
    # GET /guest/my-room — 查我的房间信息
    # ─────────────────────────────────────────

    @router.get("/guest/my-room")
    async def guest_my_room(
        room_no: str = "",
        guest_name: str = "",
    ) -> Dict[str, Any]:
        """查询我的房间信息

        Query params:
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
        """
        room = _verify_guest(room_no, guest_name)

        # 查该房间的进行中工单
        wos = data_layer.load_table("work_orders")
        active_wos = [
            {
                "wo_id": w["wo_id"],
                "work_type": w.get("work_type", ""),
                "status": w.get("status", ""),
                "created_at": w.get("created_at", ""),
            }
            for w in wos
            if w.get("room_no") == room_no
            and w.get("status") not in ("done", "rejected")
        ]

        return {
            "ok": True,
            "room": {
                "room_no": room["room_no"],
                "floor": room.get("floor", 0),
                "room_type": room.get("room_type", ""),
                "guest_name": room.get("guest_name", ""),
                "checkin_time": room.get("checkin_time", ""),
                "checkin_days": room.get("checkin_days", 0),
            },
            "active_repairs": active_wos,
        }

    # ─────────────────────────────────────────
    # GET /guest/hotel-info — 酒店基本信息
    # ─────────────────────────────────────────

    @router.get("/guest/hotel-info")
    async def hotel_info() -> Dict[str, Any]:
        """返回酒店基本信息（公开接口，无需登录）"""
        rooms = data_layer.load_table("rooms")
        total = len(rooms)
        available = sum(1 for r in rooms if r.get("status") == "空房")
        occupied = sum(1 for r in rooms if r.get("status") == "在住")

        return {
            "ok": True,
            "hotel_name": "酒店房务工作台",  # TODO: 从配置读取
            "total_rooms": total,
            "available_rooms": available,
            "occupied_rooms": occupied,
            "services": [
                {"name": "报修服务", "desc": "房间设施故障维修"},
                {"name": "送物服务", "desc": "毛巾/牙刷/矿泉水等"},
                {"name": "换房申请", "desc": "房间更换"},
            ],
            "contact": {
                "front_desk": "拨打 0 或前台分机",
                "emergency": "拨打 110/120",
            },
        }

    # ─────────────────────────────────────────
    # POST /h5/guest/request — H5 客人快速提交（不校验在住状态）
    # ─────────────────────────────────────────

    @router.post("/h5/guest/request")
    async def h5_guest_request(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """H5 客人端快速提交需求，仅校验房间号存在，不强制要求在住"""
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()
        guest_id = payload.get("guest_id", "").strip()
        work_type = payload.get("work_type", "维修").strip()
        description = payload.get("description", "").strip()
        phone = payload.get("phone", "").strip()
        urgency = payload.get("urgency", "normal").strip()

        if not room_no:
            raise HTTPException(status_code=400, detail="房间号必填")
        if not description:
            raise HTTPException(status_code=400, detail="需求描述必填")

        # v1.4.1: 如果没有 guest_id，自动生成
        if not guest_id:
            guest_id = str(uuid.uuid4())

        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        if not room:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")

        full_desc = description
        if phone:
            full_desc += f" (联系电话: {phone})"

        dept_map = {
            "维修": "engineering",
            "清洁": "housekeeping",
            "送物": "frontdesk",
            "送餐": "frontdesk",
            "前台服务": "frontdesk",
            "其他": "frontdesk",
        }

        # v1.6.1 统一收口: guest_id/guest_name 建单时并入, 通知+客人回执由 svc 统一发一次
        _extra = {"guest_id": guest_id}
        if guest_name:
            _extra["guest_name"] = guest_name
        _res = await svc_create_work_order(
            room_no=room_no,
            work_type=work_type,
            description=full_desc,
            priority=urgency if urgency in ("urgent", "high", "normal", "low") else "normal",
            reporter=f"H5客人:{guest_name or '匿名'}",
            target_dept="frontdesk",  # v2.4-intake: 前台收单
            data_source="guest_h5",
            operator=f"h5_guest:{room_no}",
            extra=_extra,
            auto_dispatch=False,
        )
        wo = _res["work_order"]

        # v1.4.1: 更新客人档案
        guest_profile.update_guest_profile(wo, room_no=room_no, guest_name=guest_name)

        return {
            "ok": True,
            "guest_id": guest_id,
            "work_order": {
                "wo_id": wo["wo_id"],
                "room_no": room_no,
                "work_type": work_type,
                "status": wo["status"],
                "created_at": wo["created_at"],
            },
            "message": "需求已提交，我们会尽快处理",
        }

    # ─────────────────────────────────────────
    # POST /guest/orders/query — 客人查询我的需求单
    # ─────────────────────────────────────────
    @router.post("/guest/orders/query")
    async def guest_orders_query(payload: dict = Body(default_factory=dict)):
        """客人通过 guest_id 查询自己的需求单列表

        v1.4.1: 仅通过 guest_id 查询，不关联房间号，保护客人隐私。
        工单上的 room_no 仅供员工端使用，客人端不返回。
        """
        guest_id = str(payload.get("guest_id", "")).strip()

        if not guest_id:
            raise HTTPException(400, detail="guest_id 必填")

        wos = data_layer.load_table("work_orders")
        result = []

        for wo in wos:
            if wo.get("guest_id") != guest_id:
                continue
            result.append({
                "wo_id": wo.get("wo_id"),
                "work_type": wo.get("work_type"),
                "description": wo.get("description"),
                "status": wo.get("status"),
                "priority": wo.get("priority"),
                "assignee": wo.get("assignee") or "未分配",
                "created_at": wo.get("created_at"),
                "completed_at": wo.get("completed_at"),
                "guest_rating": wo.get("guest_rating"),
                "guest_comment": wo.get("guest_comment"),
            })
        result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {"ok": True, "guest_id": guest_id, "count": len(result), "orders": result}

    # ─────────────────────────────────────────
    # GET /guest/orders/{wo_id} — 单条需求详情
    # ─────────────────────────────────────────
    @router.get("/guest/orders/{wo_id}")
    async def guest_order_detail(wo_id: str, guest_id: str = ""):
        """客人查询单条需求详情（通过 guest_id 验证身份）"""
        if not guest_id:
            raise HTTPException(400, detail="guest_id 必填")
        wos = data_layer.load_table("work_orders")
        wo = next((w for w in wos if w.get("wo_id") == wo_id), None)
        if not wo or wo.get("guest_id") != guest_id:
            raise HTTPException(404, detail="工单不存在")
        return {
            "ok": True,
            "order": {
                "wo_id": wo.get("wo_id"),
                "work_type": wo.get("work_type"),
                "description": wo.get("description"),
                "status": wo.get("status"),
                "priority": wo.get("priority"),
                "assignee": wo.get("assignee") or "未分配",
                "created_at": wo.get("created_at"),
                "completed_at": wo.get("completed_at"),
                "guest_rating": wo.get("guest_rating"),
                "guest_comment": wo.get("guest_comment"),
                "timeline": wo.get("timeline", {}),
            },
        }

    # ─────────────────────────────────────────
    # POST /guest/orders/{wo_id}/confirm — 客人确认服务完成
    # ─────────────────────────────────────────
    @router.post("/guest/orders/{wo_id}/confirm")
    async def guest_order_confirm(wo_id: str, payload: dict = Body(default_factory=dict)):
        """客人确认服务已完成（通过 guest_id 验证身份）"""
        guest_id = str(payload.get("guest_id", "")).strip()
        if not guest_id:
            raise HTTPException(400, detail="guest_id 必填")
        wos = data_layer.load_table("work_orders")
        wo = next((w for w in wos if w.get("wo_id") == wo_id), None)
        if not wo or wo.get("guest_id") != guest_id:
            raise HTTPException(404, detail="工单不存在")
        if wo.get("status") == "done":
            return {"ok": True, "message": "工单已完成，无需重复确认"}
        if wo.get("status") not in ("processing", "pending", "assigned"):
            raise HTTPException(400, detail=f"当前状态 {wo.get('status')} 不允许客人确认")
        # v1.6.1 统一收口: 委托 svc (完成通知+客人回执+房态还原+耗时+智能表格同步; force 放宽状态机)
        _res = await svc_complete_work_order(
            wo_id, operator=f"guest:{guest_id}",
            result_note="客人确认完成", force=True,
        )
        if not _res.get("ok"):
            raise HTTPException(400, detail=_res.get("error", "完成确认失败"))
        return {"ok": True, "message": "已确认服务完成，感谢您的使用"}

    # ─────────────────────────────────────────
    # POST /guest/orders/{wo_id}/rate — 客人评价
    # ─────────────────────────────────────────
    @router.post("/guest/orders/{wo_id}/rate")
    async def guest_order_rate(wo_id: str, payload: dict = Body(default_factory=dict)):
        """客人对已完成的工单进行评价（通过 guest_id 验证身份）"""
        guest_id = str(payload.get("guest_id", "")).strip()
        rating = payload.get("rating")
        comment = str(payload.get("comment", "")).strip()
        if not guest_id:
            raise HTTPException(400, detail="guest_id 必填")
        if rating is None or not isinstance(rating, int) or not (1 <= rating <= 5):
            raise HTTPException(400, detail="评分必须是 1-5 的整数")

        wos = data_layer.load_table("work_orders")
        wo = next((w for w in wos if w.get("wo_id") == wo_id), None)
        if not wo or wo.get("guest_id") != guest_id:
            raise HTTPException(404, detail="工单不存在")
        if wo.get("status") != "done":
            raise HTTPException(400, detail="只有已完成的工单才能评价")
        wo["guest_rating"] = rating
        wo["guest_comment"] = comment
        wo["updated_at"] = now()
        data_layer.save_table("work_orders", wos)
        try:
            await safe_sync("work_orders", wo)
        except Exception as exc:
            logger.warning("[guest_order_rate] safe_sync 失败: %s", exc)
        return {"ok": True, "message": "评价已提交，感谢您的反馈"}

    # ─────────────────────────────────────────
    # v1.4.1: 微信公众号 OAuth 获取客人 OpenID
    # ─────────────────────────────────────────
    @router.get("/guest/oauth/init")
    async def guest_oauth_init(request: Request, room: str = ""):
        """触发微信公众号 OAuth 授权

        客人在微信内打开时自动跳转，获取 OpenID 作为 guest_id。
        如果未配置公众号，返回 400 让前端 fallback 到随机 UUID。
        """
        import os
        from pathlib import Path

        # 读取公众号配置
        cfg_path = Path("/app/working/dompaw-data-backup") / "wecom_runtime_config.json"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = __import__('json').loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        appid = cfg.get("WECHAT_MP_APPID") or os.environ.get("WECHAT_MP_APPID", "")
        if not appid:
            # 未配置公众号，返回 400 让前端 fallback
            raise HTTPException(400, detail="未配置微信公众号 AppID")

        base_url = cfg.get("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")
        if not base_url:
            base_url = f"{request.url.scheme}://{request.url.netloc}"

        redirect_uri = f"{base_url}/api/domhotel-suite/guest/oauth/callback"
        if room:
            redirect_uri += f"?room={quote(room)}"

        oauth_url = (
            f"https://open.weixin.qq.com/connect/oauth2/authorize"
            f"?appid={appid}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code"
            f"&scope=snsapi_base"
            f"&state=guest#wechat_redirect"
        )
        return RedirectResponse(url=oauth_url)

    @router.get("/guest/oauth/callback")
    async def guest_oauth_callback(request: Request, code: str = "", state: str = "", room: str = ""):
        """微信公众号 OAuth 回调，用 code 换取 OpenID"""
        import os
        from pathlib import Path

        h5_url = "/api/domhotel-suite/ui/guest-h5.html"

        if not code:
            return RedirectResponse(url=f"{h5_url}?error=oauth_failed")

        # 读取公众号配置
        cfg_path = Path("/app/working/dompaw-data-backup") / "wecom_runtime_config.json"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = __import__('json').loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        appid = cfg.get("WECHAT_MP_APPID") or os.environ.get("WECHAT_MP_APPID", "")
        secret = cfg.get("WECHAT_MP_SECRET") or os.environ.get("WECHAT_MP_SECRET", "")

        if not appid or not secret:
            return RedirectResponse(url=f"{h5_url}?error=oauth_not_configured")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://api.weixin.qq.com/sns/oauth2/access_token",
                    params={
                        "appid": appid,
                        "secret": secret,
                        "code": code,
                        "grant_type": "authorization_code",
                    },
                )
                data = r.json()

            if data.get("errcode"):
                logger.warning("[guest_oauth] 获取 access_token 失败: %s", data)
                return RedirectResponse(url=f"{h5_url}?error=oauth_failed&errcode={data.get('errcode')}")

            openid = data.get("openid", "")
            if not openid:
                logger.warning("[guest_oauth] 返回没有 openid: %s", data)
                return RedirectResponse(url=f"{h5_url}?error=no_openid")

            # 跳转回 H5 页面，携带 openid
            separator = "&" if "?" in h5_url else "?"
            redirect_url = f"{h5_url}{separator}guest_id={quote(openid)}"
            if room:
                redirect_url += f"&room={quote(room)}"
            return RedirectResponse(url=redirect_url)

        except Exception as exc:
            logger.warning("[guest_oauth] OAuth 异常: %s", exc)
            return RedirectResponse(url=f"{h5_url}?error=oauth_exception")

    # ─────────────────────────────────────────
    # POST /guest/returning-check — 回访客人识别
    # ─────────────────────────────────────────
    @router.post("/guest/returning-check")
    async def guest_returning_check(payload: dict = Body(default_factory=dict)):
        """检查 guest_id (OpenID) 是否有历史记录（回访客人识别）

        客人首次打开 H5 页面时调用，如果有历史记录则显示历史工单入口。
        """
        guest_id = str(payload.get("guest_id", "")).strip()
        if not guest_id:
            return {"ok": True, "returning": False}

        # 查找是否有该 guest_id 的历史工单
        wos = data_layer.load_table("work_orders")
        my_wos = [w for w in wos if w.get("guest_id") == guest_id]
        if not my_wos:
            return {"ok": True, "returning": False}

        my_wos.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        # 统计
        total = len(my_wos)
        done = sum(1 for w in my_wos if w.get("status") == "done")
        active = total - done
        last_visit = my_wos[0].get("created_at", "")[:10] if my_wos else ""

        return {
            "ok": True,
            "returning": True,
            "total_orders": total,
            "completed_orders": done,
            "active_orders": active,
            "last_visit": last_visit,
            "message": f"欢迎回来！您有 {total} 条历史需求记录。",
        }

    # ─────────────────────────────────────────
    # POST /guest/history — 完整历史（工单 + 会话）
    # ─────────────────────────────────────────
    @router.post("/guest/history")
    async def guest_history(payload: dict = Body(default_factory=dict)):
        """获取客人的完整历史：工单列表 + 客服会话摘要

        guest_id = 微信 OpenID，作为客人永久身份标识。
        """
        guest_id = str(payload.get("guest_id", "")).strip()
        if not guest_id:
            raise HTTPException(400, detail="guest_id 必填")

        # 历史工单
        wos = data_layer.load_table("work_orders")
        my_wos = []
        for w in wos:
            if w.get("guest_id") == guest_id:
                my_wos.append({
                    "wo_id": w.get("wo_id"),
                    "work_type": w.get("work_type"),
                    "description": (w.get("description") or "")[:80],
                    "status": w.get("status"),
                    "created_at": w.get("created_at"),
                    "completed_at": w.get("completed_at", ""),
                    "guest_rating": w.get("guest_rating"),
                    "guest_comment": w.get("guest_comment", ""),
                })
        my_wos.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        # 客服会话记录（通过 external_userid 从 KF 日志查询）
        # 如果 guest_id 是 OpenID，需要找到对应的 external_userid
        kf_messages = []
        from ..wecom_kf import find_customer_by_openid
        customer = find_customer_by_openid(guest_id)
        if customer:
            ext_uid = customer.get("external_userid", "")
            if ext_uid:
                from ..wecom_kf import get_kf_session_history
                kf_messages = get_kf_session_history(ext_uid)

        return {
            "ok": True,
            "guest_id": guest_id,
            "orders": my_wos,
            "orders_count": len(my_wos),
            "conversations": kf_messages[-50:] if kf_messages else [],  # 最近 50 条
            "conversations_count": len(kf_messages),
        }

    app.include_router(router)
    logger.info("[routes/guest_portal] 已注册 14 个客人端路由 + 微信 OAuth")
