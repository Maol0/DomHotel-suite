# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 房态路由 (6 endpoints)

"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Body

from .. import data_layer
from ..wecom_sync import safe_sync, safe_sync_quiet_async
from ._helpers import now, create_work_order, new_id, build_work_order, save_work_order
from .work_order_svc import svc_create_work_order, svc_complete_work_order
from .. import auth
from .. import meta as _meta

logger = logging.getLogger(__name__)


# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()




def _resolve_operator_name(operator_id: str) -> str:
    """v2.1.18: 把 staff_id 转人类可读名字 (operator_id 可能是 'staff-20260811152022-b55e2d' 或 'domai')
    优先: 1) 如果已是短名字 (< 20 字符 + 不是 'staff-' 开头) 直接返回
         2) 查 staff 表, 找到的话返回 name
         3) 返回原值 (前 12 字符)
    """
    if not operator_id:
        return ''
    if not operator_id.startswith('staff-') and len(operator_id) < 20:
        return operator_id
    # 查 staff 表
    try:
        staffs = data_layer.load_table('staff')
        for s in staffs:
            if s.get('id') == operator_id:
                return s.get('name') or operator_id[:12]
    except Exception:
        pass
    return operator_id[:12] + '...' if len(operator_id) > 12 else operator_id


# v2.3.0: 房号 → 楼栋 推导规则
# 规则（按优先级匹配）：
#   1. 4 位数字 + 千位 1-5 → 千位=楼栋，百位=楼层（桂山华星酒店格式）
#      例 1201 → 1号楼, 2132 → 2号楼, 3126 → 3号楼, 5201 → 4号楼
#   2. 4 位数字 + 千位 0 (如 0101) → 看百位决定楼栋
#   3. 3 位数字 (如 404) → 百位=楼栋号 (漓江大酒店格式)
#   4. 字母前缀 (如 A101) → A/B/C/D/E 映射到 1-5 号楼
#   5. 其他 → "未分类"
# 整合版: checkins 表同步辅助 — 房态看板的入住/退房同步到前台接待登记表
# (与 backend/checkin.py 的字段语义一致, source 标记来源为 rooms_board)
def _sync_checkin_record(action: str, room_no: str, guest_name: str,
                         guest_phone: str = "", operator: str = "",
                         notes: str = "") -> Dict[str, Any]:
    """房态看板入住/退房时同步 checkins 表, 让前台接待 tab 数据打通

    action=checkin  → 追加一条 in_house 登记 (source=rooms_board)
    action=checkout → 找该房号最近一条 in_house 登记置为 checked_out
    任何异常静默降级 (不影响房态主流程), 返回同步结果摘要。
    """
    try:
        _ts = now()
        checkins = data_layer.load_table("checkins")
        if action == "checkin":
            row = {
                "id": new_id("CI"),
                "guest_name": guest_name,
                "phone": guest_phone,
                "id_type": "",
                "id_no": "",
                "party_size": 1,
                "room_no": room_no,
                "notes": notes or "",
                "status": "in_house",
                "created_at": _ts,
                "created_by": operator or "房态看板",
                "checkin_time": _ts,
                "checkout_time": "",
                "room_linked": True,
                "source": "rooms_board",
            }
            checkins.append(row)
            data_layer.save_table("checkins", checkins)
            data_layer.append_log("checkins_log", {
                "record_key": row["id"], "action": "create",
                "guest_name": guest_name, "room_no": room_no,
                "operator": row["created_by"], "time": _ts,
                "source": "rooms_board",
            })
            return {"synced": True, "action": "checkin", "id": row["id"]}
        if action == "checkout":
            for c in reversed(checkins):
                if c.get("room_no") == room_no and c.get("status") == "in_house":
                    c["status"] = "checked_out"
                    c["checkout_time"] = _ts
                    c["updated_at"] = _ts
                    c["updated_by"] = operator or "房态看板"
                    data_layer.save_table("checkins", checkins)
                    data_layer.append_log("checkins_log", {
                        "record_key": c.get("id"), "action": "checkout",
                        "guest_name": c.get("guest_name"), "room_no": room_no,
                        "operator": c.get("updated_by"), "time": _ts,
                        "source": "rooms_board",
                    })
                    return {"synced": True, "action": "checkout", "id": c.get("id")}
            # 该房在住不是从有登记记录的入口进来的 (老数据) — 不报错
            return {"synced": False, "reason": f"{room_no} 无在住登记记录 (可能为历史数据)"}
        return {"synced": False, "reason": f"未知 action: {action}"}
    except Exception as exc:
        logger.warning("_sync_checkin_record(%s, %s) 失败: %s", action, room_no, exc)
        return {"synced": False, "reason": str(exc)}


def _ensure_building(room: Dict[str, Any]) -> Dict[str, Any]:
    """根据 room_no 自动推导并补全 building 字段（不修改原 dict）

    设计原则: 当前 plugin 只服务漓江大瀑布一家单栋楼酒店。
    房号格式 0404~1680（千位是楼层高位,不是楼栋号）。
    任何房间都归入 "主楼",楼层由 floor 字段决定(已是数字 4~16)。

    如未来支持多楼栋酒店,约定: 房号带显式楼栋前缀时(如 "1号楼-1201"),
    在房间数据里直接设置 `building` 字段,本函数不覆盖显式值。
    """
    if room.get("building"):
        return room
    # 兜底统一归主楼(单栋楼酒店)
    room["building"] = "主楼"
    room["building_block"] = "BLOCK MAIN"
    return room


async def _auto_link_work_order(room_no: str, old_status: str, new_status: str, operator: str = "staff", note: str = "") -> dict:
    """v2.1.18 联动: 改房态时自动建/关工单, 保持业务闭环

    规则:
      - 维修中 (新建维修工单) - 故障描述用 room.notes 或 note
      - 待打扫 (新建清洁工单) - request_clean 也用这个, 但先到的赢
      - 空房 (从待打扫/维修中来) - 关闭关联工单
      - 其他转换 - 不联动

    v2026-09-04: 用 build + save 分两步，避免双重保存
    v2026-09-04: 修复关闭工单逻辑 - 先关闭再返回，避免被 existing_open 检查跳过
    """
    # 使用模块顶层导入的 now 函数，避免相对导入问题
    _now = now
    wos = data_layer.load_table("work_orders")
    
    # v2026-09-04: 先处理关闭工单逻辑（避免被 existing_open 检查跳过）
    if new_status == "空房" and old_status in ("待打扫", "维修中"):
        # 找该房最近一条相关工单
        candidates = [w for w in wos if w.get("room_no") == room_no
                      and w.get("status") not in ("done", "rejected", "completed")
                      and w.get("work_type") == ("维修" if old_status == "维修中" else "清洁")]
        
        if candidates:
            wo = candidates[-1]  # 最新的
            # v1.6.1 统一收口: 委托 service 关单 (force 允许 pending→done;
            # 耗时/pending_actions/通知/safe_sync 单一实现; 通知动作正名为"完成")
            result = await svc_complete_work_order(
                wo.get("wo_id", ""), operator=operator,
                result_note=(note or "完成") + " (联动自动关闭)",
                force=True,
            )
            return result.get("work_order", wo)

    # v2.1.18: 先查同房同类型未关闭的工单, 避免重复建 (request_clean 端点已经建过)
    existing_open = [w for w in wos
                    if w.get("room_no") == room_no
                    and w.get("status") not in ("done", "rejected", "pending_confirm")
                    and (w.get("work_type") == ("维修" if new_status == "维修中" else "清洁" if new_status == "待打扫" else None))]
    if existing_open:
        # 已有关联未关闭工单, 跳过新建 (返回最近一个)
        return existing_open[-1]

    # 1) 改'维修中' → 建维修工单 (pending, 管理者在看板手动/AI派单)
    if new_status == "维修中":
        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        description = (room.get("notes") if room else None) or note or "报修 (未填描述)"
        # v1.6.1 统一收口: 委托 service (不自动派单, 保留"待看板派单"语义;
        # 建单+同步+员工通知+客人回执由 svc 统一)
        result = await svc_create_work_order(
            room_no=room_no, work_type="维修",
            description=description, priority="normal",
            reporter=operator, target_dept="engineering",
            data_source="manual", operator=operator,
            auto_dispatch=False,
        )
        return result.get("work_order", {})

    # 2) 改'待打扫' 或 '脏房' → 建清洁工单 (pending, 管理者在看板手动/AI派单)
    if new_status in ("待打扫", "脏房"):
        # v1.6.1 统一收口: 委托 service (不自动派单; 同步+通知统一)
        result = await svc_create_work_order(
            room_no=room_no, work_type="清洁",
            description=note or f"房间 {room_no} 需要打扫",
            priority="normal", reporter=operator,
            target_dept="housekeeping",
            data_source="manual", operator=operator,
            auto_dispatch=False,
        )
        return result.get("work_order", {})

    return None



def register_routes(app) -> None:
    """注册房态路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/rooms")
    async def list_rooms(
        ctx=Depends(get_ctx),
        status: str = "",
        floor: str = "",
        room_type: str = "",
        building: str = "",
        search: str = "",
    ) -> List[Dict[str, Any]]:
        rooms = data_layer.load_table("rooms")
        result: List[Dict[str, Any]] = []
        for r in rooms:
            # v2.3.0: 自动从 room_no 推导 building (兼容历史数据, 避免前端按楼栋分组时全部为空)
            r = _ensure_building(r)
            if status and r.get("status") != status:
                continue
            if floor and str(r.get("floor")) != floor:
                continue
            if room_type and r.get("room_type") != room_type:
                continue
            if building and r.get("building") != building:
                continue
            if search and search not in (r.get("room_no", "") + r.get("guest_name", "")):
                continue
            result.append(r)
        return result

    # v2.3.0: 楼栋统计端点
    @router.get("/buildings")
    async def list_buildings(ctx=Depends(get_ctx)) -> List[Dict[str, Any]]:
        """返回所有楼栋及其统计信息, 用于前端按楼栋分组展示"""
        rooms = data_layer.load_table("rooms")
        buildings: Dict[str, Dict[str, Any]] = {}
        for r in rooms:
            r = _ensure_building(r)
            b = r.get("building") or "未分类"
            if b not in buildings:
                buildings[b] = {
                    "name": b,
                    "block": r.get("building_block", ""),
                    "room_count": 0,
                    "floor_count": 0,
                    "floors": set(),
                    "status_summary": {},
                }
            buildings[b]["room_count"] += 1
            if r.get("floor"):
                try:
                    buildings[b]["floors"].add(int(r["floor"]))
                except (ValueError, TypeError):
                    pass
            s = r.get("status", "未分类")
            buildings[b]["status_summary"][s] = buildings[b]["status_summary"].get(s, 0) + 1
        result = []
        for b in buildings.values():
            b["floor_count"] = len(b["floors"])
            b["floors"] = sorted(b["floors"])
            result.append(b)
        return sorted(result, key=lambda x: x["name"])

    @router.get("/rooms/{room_no}")
    async def get_room(ctx=Depends(get_ctx), room_no: str = "") -> Dict[str, Any]:
        for r in data_layer.load_table("rooms"):
            if r.get("room_no") == room_no:
                return r
        raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")

    @router.put("/rooms/{room_no}/status")
    async def update_room_status(
        ctx=Depends(get_ctx),
        room_no: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """改房态(v2.1.18: 直接落地, 不再走 pending_actions)

        历史: Phase 6 设计是改房态入 pending_actions 队列等审批 — 但房态变化是日常高频操作
              (标记打扫/打扫完成/换房), 走队列让用户感觉'按了没反应'。
              现在改为直接生效, 只记录到 rooms 表 history 字段 (审计可追溯)。

        注意: 报修联动改'维修中' 建 pending 工单, 管理者在看板手动/AI派单。
              修这里之前用本 endpoint 的人已经在 doReportRepair() 看到 pending 提示, 走通。
              此处只删掉纯改房态的 pending 路径, 让用户感受到 '按了就改'。
        """
        new_status = payload.get("new_status", "")
        operator = payload.get("operator", "ai")
        note = payload.get("note", "")
        created_by = payload.get("created_by", operator)
        if not new_status:
            raise HTTPException(status_code=400, detail="new_status 必填")
        # 校验房间存在
        rooms = data_layer.load_table("rooms")
        room_found = None
        for r in rooms:
            if r.get("room_no") == room_no:
                room_found = r
                break
        if not room_found:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")
        old_status = room_found.get("status", "")
        # 直接改房态
        room_found["status"] = new_status
        room_found["updated_at"] = now()
        room_found["updated_by"] = _resolve_operator_name(created_by)
        # v2.1.18: 记录历史 (审计可追溯, 不入 pending 队列)
        if "history" not in room_found:
            room_found["history"] = []
        room_found["history"].append({
            "time": now(),
            "from": old_status,
            "to": new_status,
            "operator": created_by,
            "note": note,
        })
        # 历史只留最近 20 条
        if len(room_found["history"]) > 20:
            room_found["history"] = room_found["history"][-20:]
        try:
            _meta.apply_meta_on_update(room_found, source="manual", operator=created_by)
        except Exception as meta_exc:
            with open('/tmp/debug_meta_error.txt', 'a', encoding='utf-8') as f:
                f.write(f"_meta.apply_meta_on_update 失败: {meta_exc}\n")
            # 继续执行，不阻塞
            pass
        data_layer.save_table("rooms", rooms)
        # v2.1.18: 联动 - 改房态时自动建/关工单 (业务闭环)
        related_work_order = None
        try:
            related_work_order = await _auto_link_work_order(
                room_no=room_no, old_status=old_status, new_status=new_status,
                operator=created_by, note=note
            )
        except Exception as link_exc:
            logger.warning("联动工单失败: %s", link_exc)
            logger.exception("联动工单异常详情:")
        # 推企微 (智能表格 房态变更走 rooms_log; Phase 5 当前用 sync_now 推到表)
        try:
            _ts = now()
            await safe_sync("rooms_log", {
                # v2.1.18: record_key 幂等键 (room_no + 时间), 防止每次重复推送新建一行
                "record_key": f"{room_no}-{_ts}",
                "room_no": room_no,
                "status": new_status,
                "guest_name": room_found.get("guest_name", ""),
                "guest_phone": room_found.get("guest_phone", ""),
                "updated_at": _ts,
                "updated_by": created_by,
                "note": note or f"手动改房态: {old_status} → {new_status}",
            })
        except Exception as exc:
            logger.warning("safe_sync rooms_log 失败: %s", exc)
        return {
            "ok": True,
            "pending": False,
            "room": room_found,
            "message": f"房间 {room_no}: {old_status} → {new_status}",
            "old_status": old_status,
            "new_status": new_status,
            # v2.1.18: 联动工单
            "work_order": related_work_order,
        }

    @router.post("/rooms/{room_no}/checkin")
    async def checkin(
        ctx=Depends(get_ctx),
        room_no: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """客人入住登记 (v2.1.18: 直接落地, 不再走 pending_actions)

        历史: 跟 update_status / checkout 同样的问题 — 入住是日常高频操作, 走队列让用户
              感觉'按了没反应'(房态不变)。改为直接生效。
        """
        guest_name = payload.get("guest_name", "")
        guest_phone = payload.get("guest_phone", "")
        checkin_days = int(payload.get("checkin_days", 1))
        # v2.1.18: 接收前端发的 notes 字段
        notes = payload.get("notes", "")
        created_by = payload.get("created_by", "frontdesk")
        if not guest_name:
            raise HTTPException(status_code=400, detail="guest_name 必填")
        # 校验房间存在 + 空房状态
        rooms = data_layer.load_table("rooms")
        room_found = None
        for r in rooms:
            if r.get("room_no") == room_no:
                room_found = r
                break
        if not room_found:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")
        old_status = room_found.get("status", "")
        if old_status != "空房":
            raise HTTPException(
                status_code=400,
                detail=f"房间 {room_no} 当前状态 {old_status}，无法入住",
            )
        # 直接改房态 (空房 → 在住)
        room_found["status"] = "在住"
        room_found["guest_name"] = guest_name
        room_found["guest_phone"] = guest_phone
        room_found["checkin_time"] = now()
        room_found["checkin_days"] = checkin_days
        if notes:
            room_found["notes"] = notes
        room_found["updated_at"] = now()
        room_found["updated_by"] = _resolve_operator_name(created_by)
        # 记 history
        if "history" not in room_found:
            room_found["history"] = []
        room_found["history"].append({
            "time": now(),
            "from": old_status,
            "to": "在住",
            "operator": created_by,
            "note": f"入住: {guest_name}" + (f" / {guest_phone}" if guest_phone else ""),
        })
        if len(room_found["history"]) > 20:
            room_found["history"] = room_found["history"][-20:]
        try:
            _meta.apply_meta_on_update(room_found, source="manual", operator=created_by)
        except Exception as meta_exc:
            with open('/tmp/debug_meta_error.txt', 'a', encoding='utf-8') as f:
                f.write(f"_meta.apply_meta_on_update 失败: {meta_exc}\n")
            # 继续执行，不阻塞
            pass
        data_layer.save_table("rooms", rooms)
        # 整合版: 同步写入前台接待 checkins 表 (数据打通, 前台接待 tab 能看到)
        _sync_checkin_record(
            action="checkin",
            room_no=room_no,
            guest_name=guest_name,
            guest_phone=guest_phone,
            operator=_resolve_operator_name(created_by),
            notes=notes,
        )
        # 推企微 (房态变更 + 客人入住日志)
        try:
            _ts = now()
            await safe_sync("rooms_log", {
                "record_key": f"{room_no}-{_ts}",
                "room_no": room_no,
                "status": "在住",
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "checkin_time": _ts,
                "updated_at": _ts,
                "updated_by": created_by,
                "note": notes or f"入住: {guest_name}",
            })
            await safe_sync("guests_log", {
                "record_key": f"{room_no}-{_ts}",
                "room_no": room_no,
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "checkin_time": _ts,
                "checkout_time": "",
                "checkin_days": checkin_days,
            })
        except Exception as exc:
            logger.warning("safe_sync checkin 失败: %s", exc)
        return {
            "ok": True,
            "pending": False,
            "room": room_found,
            "message": f"房间 {room_no} 入住成功: {guest_name}",
            "new_status": "在住",
        }

    @router.post("/rooms/{room_no}/checkout")
    async def checkout(
        ctx=Depends(get_ctx),
        room_no: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """客人退房 (v2.1.18: 直接落地, 不再走 pending_actions)

        历史: 跟 update_room_status 同样的问题 — 退房是日常高频操作, 走 pending 队列
              让用户感觉'按了没反应'(房态不变)。改为直接生效 + 记 history。
        """
        created_by = payload.get("created_by", "frontdesk")
        # 校验房间存在 + 在住状态
        rooms = data_layer.load_table("rooms")
        room_found = None
        for r in rooms:
            if r.get("room_no") == room_no:
                room_found = r
                break
        if not room_found:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")
        if room_found.get("status") != "在住":
            raise HTTPException(
                status_code=400,
                detail=f"房间 {room_no} 状态为 {room_found.get('status')}，无法退房",
            )
        old_status = room_found.get("status", "")
        old_guest = room_found.get("guest_name", "")
        old_phone = room_found.get("guest_phone", "")
        old_checkin = room_found.get("checkin_time", "")
        # 直接改: 在住 → 脏房 (前台下班前, 阿姨看到脏房就知道要打扫)
        room_found["status"] = "脏房"
        room_found["guest_name"] = ""
        room_found["guest_phone"] = ""
        room_found["checkin_time"] = ""
        room_found["checkin_days"] = 0
        room_found["updated_at"] = now()
        room_found["updated_by"] = _resolve_operator_name(created_by)
        # v2.1.18: 记 history 字段 (审计可追溯)
        if "history" not in room_found:
            room_found["history"] = []
        room_found["history"].append({
            "time": now(),
            "from": old_status,
            "to": "脏房",
            "operator": created_by,
            "note": f"退房: 原住客 {old_guest or '(无)'}",
        })
        if len(room_found["history"]) > 20:
            room_found["history"] = room_found["history"][-20:]
        try:
            _meta.apply_meta_on_update(room_found, source="manual", operator=created_by)
        except Exception as meta_exc:
            with open('/tmp/debug_meta_error.txt', 'a', encoding='utf-8') as f:
                f.write(f"_meta.apply_meta_on_update 失败: {meta_exc}\n")
            # 继续执行，不阻塞
            pass
        data_layer.save_table("rooms", rooms)
        # 整合版: 同步关闭前台接待 checkins 表里对应的在住登记 (数据打通)
        _sync_checkin_record(
            action="checkout",
            room_no=room_no,
            guest_name=old_guest,
            guest_phone=old_phone,
            operator=_resolve_operator_name(created_by),
        )
        # 推企微 (房态变更走 rooms_log; 客人入住历史走 guests_log)
        try:
            _ts = now()
            await safe_sync("rooms_log", {
                "record_key": f"{room_no}-{_ts}",
                "room_no": room_no,
                "status": "脏房",
                "guest_name": "",
                "guest_phone": "",
                "checkin_time": "",
                "updated_at": _ts,
                "updated_by": created_by,
                "note": f"退房: 原住客 {old_guest or '(无)'}",
            })
            await safe_sync("guests_log", {
                "record_key": f"{room_no}-{_ts}",
                "room_no": room_no,
                "guest_name": old_guest,
                "guest_phone": old_phone,
                "checkin_time": old_checkin,
                "checkout_time": _ts,
                "checkin_days": 0,
            })
        except Exception as exc:
            logger.warning("safe_sync checkout 失败: %s", exc)
        # v2.1.19: 退房自动建清洁工单 (pending 状态, 管理者在看板手动/AI派单)
        # v2026-09-04: 用 build + save 分两步，避免双重保存
        work_order = None
        try:

            _res = await svc_create_work_order(
                room_no=room_no, work_type="清洁",
                description=f"退房后清洁 (原住客 {old_guest or '(无)'})",
                priority="normal", reporter=created_by, target_dept="housekeeping",
                data_source="manual", operator=created_by,
                auto_dispatch=False,
            )
            work_order = _res.get("work_order")
        except Exception as we:
            logger.warning(f"退房建清洁工单失败: {we}")
        return {
            "ok": True,
            "pending": False,
            "room": room_found,
            "work_order": work_order,
            "message": f"房间 {room_no} 退房成功: {old_guest or '(无)'} → 脏房" + (f", 已派给 {work_order.get('assignee','?')}" if work_order and work_order.get('assignee') else ""),
            "old_guest": old_guest,
            "new_status": "脏房",
        }

    @router.post("/rooms/{room_no}/replenish")
    async def replenish(
        ctx=Depends(get_ctx),
        room_no: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        items = payload.get("items", [])
        if not items:
            raise HTTPException(status_code=400, detail="items 必填")
        # v2.1.16-hotfix4: 兼容两种 items 格式
        #   - [{"name":"矿泉水","qty":1}]  (dict 列表,前端常用)
        #   - ["矿泉水", "牙刷"]           (str 列表,简洁模式)
        if isinstance(items[0], dict):
            items_str = ", ".join(f"{i.get('name','?')}x{i.get('qty',1)}" for i in items)
            items_norm = items
        else:
            items_str = ", ".join(items)
            items_norm = [{"name": n, "qty": 1} for n in items]
        import sys, traceback
        try:
            supplies = data_layer.load_table("supplies")
            record = {
                "room_no": room_no,
                "items": items_norm,
                "time": now(),
                "operator": payload.get("operator", "ai"),
            }
            supplies.append(record)
            data_layer.save_table("supplies", supplies)
            print(f"[replenish] step1 ok supplies_count={len(supplies)}", file=sys.stderr, flush=True)

            # 找客房阿姨 (跟前几个端点一致)
            rstaffs = data_layer.load_table("staff")
            rcandidates = [s for s in rstaffs if s.get("department_id") in ("dept-5", "dept_housekeeping") and not s.get("deleted") and s.get("on_duty")]
            if not rcandidates:
                rcandidates = [s for s in rstaffs if not s.get("deleted") and s.get("on_duty")]
            rcandidates.sort(key=lambda s: (0 if s.get("role")=="manager" else 1, s.get("id") or ""))
            ra = rcandidates[0] if rcandidates else None
            rassignee = ra.get("name","") if ra else ""
            rassignee_id = ra.get("id","") if ra else ""

            # v2026-09-04: 用 build + save 分两步，避免双重保存

            # v1.6.1 统一收口: 委托 service (pending 前台确认派单; 补上此前漏的 work_orders 表格同步 + 统一通知)
            _res = await svc_create_work_order(
                room_no=room_no,
                work_type="补充消耗品",
                description=f"补充：{items_str}",
                priority="normal",
                reporter=payload.get("operator", "ai"),
                target_dept="housekeeping",
                data_source="manual",
                operator=payload.get("operator", "ai"),
                auto_dispatch=False,
            )
            wo = _res.get("work_order", {})
            print(f"[replenish] step2 ok wo_id={wo.get('wo_id')} (pending, 管理者在看板手动/AI派单)", file=sys.stderr, flush=True)

            # v2.1.16-hotfix4: safe_sync 是 fire-and-forget,但 await 它会真起 task;
            #   万一 create_task 失败(_add_record 内部 raise) 会把异常带回来。
            #   改用直接 fire-and-forget,不让外层路由感知。
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                # v2.1.18: record_key 幂等键 (room_no + supplies 时间), 防止每次重复推送
                _sup_key = f"{room_no}-{record['time']}"
                if loop.is_running():
                    # 在异步上下文,直接 await 但 catch 所有异常
                    await safe_sync_quiet_async("supplies_log", {
                        "record_key": _sup_key,
                        "room_no": room_no,
                        "items": ", ".join(items),
                        "time": record["time"],
                    })
                else:
                    await safe_sync("supplies_log", {
                        "record_key": _sup_key,
                        "room_no": room_no,
                        "items": ", ".join(items),
                        "time": record["time"],
                    })
            except Exception as sync_exc:
                # 企微同步失败不影响主流程,只打日志
                print(f"[replenish] safe_sync warn: {sync_exc}", file=sys.stderr, flush=True)

            print(f"[replenish] step3 ok all done", file=sys.stderr, flush=True)
            return {"ok": True, "supplies_record": record, "auto_work_order": wo}
        except HTTPException:
            raise
        except Exception as exc:
            print(f"[replenish] FATAL: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            raise HTTPException(status_code=500, detail=f"replenish failed: {str(exc)[:200]}")

    @router.post("/rooms/{room_no}/request_clean")
    async def request_clean(
        ctx=Depends(get_ctx),
        room_no: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        """标记打扫:
        - 改房态: 空房/脏房 → 待打扫
        - 建清洁工单 (pending 状态, 管理者在看板手动/AI派单)
        """
        created_by = payload.get("created_by", sess.get("user_id", "staff"))
        note = payload.get("note", "")
        rooms = data_layer.load_table("rooms")
        room_found = None
        for r in rooms:
            if r.get("room_no") == room_no:
                room_found = r
                break
        if not room_found:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")
        old_status = room_found.get("status", "")
        if old_status not in ("空房", "脏房", "已清洁", "已退未查", "干净"):
            raise HTTPException(
                status_code=400,
                detail=f"房间 {room_no} 当前状态 {old_status}，无需打扫"
            )
        # 改房态 → 待打扫
        room_found["status"] = "待打扫"
        room_found["updated_at"] = now()
        room_found["updated_by"] = _resolve_operator_name(created_by)
        if "history" not in room_found:
            room_found["history"] = []
        room_found["history"].append({
            "time": now(),
            "from": old_status,
            "to": "待打扫",
            "operator": created_by,
            "note": note or "标记打扫",
        })
        if len(room_found["history"]) > 20:
            room_found["history"] = room_found["history"][-20:]
        try:
            _meta.apply_meta_on_update(room_found, source="manual", operator=created_by)
        except Exception as meta_exc:
            with open('/tmp/debug_meta_error.txt', 'a', encoding='utf-8') as f:
                f.write(f"_meta.apply_meta_on_update 失败: {meta_exc}\n")
            # 继续执行，不阻塞
            pass
        data_layer.save_table("rooms", rooms)
        # 建清洁工单 (pending, 管理者在看板手动/AI派单)
        # v2026-09-04: 用 build + save 分两步，避免双重保存
        try:

            _res = await svc_create_work_order(
                room_no=room_no, work_type="清洁",
                description=note or f"房间 {room_no} 需要打扫",
                priority="normal", reporter=created_by, target_dept="housekeeping",
                data_source="manual", operator=created_by,
                auto_dispatch=False,
            )
            wo = _res.get("work_order", {})
        except Exception as exc:
            logger.error(f"创建清洁工单失败: {exc}", exc_info=True)
            wo = {"wo_id": "FAILED", "error": str(exc)}
        # 推企微 (work_orders 已由 svc 同步, 这里只补房态日志)
        try:
            await safe_sync("rooms_log", {
                "record_key": f"{room_no}-{now()}",
                "room_no": room_no,
                "status": "待打扫",
                "updated_at": now(),
                "updated_by": created_by,
                "note": note or "标记打扫",
            })
        except Exception as exc:
            logger.warning("safe_sync request_clean 失败: %s", exc)
        return {
            "ok": True,
            "new_status": "待打扫",
            "room": room_found,
            "work_order": wo,
            "message": f"房间 {room_no} 已标记打扫,工单 {wo['wo_id']} 待派单",
        }

    app.include_router(router)
    logger.info("[routes/rooms] 已注册 7 个房态路由 (v2.1.18+request_clean)")
