# -*- coding: utf-8 -*-
"""DomHotel Suite — 前台接待登记 (checkin) 路由

整合版新增模块 (移植自 DomAI visitor-kiosk 的"客人登记 + 员工后台"双入口理念):
  - 客人入住登记 (姓名/电话/证件/人数/房号/备注)
  - 登记列表 (在住/已退房/已取消 筛选)
  - 退房 / 取消登记
  - CSV 导出
  - 与房态看板联动: 登记时可将房间置为"在住", 退房时置为"待打扫"

数据存储: data_layer 的 checkins 表 (+ checkins_log 审计)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from .. import data_layer
from .. import auth
from ..wecom_sync import safe_sync
from .work_order_svc import svc_create_work_order
from ..wecom_kf import sync_checkout_from_pms

logger = logging.getLogger("domhotel-suite.checkin")

router = APIRouter()

_ID_TYPES = ("身份证", "护照", "港澳通行证", "台胞证", "其他")
_STATUS_IN_HOUSE = "in_house"
_STATUS_CHECKED_OUT = "checked_out"
_STATUS_CANCELLED = "cancelled"


def _find_checkin(checkins: List[Dict[str, Any]], checkin_id: str) -> Dict[str, Any]:
    for c in checkins:
        if c.get("id") == checkin_id:
            return c
    raise HTTPException(status_code=404, detail=f"登记记录 {checkin_id} 不存在")


def _apply_room_state(room_no: str, guest_name: str, guest_phone: str,
                      action: str) -> Dict[str, Any]:
    """登记/退房时联动房态 (直接写 rooms 表, 语义与 rooms.py 一致)

    action: checkin → 空房→在住; checkout → 在住→待打扫 (清住客)
    返回 {changed: bool, room_no, new_status, reason}
    """
    rooms = data_layer.load_table("rooms")
    room = next((r for r in rooms if r.get("room_no") == room_no), None)
    if not room:
        return {"changed": False, "room_no": room_no, "reason": "房间不存在"}
    now = data_layer.now_str()
    if action == "checkin":
        if room.get("status") not in ("空房", "已清洁", "干净"):
            return {"changed": False, "room_no": room_no,
                    "new_status": room.get("status"),
                    "reason": f"房间当前状态为 {room.get('status')}，未联动房态"}
        old = room.get("status")
        room["status"] = "在住"
        room["guest_name"] = guest_name
        room["guest_phone"] = guest_phone
        room["checkin_time"] = now
        room["checkin_days"] = 1
        room["updated_at"] = now
        room["updated_by"] = "前台接待"
        room.setdefault("history", []).append({
            "time": now, "from": old, "to": "在住", "operator": "frontdesk",
            "note": f"前台登记: {guest_name}",
        })
        data_layer.save_table("rooms", rooms)
        return {"changed": True, "room_no": room_no, "new_status": "在住"}
    if action == "checkout":
        if room.get("status") == "在住":
            old = room.get("status")
            room["status"] = "待打扫"
            room["updated_at"] = now
            room["updated_by"] = "前台接待"
            room.setdefault("history", []).append({
                "time": now, "from": old, "to": "待打扫", "operator": "frontdesk",
                "note": f"前台退房: {room.get('guest_name', '')}",
            })
            for k in ("guest_name", "guest_phone", "checkin_time", "checkin_days"):
                room.pop(k, None)
            data_layer.save_table("rooms", rooms)
            return {"changed": True, "room_no": room_no, "new_status": "待打扫"}
        return {"changed": False, "room_no": room_no,
                "new_status": room.get("status"),
                "reason": f"房间状态 {room.get('status')} 非在住，未联动"}
    return {"changed": False, "room_no": room_no, "reason": "未知操作"}


def register_routes(app) -> None:
    """routes/__init__.py 的合并 router 机制要求: 触发 @router 装饰器"""

    # ─────────────────────────────────────────────
    # 登记列表
    # ─────────────────────────────────────────────
    @router.get("/checkins")
    async def list_checkins(
        status: str = "",
        limit: int = 200,
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> List[Dict[str, Any]]:
        rows = data_layer.load_table("checkins")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        return rows[:max(1, min(limit, 1000))]

    # ─────────────────────────────────────────────
    # 今日统计 (前台接待 tab 顶部卡片)
    # ─────────────────────────────────────────────
    @router.get("/checkins/stats")
    async def checkin_stats(
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        rows = data_layer.load_table("checkins")
        today = data_layer.today_str()
        today_rows = [r for r in rows if str(r.get("created_at", "")).startswith(today)]
        return {
            "ok": True,
            "today_total": len(today_rows),
            "today_in_house": sum(1 for r in today_rows if r.get("status") == _STATUS_IN_HOUSE),
            "today_checked_out": sum(1 for r in today_rows if r.get("status") == _STATUS_CHECKED_OUT),
            "in_house_total": sum(1 for r in rows if r.get("status") == _STATUS_IN_HOUSE),
            "total": len(rows),
        }

    # ─────────────────────────────────────────────
    # 新建登记 (可选联动房态)
    # ─────────────────────────────────────────────
    @router.post("/checkins")
    async def create_checkin(
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        guest_name = str(payload.get("guest_name", "")).strip()
        if not guest_name:
            raise HTTPException(status_code=400, detail="guest_name (客人姓名) 必填")
        phone = str(payload.get("phone", "")).strip()
        id_type = str(payload.get("id_type", "身份证")).strip()
        if id_type not in _ID_TYPES:
            id_type = "其他"
        id_no = str(payload.get("id_no", "")).strip()
        if id_type in ("身份证", "护照") and not id_no:
            raise HTTPException(status_code=400, detail=f"{id_type}号必填 (住宿登记合规要求)")
        try:
            party_size = max(1, int(payload.get("party_size", 1)))
        except (TypeError, ValueError):
            party_size = 1
        room_no = str(payload.get("room_no", "")).strip()
        notes = str(payload.get("notes", "")).strip()[:500]
        link_room = bool(payload.get("link_room", True))

        operator = sess.get("name") or sess.get("staff_id") or "frontdesk"
        now = data_layer.now_str()
        checkin_id = data_layer.new_id("CI")

        room_result: Dict[str, Any] = {"changed": False}
        if room_no and link_room:
            room_result = _apply_room_state(room_no, guest_name, phone, "checkin")
        elif room_no and not link_room:
            room_result = {"changed": False, "room_no": room_no,
                           "reason": "未选择联动房态"}

        row = {
            "id": checkin_id,
            "guest_name": guest_name,
            "phone": phone,
            "id_type": id_type,
            "id_no": id_no,
            "party_size": party_size,
            "room_no": room_no,
            "notes": notes,
            "status": _STATUS_IN_HOUSE,
            "created_at": now,
            "created_by": operator,
            "checkin_time": now,
            "checkout_time": "",
            "room_linked": bool(room_result.get("changed")),
        }
        rows = data_layer.load_table("checkins")
        rows.append(row)
        data_layer.save_table("checkins", rows)
        data_layer.append_log("checkins_log", {
            "record_key": f"{checkin_id}",
            "action": "create",
            "guest_name": guest_name,
            "room_no": room_no,
            "operator": operator,
            "time": now,
        })
        # 整合版: 联动了房态则同步推企微 (与 rooms.py 入住行为一致: rooms_log + guests_log)
        if room_result.get("changed"):
            try:
                await safe_sync("rooms_log", {
                    "record_key": f"{room_no}-{now}",
                    "room_no": room_no,
                    "status": "在住",
                    "guest_name": guest_name,
                    "guest_phone": phone,
                    "checkin_time": now,
                    "updated_at": now,
                    "updated_by": operator,
                    "note": f"前台登记: {guest_name}",
                })
                await safe_sync("guests_log", {
                    "record_key": f"{room_no}-{now}",
                    "room_no": room_no,
                    "guest_name": guest_name,
                    "guest_phone": phone,
                    "checkin_time": now,
                    "checkout_time": "",
                    "checkin_days": 1,
                })
            except Exception as exc:
                logger.warning("[checkin] 推企微失败 (不影响登记): %s", exc)
        logger.info("[checkin] 新登记 %s %s → %s (room_linked=%s)",
                    checkin_id, guest_name, room_no or "未分配", room_result.get("changed"))
        msg = f"登记成功: {guest_name}"
        if room_result.get("changed"):
            msg += f"，房间 {room_no} 已置为在住"
        elif room_no:
            msg += f"（房间 {room_no} 未联动: {room_result.get('reason', '')}）"
        return {"ok": True, "checkin": row, "room": room_result, "message": msg}

    # ─────────────────────────────────────────────
    # 退房
    # ─────────────────────────────────────────────
    @router.post("/checkins/{checkin_id}/checkout")
    async def checkout_checkin(
        checkin_id: str = "",
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        rows = data_layer.load_table("checkins")
        row = _find_checkin(rows, checkin_id)
        if row.get("status") != _STATUS_IN_HOUSE:
            raise HTTPException(status_code=400,
                                detail=f"当前状态 {row.get('status')} 不可退房")
        operator = sess.get("name") or sess.get("staff_id") or "frontdesk"
        now = data_layer.now_str()
        row["status"] = _STATUS_CHECKED_OUT
        row["checkout_time"] = now
        row["updated_at"] = now
        row["updated_by"] = operator
        room_result = {"changed": False}
        if row.get("room_no") and row.get("room_linked"):
            room_result = _apply_room_state(row["room_no"], "", "", "checkout")
        data_layer.save_table("checkins", rows)
        # v2.4: 退房反向解绑微信客人绑定 (修: 看板/前台退房后微信仍认旧房)
        try:
            sync_checkout_from_pms(
                row.get("room_no") or "", phone=row.get("phone") or "",
                name=row.get("guest_name") or "",
            )
        except Exception as e:
            logger.warning("[checkin] 退房反向解绑微信失败(不影响退房): %s", e)
        data_layer.append_log("checkins_log", {
            "record_key": checkin_id,
            "action": "checkout",
            "guest_name": row.get("guest_name"),
            "room_no": row.get("room_no"),
            "operator": operator,
            "time": now,
        })
        # 整合版: 联动了房态则同步推企微 (与 rooms.py 退房行为一致)
        if room_result.get("changed"):
            _room_no = row.get("room_no") or ""
            try:
                await safe_sync("rooms_log", {
                    "record_key": f"{_room_no}-{now}",
                    "room_no": _room_no,
                    "status": "待打扫",
                    "guest_name": "",
                    "guest_phone": "",
                    "checkin_time": "",
                    "updated_at": now,
                    "updated_by": operator,
                    "note": f"前台退房: {row.get('guest_name', '')}",
                })
                await safe_sync("guests_log", {
                    "record_key": f"{_room_no}-{now}",
                    "room_no": _room_no,
                    "guest_name": row.get("guest_name", ""),
                    "guest_phone": row.get("phone", ""),
                    "checkin_time": row.get("checkin_time", ""),
                    "checkout_time": now,
                    "checkin_days": 0,
                })
            except Exception as exc:
                logger.warning("[checkin] 退房推企微失败 (不影响退房): %s", exc)
            # v1.6.1: 退房即建清洁工单 (pending 看板/AI派单; 与 rooms.py 退房一致, svc 统一同步+通知)
            try:
                _res = await svc_create_work_order(
                    room_no=_room_no, work_type="清洁",
                    description=f"退房后清洁 (原住客 {row.get('guest_name', '') or '(无)'})",
                    priority="normal", reporter=operator, target_dept="housekeeping",
                    data_source="manual", operator=operator,
                    auto_dispatch=False,
                )
                _wo = _res.get("work_order")
                if _wo:
                    room_result["clean_work_order_id"] = _wo.get("wo_id", "")
            except Exception as we:
                logger.warning("[checkin] 退房建清洁工单失败 (不影响退房): %s", we)
        msg = f"{row.get('guest_name')} 已退房"
        if room_result.get("changed"):
            msg += f"，房间 {row.get('room_no')} 已置为待打扫，已建清洁工单"
        return {"ok": True, "checkin": row, "room": room_result, "message": msg}

    # ─────────────────────────────────────────────
    # 取消登记
    # ─────────────────────────────────────────────
    @router.post("/checkins/{checkin_id}/cancel")
    async def cancel_checkin(
        checkin_id: str = "",
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        rows = data_layer.load_table("checkins")
        row = _find_checkin(rows, checkin_id)
        if row.get("status") == _STATUS_CHECKED_OUT:
            raise HTTPException(status_code=400, detail="已退房记录不可取消")
        operator = sess.get("name") or sess.get("staff_id") or "frontdesk"
        now = data_layer.now_str()
        row["status"] = _STATUS_CANCELLED
        row["updated_at"] = now
        row["updated_by"] = operator
        data_layer.save_table("checkins", rows)
        data_layer.append_log("checkins_log", {
            "record_key": checkin_id,
            "action": "cancel",
            "guest_name": row.get("guest_name"),
            "room_no": row.get("room_no"),
            "operator": operator,
            "time": now,
        })
        return {"ok": True, "checkin": row, "message": f"{row.get('guest_name')} 登记已取消"}

    # ─────────────────────────────────────────────
    # 删除登记 (经理以上)
    # ─────────────────────────────────────────────
    @router.delete("/checkins/{checkin_id}")
    async def delete_checkin(
        checkin_id: str = "",
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ) -> Dict[str, Any]:
        rows = data_layer.load_table("checkins")
        row = _find_checkin(rows, checkin_id)
        rows = [r for r in rows if r.get("id") != checkin_id]
        data_layer.save_table("checkins", rows)
        operator = sess.get("name") or sess.get("staff_id") or "frontdesk"
        data_layer.append_log("checkins_log", {
            "record_key": checkin_id,
            "action": "delete",
            "guest_name": row.get("guest_name"),
            "room_no": row.get("room_no"),
            "operator": operator,
            "time": data_layer.now_str(),
        })
        return {"ok": True, "message": f"已删除 {row.get('guest_name')} 的登记记录"}

    # ─────────────────────────────────────────────
    # CSV 导出
    # ─────────────────────────────────────────────
    @router.get("/checkins/export", response_class=PlainTextResponse)
    async def export_checkins(
        status: str = "",
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> str:
        rows = data_layer.load_table("checkins")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: str(r.get("created_at", "")))
        status_label = {
            _STATUS_IN_HOUSE: "在住", _STATUS_CHECKED_OUT: "已退房", _STATUS_CANCELLED: "已取消",
        }

        def _csv(s: Any) -> str:
            return '"' + str(s if s is not None else "").replace('"', '""') + '"'

        lines = ["登记ID,客人姓名,电话,证件类型,证件号,人数,房号,状态,入住时间,退房时间,登记人,备注"]
        for r in rows:
            lines.append(",".join(_csv(x) for x in [
                r.get("id", ""), r.get("guest_name", ""), r.get("phone", ""),
                r.get("id_type", ""), r.get("id_no", ""), r.get("party_size", ""),
                r.get("room_no", ""), status_label.get(r.get("status"), r.get("status", "")),
                r.get("checkin_time", ""), r.get("checkout_time", ""),
                r.get("created_by", ""), r.get("notes", ""),
            ]))
        return "\n".join(lines)


def get_frontdesk_summary() -> Dict[str, Any]:
    """供其他模块/仪表盘调用的汇总 (非路由)"""
    rows = data_layer.load_table("checkins")
    today = data_layer.today_str()
    return {
        "in_house": sum(1 for r in rows if r.get("status") == _STATUS_IN_HOUSE),
        "today_new": sum(1 for r in rows if str(r.get("created_at", "")).startswith(today)),
        "total": len(rows),
    }
