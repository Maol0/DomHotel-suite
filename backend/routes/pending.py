# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 待确认动作路由 (Phase 6)

设计:
  - 所有写操作(入住/退房/改房态/派单/补消耗品)默认不直接落地,
    而是写入 pending_actions 表,等员工在 UI 上点"确认"才真正执行
  - 员工只能确认 assigned_to=自己(部门/人)的待确认单
  - 支持批量确认
  - 拒绝时记录拒绝原因,待确认单状态 → rejected

依赖:
  - data_layer (读写 JSON 表)
  - wecom_sync.safe_sync (确认后推企微)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Body

from .. import data_layer
from ..wecom_sync import safe_sync
from ._helpers import now

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部 helper: 把待确认动作"落地"到目标表
# ---------------------------------------------------------------------------

def _execute_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """把 pending_action 真正写入到目标表。

    action 结构:
      {
        "action_id": "PA-...",
        "action_type": "checkin" | "checkout" | "status_change" | "assign" | "replenish" | ...,
        "target_table": "rooms" | "work_orders" | ...,
        "target_record": { ... },  # 要写入的字段
        "assigned_to": "housekeeping" | "wang_qian_tai" | ...,
        "created_by": "ai" | "guest" | "frontdesk",
        ...
      }

    Returns:
        {"ok": True, "result": ...} 或 {"ok": False, "error": "..."}
    """
    action_type = action.get("action_type", "")
    target_table = action.get("target_table", "")
    record = action.get("target_record", {}) or {}

    try:
        if action_type == "checkin":
            # 入住:更新 rooms
            room_no = record.get("room_no", "")
            rooms = data_layer.load_table("rooms")
            for r in rooms:
                if r.get("room_no") == room_no:
                    r["status"] = "在住"
                    r["guest_name"] = record.get("guest_name", "")
                    r["guest_phone"] = record.get("guest_phone", "")
                    r["checkin_time"] = now()
                    r["checkin_days"] = record.get("checkin_days", 1)
                    # v2.1.18 修复: 入住备注也持久化 (前端 UI 显示 room.notes)
                    if record.get("notes"):
                        r["notes"] = record.get("notes", "")
                    r["updated_at"] = now()
                    r["updated_by"] = action.get("confirmed_by", "staff")
                    data_layer.save_table("rooms", rooms)
                    return {"ok": True, "room": r}
            return {"ok": False, "error": f"房间 {room_no} 不存在"}

        elif action_type == "checkout":
            room_no = record.get("room_no", "")
            rooms = data_layer.load_table("rooms")
            for r in rooms:
                if r.get("room_no") == room_no:
                    if r.get("status") != "在住":
                        return {"ok": False, "error": f"房间 {room_no} 状态为 {r.get('status')}，无法退房"}
                    old_guest = r.get("guest_name", "")
                    r["status"] = "脏房"
                    r["guest_name"] = ""
                    r["guest_phone"] = ""
                    r["checkin_time"] = ""
                    r["checkin_days"] = 0
                    r["updated_at"] = now()
                    r["updated_by"] = action.get("confirmed_by", "staff")
                    data_layer.save_table("rooms", rooms)
                    return {"ok": True, "room": r, "old_guest": old_guest}
            return {"ok": False, "error": f"房间 {room_no} 不存在"}

        elif action_type == "status_change":
            # 改房态(空房/脏房/维修中/已锁房)
            room_no = record.get("room_no", "")
            new_status = record.get("new_status", "")
            rooms = data_layer.load_table("rooms")
            for r in rooms:
                if r.get("room_no") == room_no:
                    r["status"] = new_status
                    r["updated_at"] = now()
                    r["updated_by"] = action.get("confirmed_by", "staff")
                    if record.get("note"):
                        r.setdefault("history", []).append({
                            "time": now(),
                            "status": new_status,
                            "operator": action.get("confirmed_by", "staff"),
                            "note": record["note"],
                        })
                    data_layer.save_table("rooms", rooms)
                    return {"ok": True, "room": r}
            return {"ok": False, "error": f"房间 {room_no} 不存在"}

        elif action_type in ("assign", "replenish"):
            # 派单:更新 work_orders.assignee + status (assign 和 replenish 走同一个动作)
            wo_id = record.get("wo_id", "")
            wos = data_layer.load_table("work_orders")
            for w in wos:
                if w.get("wo_id") == wo_id:
                    # v2.1.18: target_record 没传 assignee 时, 按工单 target_dept 自动派部门第一个人 (manager 优先)
                    asg_name = record.get("assignee", "") or ""
                    asg_id = record.get("assignee_id", "") or ""
                    if not asg_name and action_type == "assign":
                        # 报修/维修类 (target_dept=engineering) → 工程部第一个人
                        dept = w.get("target_dept", "housekeeping")
                        sts = data_layer.load_table("staff")
                        cands = [s for s in sts if s.get("department_id")==dept and not s.get("deleted") and s.get("on_duty")]
                        if cands:
                            cands.sort(key=lambda s: (0 if s.get("role")=="manager" else 1, s.get("id") or ""))
                            asg_name = cands[0].get("name", "")
                            asg_id = cands[0].get("id", "")
                    if asg_name:
                        w["assignee"] = asg_name
                    if asg_id:
                        w["assignee_id"] = asg_id
                    w["status"] = "assigned"
                    w["updated_at"] = now()
                    w.setdefault("history", []).append({
                        "time": now(),
                        "from": "pending",
                        "to": "assigned",
                        "operator": action.get("confirmed_by", "staff"),
                        "note": f"确认 {action_type} 派单 (前端确认, 自动派给 {asg_name or '部门'})"
                    })
                    data_layer.save_table("work_orders", wos)
                    return {"ok": True, "work_order": w}
            return {"ok": False, "error": f"工单 {wo_id} 不存在"}

        else:
            return {"ok": False, "error": f"未知 action_type: {action_type}"}

    except Exception as exc:
        logger.exception("execute_action 失败: %s", exc)
        return {"ok": False, "error": f"执行失败: {exc}"}


# ---------------------------------------------------------------------------
# 待确认动作管理 helper
# ---------------------------------------------------------------------------

def create_pending_action(
    action_type: str,
    target_table: str,
    target_record: Dict[str, Any],
    assigned_to: str = "",
    created_by: str = "ai",
    description: str = "",
) -> Dict[str, Any]:
    """新建一条待确认动作(写入 pending_actions 表)。

    Args:
        action_type: checkin/checkout/status_change/assign/replenish
        target_table: 目标表名
        target_record: 要写入的字段
        assigned_to: 谁负责确认(部门/人名),空=任何人可确认
        created_by: 谁创建的(ai/guest/frontdesk)
        description: 给员工看的人话描述
    """
    action = {
        "action_id": data_layer.new_id("PA"),
        "action_type": action_type,
        "target_table": target_table,
        "target_record": target_record,
        "assigned_to": assigned_to,
        "created_by": created_by,
        "description": description,
        "status": "pending",  # pending/confirmed/rejected
        "created_at": now(),
        "confirmed_by": "",
        "confirmed_at": "",
        "rejected_by": "",
        "rejected_at": "",
        "reject_reason": "",
    }
    actions = data_layer.load_table("pending_actions")
    actions.append(action)
    data_layer.save_table("pending_actions", actions)
    return action


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------

def register_routes(app) -> None:
    """注册待确认动作路由到 PawApp SDK"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/pending")
    async def list_pending(
        ctx=Depends(get_ctx),
        assigned_to: str = "",
        status: str = "pending",
        action_type: str = "",
    ) -> List[Dict[str, Any]]:
        """列待确认动作。

        Query:
          - assigned_to: 过滤员工(空=全部, 传"housekeeping"=部门, 传"张三"=人)
          - status: pending(默认) / confirmed / rejected
          - action_type: 过滤具体类型(checkin/checkout/...)
        """
        actions = data_layer.load_table("pending_actions")
        result: List[Dict[str, Any]] = []
        for a in actions:
            if status and a.get("status") != status:
                continue
            if assigned_to and a.get("assigned_to") != assigned_to:
                continue
            if action_type and a.get("action_type") != action_type:
                continue
            result.append(a)
        # 按时间倒序(最新的在最前)
        result.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        return result

    @router.get("/pending/stats")
    async def pending_stats(ctx=Depends(get_ctx)) -> Dict[str, Any]:
        """统计:总数 + 按类型分桶 + 按状态分桶"""
        actions = data_layer.load_table("pending_actions")
        stats: Dict[str, Any] = {
            "total": len(actions),
            "by_status": {},
            "by_type": {},
            "by_assigned_to": {},
        }
        for a in actions:
            s = a.get("status", "unknown")
            stats["by_status"][s] = stats["by_status"].get(s, 0) + 1
            t = a.get("action_type", "unknown")
            stats["by_type"][t] = stats["by_type"].get(t, 0) + 1
            who = a.get("assigned_to", "") or "(未分配)"
            stats["by_assigned_to"][who] = stats["by_assigned_to"].get(who, 0) + 1
        return stats

    @router.post("/pending/{action_id}/confirm")
    async def confirm_pending_action(
        ctx=Depends(get_ctx),
        action_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """员工确认单条待确认动作(真正落地 + 推企微)"""
        confirmed_by = payload.get("confirmed_by", "staff")
        note = payload.get("note", "")
        actions = data_layer.load_table("pending_actions")
        for a in actions:
            if a.get("action_id") != action_id:
                continue
            if a.get("status") != "pending":
                raise HTTPException(
                    status_code=400,
                    detail=f"动作 {action_id} 当前状态 {a.get('status')} 不允许确认",
                )
            # 权限校验: 员工只能确认自己的
            # (前端会传 confirmed_by, 后端再校验一次)
            assigned_to = a.get("assigned_to", "")
            if assigned_to and confirmed_by and assigned_to != confirmed_by and not payload.get("is_manager", False):
                # 注: 如果传 is_manager=True 则跳过权限(给值班经理用)
                raise HTTPException(
                    status_code=403,
                    detail=f"动作 {action_id} 分配给 {assigned_to}, 当前确认人 {confirmed_by} 不匹配",
                )
            # 真正落地
            result = _execute_action({**a, "confirmed_by": confirmed_by, "target_record": {**(a.get("target_record") or {}), **({"assignee": payload.get("assignee","")} if payload.get("assignee") else {}), **({"assignee_id": payload.get("assignee_id","")} if payload.get("assignee_id") else {})}})
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("error", "执行失败"))
            # 更新 pending_actions 状态
            a["status"] = "confirmed"
            a["confirmed_by"] = confirmed_by
            a["confirmed_at"] = now()
            if note:
                a["confirm_note"] = note
            data_layer.save_table("pending_actions", actions)
            # 推企微(异步,不阻塞响应)
            try:
                await safe_sync("pending_actions", a)
            except Exception as exc:
                logger.warning("safe_sync pending_actions 失败: %s", exc)
            return {"ok": True, "action": a, "execution_result": result}

        raise HTTPException(status_code=404, detail=f"动作 {action_id} 不存在")

    @router.post("/pending/confirm_batch")
    async def confirm_pending_batch(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """批量确认(只确认 confirmed_by 自己负责的)"""
        action_ids: List[str] = payload.get("action_ids", [])
        confirmed_by = payload.get("confirmed_by", "staff")
        note = payload.get("note", "")
        if not action_ids:
            raise HTTPException(status_code=400, detail="action_ids 必填(非空数组)")
        actions = data_layer.load_table("pending_actions")
        results: List[Dict[str, Any]] = []
        ok_count = 0
        skip_count = 0
        fail_count = 0
        for a in actions:
            if a.get("action_id") not in action_ids:
                continue
            if a.get("status") != "pending":
                results.append({
                    "action_id": a.get("action_id"),
                    "ok": False,
                    "skip_reason": f"状态 {a.get('status')} 不可确认",
                })
                skip_count += 1
                continue
            assigned_to = a.get("assigned_to", "")
            if assigned_to and confirmed_by and assigned_to != confirmed_by and not payload.get("is_manager", False):
                results.append({
                    "action_id": a.get("action_id"),
                    "ok": False,
                    "skip_reason": f"权限不足: 分配给 {assigned_to}, 当前 {confirmed_by}",
                })
                skip_count += 1
                continue
            # 落地
            result = _execute_action({**a, "confirmed_by": confirmed_by, "target_record": {**(a.get("target_record") or {}), **({"assignee": payload.get("assignee","")} if payload.get("assignee") else {}), **({"assignee_id": payload.get("assignee_id","")} if payload.get("assignee_id") else {})}})
            if result.get("ok"):
                a["status"] = "confirmed"
                a["confirmed_by"] = confirmed_by
                a["confirmed_at"] = now()
                if note:
                    a["confirm_note"] = note
                results.append({"action_id": a.get("action_id"), "ok": True})
                ok_count += 1
            else:
                a["status"] = "rejected"
                a["rejected_by"] = confirmed_by
                a["rejected_at"] = now()
                a["reject_reason"] = f"执行失败: {result.get('error')}"
                results.append({
                    "action_id": a.get("action_id"),
                    "ok": False,
                    "error": result.get("error"),
                })
                fail_count += 1
        data_layer.save_table("pending_actions", actions)
        return {
            "ok": True,
            "summary": {
                "total": len(action_ids),
                "confirmed": ok_count,
                "skipped": skip_count,
                "failed": fail_count,
            },
            "results": results,
        }

    @router.post("/pending/{action_id}/reject")
    async def reject_pending_action(
        ctx=Depends(get_ctx),
        action_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """员工拒绝(不落地)"""
        rejected_by = payload.get("rejected_by", "staff")
        reason = payload.get("reason", "")
        is_manager = payload.get("is_manager", False)
        actions = data_layer.load_table("pending_actions")
        for a in actions:
            if a.get("action_id") != action_id:
                continue
            if a.get("status") != "pending":
                raise HTTPException(
                    status_code=400,
                    detail=f"动作 {action_id} 当前状态 {a.get('status')} 不允许拒绝",
                )
            assigned_to = a.get("assigned_to", "")
            if assigned_to and rejected_by and assigned_to != rejected_by and not is_manager:
                raise HTTPException(
                    status_code=403,
                    detail=f"动作 {action_id} 分配给 {assigned_to}, 当前拒绝人 {rejected_by} 不匹配",
                )
            a["status"] = "rejected"
            a["rejected_by"] = rejected_by
            a["rejected_at"] = now()
            if reason:
                a["reject_reason"] = reason
            data_layer.save_table("pending_actions", actions)
            try:
                await safe_sync("pending_actions", a)
            except Exception as exc:
                logger.warning("safe_sync pending_actions 失败: %s", exc)
            return {"ok": True, "action": a}

        raise HTTPException(status_code=404, detail=f"动作 {action_id} 不存在")

    app.include_router(router)
    logger.info("[routes/pending] 已注册 4 个待确认动作路由")