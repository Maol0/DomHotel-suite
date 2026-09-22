# -*- coding: utf-8 -*-
"""DomHotel Suite — 工单业务逻辑 service 层

HTTP 路由 (work_orders.py) 和 agent 工具 (hotel_ops_tools.py) 共用本模块，
保证所有工单操作走同一条代码路径：safe_sync / notify / history / 房态联动等
业务逻辑只写一份。

调用方负责传 operator (谁触发的), 本模块不关心 auth / HTTP 层。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .. import data_layer
from .. import wecom_kf
from ..wecom_sync import safe_sync
from ._helpers import now, build_work_order, save_work_order

logger = logging.getLogger("domhotel-suite.work_order_svc")


def _get_work_orders_module():
    """定位 work_orders 模块 (含 _notify_staff_wo_change)。

    插件运行时的模块命名不稳定(routes/__init__ 用 __domhotel_suite_backend__,
    loader 用 plugin_domhotel_suite),按名字 endswith 经常落空 -> 建单不通知。
    v1.6.3: 改为按"属性"扫描 sys.modules 定位, 再兜底按文件路径 importlib 加载。"""
    import sys
    # 1) 按属性扫描: 任何模块只要暴露 _notify_staff_wo_change 即命中
    for k, v in list(sys.modules.items()):
        if v is not None and callable(getattr(v, "_notify_staff_wo_change", None)):
            return v
    # 2) 按候选名兜底
    for name in (
        "__domhotel_suite_backend__.routes.work_orders",
        "plugin_domhotel_suite.routes.work_orders",
    ):
        m = sys.modules.get(name)
        if m is not None and callable(getattr(m, "_notify_staff_wo_change", None)):
            return m
    # 3) 最后兜底: 用"当前包"的相对导入加载 work_orders
    #    (svc 的 __package__ 就是运行时真实包名, 如 plugin_domhotel_suite.routes,
    #     不猜命名、不碰文件路径, 避开 __domhotel_suite_backend__ 未注册的坑)
    try:
        import importlib
        pkg = __package__ or __name__.rsplit(".", 1)[0]
        mod = importlib.import_module(".work_orders", pkg)
        if callable(getattr(mod, "_notify_staff_wo_change", None)):
            import sys as _s
            print(f"[WO通知] _get_work_orders_module 走相对导入兜底成功 pkg={pkg}",
                  file=_s.stderr, flush=True)
            return mod
    except Exception as exc:
        import sys as _s
        print(f"[WO通知] _get_work_orders_module 相对导入兜底失败: {exc}",
              file=_s.stderr, flush=True)
    try:
        import sys as _s
        print("[WO通知] _get_work_orders_module 未命中(属性扫描/导入兜底均无)",
              file=_s.stderr, flush=True)
    except Exception:
        pass
    return None

# ───────────── 通知函数注册握手 (v1.6.3) ─────────────
# work_orders.py 在 import 时调用 register_notify(_notify_staff_wo_change) 把自己注入这里。
# svc 通过 _notify_change 直接 await, 彻底绕开"运行时包名不确定/父包未注册"的问题。
_NOTIFY_FN = None


def register_notify(fn) -> None:
    """由 work_orders 模块在加载时调用, 注册真正的通知实现。"""
    global _NOTIFY_FN
    if callable(fn):
        _NOTIFY_FN = fn


def _resolve_notify_fn():
    global _NOTIFY_FN
    if callable(_NOTIFY_FN):
        return _NOTIFY_FN
    m = _get_work_orders_module()  # 兜底: 仍尝试一次模块定位
    fn = getattr(m, "_notify_staff_wo_change", None) if m is not None else None
    if callable(fn):
        _NOTIFY_FN = fn
    return fn


async def _notify_change(wo: dict, action: str, operator: str = "", tag: str = "") -> None:
    """统一通知入口: 有注册函数就发, 没有就打印可见告警(不再静默)。"""
    fn = _resolve_notify_fn()
    wid = (wo or {}).get("wo_id", "")
    dept = (wo or {}).get("target_dept", "")
    try:
        import sys as _s
        print(f"[WO通知] {tag} wo={wid} dept={dept} action={action} "
              f"notify_fn={'已注册✓' if fn else 'None(跳过通知!!)'}",
              file=_s.stderr, flush=True)
    except Exception:
        pass
    if fn is None:
        return
    try:
        await fn(wo, action, operator)
    except Exception as exc:
        logger.warning("[%s] 通知失败: %s", tag or "svc", exc)
        try:
            import sys as _s
            print(f"[WO通知] {tag} 通知异常: {exc}", file=_s.stderr, flush=True)
        except Exception:
            pass


# ───────────────────── 部门-员工映射 ─────────────────────

# 部门 ID → 部门名称
_DEPT_ID_NAME = {
    "dept-1": "桂山测试",
    "dept-2": "客房服务",
    "dept-3": "酒店前台",
    "dept-4": "酒店工程",
    "dept-5": "酒店清洁",
}

# target_dept → 允许的 department_id
_DEPT_TO_DEPT_IDS = {
    "engineering": ["dept-4"],
    "housekeeping": ["dept-5", "dept-2"],  # 清洁 + 客房服务
    "frontdesk": ["dept-3"],
}

# work_type → target_dept (自动推导)
_WORK_TYPE_DEPT = {
    "维修": "engineering",
    "清洁": "housekeeping",
    "补充消耗品": "housekeeping",
    "换房清洁": "housekeeping",
    "送物": "frontdesk",
    "投诉": "frontdesk",
}


# ───────────────────── 排班表 ─────────────────────

def _load_schedules(date: str, dept: str = "") -> list:
    """读取当天在岗人员（从周排班展开）。

    周排班格式: {week_start, shifts: ["M","M","M","M","M","R","R"], ...}
    """
    try:
        rows = data_layer.load_table("schedules")
    except Exception:
        rows = []

    from datetime import datetime as _dt, timedelta as _td
    d = _dt.strptime(date, "%Y-%m-%d")
    weekday = d.weekday()  # 0=周一
    week_start = (d - _td(days=weekday)).strftime("%Y-%m-%d")

    result = []
    for r in rows:
        if r.get("deleted"):
            continue
        if dept and r.get("dept") != dept:
            continue
        if r.get("week_start") != week_start:
            continue
        shifts = r.get("shifts", [])
        if weekday < len(shifts) and shifts[weekday] in ("M", "A", "N"):
            result.append(r)
    return result


def get_on_shift_staff(target_dept: str, date: str = "") -> list:
    """获取指定部门当天在岗人员名单。

    优先级:
    1. 排班表 (schedules)
    2. 当天已有工单推断
    3. fallback 到部门全部在职员工

    返回: [(name, source)] 列表, source="schedule"|"order"|"fallback"
    """
    if not date:
        date = now()[:10]

    dept_ids = _DEPT_TO_DEPT_IDS.get(target_dept, [])
    if not dept_ids:
        return []

    # 该部门全部在职员工
    all_staff = []
    try:
        for s in data_layer.load_table("staff"):
            if s.get("deleted") or not s.get("on_duty", True):
                continue
            if s.get("department_id") in dept_ids:
                all_staff.append(s)
    except Exception:
        pass
    if not all_staff:
        return []

    staff_names = {s.get("name") for s in all_staff}

    # 1. 查排班表（周排班展开为当天）
    all_scheds = _load_schedules(date, target_dept)
    if all_scheds:
        on_shift = []
        for sch in all_scheds:
            name = sch.get("staff_name", "")
            if name in staff_names:
                # 周排班的 shifts 数组已由 _load_schedules 过滤
                on_shift.append((name, "schedule"))
        if on_shift:
            return on_shift

    # 2. 从当天工单推断
    on_shift = set()
    try:
        for w in data_layer.load_table("work_orders"):
            if w.get("deleted"):
                continue
            assignee = w.get("assignee", "")
            if assignee not in staff_names:
                continue
            created = w.get("created_at", "")
            assigned_at = w.get("assigned_at", "")
            if created[:10] == date or assigned_at[:10] == date:
                on_shift.add(assignee)
    except Exception:
        pass
    if on_shift:
        return [(name, "order") for name in on_shift]

    # 3. fallback: 全部在职
    return [(s.get("name", ""), "fallback") for s in all_staff]


def _auto_dispatch(wo: dict) -> str:
    """自动派单：排班+楼层+技能+负载 综合评分。

    评分规则:
      技能匹配 +2分, 楼层匹配 +2分, 在途工单数 -N分
      选得分最高的人（相同得分时选负载最低的）
    """
    target_dept = wo.get("target_dept", "")
    if not target_dept:
        target_dept = _WORK_TYPE_DEPT.get(wo.get("work_type", ""), "frontdesk")
        wo["target_dept"] = target_dept

    room_no = wo.get("room_no", "")
    work_type = wo.get("work_type", "")
    date = (wo.get("created_at") or now())[:10]

    # 获取排班表数据（周排班展开为当天）
    try:
        all_schedules = data_layer.load_table("schedules")
    except Exception:
        all_schedules = []

    # 展开当天排班（从周排班的 shifts 数组中取当天班次）
    from datetime import datetime as _dt, timedelta as _td
    weekday = _dt.strptime(date, "%Y-%m-%d").weekday()  # 0=周一
    week_start_date = (_dt.strptime(date, "%Y-%m-%d") - _td(days=weekday)).strftime("%Y-%m-%d")

    day_schedules = []
    for s in all_schedules:
        if s.get("deleted") or s.get("dept") != target_dept:
            continue
        if s.get("week_start") == week_start_date:
            shifts = s.get("shifts", [])
            if weekday < len(shifts) and shifts[weekday] in ("M", "A", "N"):
                day_schedules.append(s)

    # 如果有排班数据，用排班+评分模式
    if day_schedules:
        # 负载统计
        active_count = {}
        try:
            for w in data_layer.load_table("work_orders"):
                if w.get("deleted"):
                    continue
                if w.get("status") not in ("done", "rejected", "cancelled"):
                    a = w.get("assignee", "")
                    if a:
                        active_count[a] = active_count.get(a, 0) + 1
        except Exception:
            pass

        candidates = []
        for s in day_schedules:
            name = s.get("staff_name", "")
            fz = s.get("floor_zones", "")
            skills = s.get("skills", "")

            # 楼层匹配
            floor_ok = True
            if fz and room_no:
                floor_ok = False
                try:
                    floor = int(str(room_no).strip()[:1])
                    for part in fz.split(","):
                        part = part.strip()
                        if "-" in part:
                            lo, hi = int(part.split("-")[0]), int(part.split("-")[1])
                            if lo <= floor <= hi:
                                floor_ok = True
                                break
                        elif part.isdigit() and int(part) == floor:
                            floor_ok = True
                            break
                except (ValueError, IndexError):
                    floor_ok = True

            # 技能匹配
            skill_ok = True
            if skills and work_type:
                skill_ok = False
                for sk in skills.split(","):
                    sk = sk.strip()
                    if sk and (sk in work_type or work_type in sk):
                        skill_ok = True
                        break

            load = active_count.get(name, 0)
            score = (2 if skill_ok else 0) + (2 if floor_ok else 0) - load

            candidates.append((name, score, load))

        if candidates:
            # 按得分降序、负载升序排序
            candidates.sort(key=lambda x: (-x[1], x[2]))
            return candidates[0][0]

    # fallback: 原逻辑（无排班数据时）
    on_shift = get_on_shift_staff(target_dept)
    if not on_shift:
        return ""

    candidate_names = [name for name, _ in on_shift]

    active_count = {name: 0 for name in candidate_names}
    try:
        for w in data_layer.load_table("work_orders"):
            if w.get("deleted"):
                continue
            if w.get("status") not in ("done", "rejected", "cancelled"):
                a = w.get("assignee", "")
                if a in active_count:
                    active_count[a] += 1
    except Exception:
        pass

    best = min(candidate_names, key=lambda name: active_count.get(name, 0))
    return best


# ───────────────────── 内部辅助 ─────────────────────

def _resolve_assignee(assignee: str = "", assignee_id: str = "") -> tuple[str, str]:
    """根据姓名或 ID 互相查找, 返回 (assignee_name, assignee_id)"""
    if assignee_id and not assignee:
        try:
            for s in data_layer.load_table("staff"):
                if s.get("id") == assignee_id and not s.get("deleted"):
                    return s.get("name", assignee_id), assignee_id
        except Exception:
            pass
    if assignee and not assignee_id:
        try:
            for s in data_layer.load_table("staff"):
                if s.get("name") == assignee and not s.get("deleted"):
                    return assignee, s.get("id", "")
        except Exception:
            pass
    return assignee or assignee_id, assignee_id


def _calc_duration(wo: dict) -> None:
    """计算工单耗时, 写入 wo['duration'] 字段"""
    from datetime import datetime as dt
    timeline = wo.get("timeline", {})
    created = timeline.get("created_at") or wo.get("created_at", "")
    started = timeline.get("started_at", "")
    completed = timeline.get("completed_at") or wo.get("completed_at", "")
    if not created or not completed:
        return
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        t_created = dt.strptime(created[:19], fmt)
        t_completed = dt.strptime(completed[:19], fmt)
        total = int((t_completed - t_created).total_seconds() / 60)
        wait, work = 0, total
        if started:
            t_started = dt.strptime(started[:19], fmt)
            wait = int((t_started - t_created).total_seconds() / 60)
            work = int((t_completed - t_started).total_seconds() / 60)
        wo["duration"] = {
            "total_minutes": max(total, 0),
            "wait_minutes": max(wait, 0),
            "work_minutes": max(work, 0),
        }
    except Exception:
        pass


async def _apply_meta(wo: dict, source: str, operator: str) -> None:
    """标记更新来源 + 版本号 (容错)"""
    try:
        from .. import meta as _meta
        _meta.apply_meta_on_update(wo, source=source, operator=operator)
    except Exception:
        pass


# ───────────────────── 创建 ─────────────────────

async def svc_create_work_order(
    room_no: str,
    work_type: str,
    description: str,
    priority: str = "normal",
    reporter: str = "guest",
    target_dept: str = "",
    data_source: str = "manual",
    operator: str = "",
    extra: Optional[Dict[str, Any]] = None,
    auto_dispatch: bool = True,
) -> Dict[str, Any]:
    """创建工单 + safe_sync + 部门群通知 + 员工通知 (work_orders 建单统一入口)

    HTTP 路由 / agent 工具 / 房态联动 / 客人 H5 共用此函数。
    - extra: 建单时并入的额外字段 (如 guest_id)
    - auto_dispatch=False: 保留 pending 不自动派单 (房态联动"待看板派单"语义)
    """
    wo = build_work_order(
        room_no=room_no, work_type=work_type, description=description,
        priority=priority, reporter=reporter, target_dept=target_dept,
        data_source=data_source, operator=operator,
    )
    if extra:
        wo.update(extra)
    save_work_order(wo)

    # 自动派单：找该部门在途最少的员工 (可关)
    if auto_dispatch:
        assignee = _auto_dispatch(wo)
        if assignee:
            wo["assignee"] = assignee
            wo["status"] = "assigned"
            wo["assigned_at"] = now()
            wo["assigned_by"] = "auto_dispatch"
            save_work_order(wo)

    # 企微智能表格同步
    try:
        await safe_sync("work_orders", wo)
    except Exception as exc:
        logger.warning("[svc_create] safe_sync 失败: %s", exc)

    # 通知 (部门群 + 员工个人)
    await _notify_change(wo, "新建", operator, tag="svc_create")

    # 客人创建确认 (KF 来源的工单)
    try:
        is_guest_order = (
            data_source in ("guest", "guest_kf", "guest_h5")
            or wo.get("guest_id")
            or "guest" in str(reporter).lower()
        )
        if is_guest_order:
            await wecom_kf.notify_guest_work_order_change(wo, "received")
    except Exception:
        pass

    return {"ok": True, "work_order": wo}


# ───────────────────── 派单 ─────────────────────

async def svc_assign_work_order(
    wo_id: str,
    assignee: str,
    operator: str = "",
    assignee_id: str = "",
) -> Dict[str, Any]:
    """派单 + history + pending_actions 联动 + safe_sync + 通知

    返回 {"ok": True, "work_order": wo} 或 {"ok": False, "error": "..."}
    """
    assignee_name, assignee_id = _resolve_assignee(assignee, assignee_id)

    wos = data_layer.load_table("work_orders")
    wo = None
    for w in wos:
        if w.get("wo_id") == wo_id:
            wo = w
            break
    if not wo:
        return {"ok": False, "error": f"工单 {wo_id} 不存在"}
    if wo.get("status") in ("done", "rejected"):
        return {"ok": False, "error": f"工单 {wo_id} 已 {wo.get('status')}, 不能改派"}

    old_status = wo.get("status", "")
    wo["assignee"] = assignee_name
    if assignee_id:
        wo["assignee_id"] = assignee_id
    if old_status in ("pending", "pending_confirm"):
        wo["status"] = "assigned"
    wo["updated_at"] = now()
    wo["assigned_by"] = operator or "agent"
    wo["assigned_at"] = now()

    # history
    wo.setdefault("history", []).append({
        "time": now(), "from": old_status, "to": wo["status"],
        "operator": operator, "note": f"派单 → {assignee_name}",
    })
    if len(wo["history"]) > 20:
        wo["history"] = wo["history"][-20:]

    await _apply_meta(wo, source="manual", operator=operator)
    data_layer.save_table("work_orders", wos)

    # pending_actions 联动
    try:
        pas = data_layer.load_table("pending_actions")
        changed = False
        for pa in pas:
            if pa.get("status") != "pending":
                continue
            rec = pa.get("target_record") or {}
            if rec.get("wo_id") == wo_id:
                pa["status"] = "confirmed"
                pa["confirmed_by"] = operator
                pa["confirmed_at"] = now()
                changed = True
        if changed:
            data_layer.save_table("pending_actions", pas)
    except Exception as exc:
        logger.warning("[svc_assign] pending_actions 联动失败: %s", exc)

    # 企微智能表格同步
    try:
        await safe_sync("work_orders", wo)
    except Exception as exc:
        logger.warning("[svc_assign] safe_sync 失败: %s", exc)

    # 通知
    await _notify_change(wo, "派单", operator, tag="svc_assign")

    # 客人派单通知
    try:
        is_guest_order = (
            wo.get("data_source") in ("guest", "guest_kf", "guest_h5")
            or wo.get("guest_id")
            or "guest" in str(wo.get("reporter", "")).lower()
        )
        if is_guest_order:
            await wecom_kf.notify_guest_work_order_change(wo, "assigned")
    except Exception:
        pass

    return {"ok": True, "work_order": wo}


# ───────────────────── 完成 ─────────────────────

async def svc_complete_work_order(
    wo_id: str,
    operator: str = "",
    result_note: str = "",
    photo_urls: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """完成工单 + 耗时计算 + 房态联动 + pending_actions 联动 + safe_sync + 通知

    - photo_urls: 完成时并入的照片
    - force=True: 允许从 pending/pending_confirm 直接关单 (房态联动自动关单用;
      默认仍守状态机, 只接受 in_progress/assigned)
    返回 {"ok": True, "work_order": wo} 或 {"ok": False, "error": "..."}
    """
    wos = data_layer.load_table("work_orders")
    wo = None
    for w in wos:
        if w.get("wo_id") == wo_id:
            wo = w
            break
    if not wo:
        return {"ok": False, "error": f"工单 {wo_id} 不存在"}
    if wo.get("status") in ("done",):
        return {"ok": True, "deduplicated": True, "work_order": wo,
                "message": f"工单 {wo_id} 已完成, 无需重复"}

    # 状态机: 默认须经 in_progress/assigned; force=True 允许 pending 直接关单 (房态联动)
    cur = wo.get("status", "")
    _allowed = ("in_progress", "assigned") if not force else \
        ("in_progress", "assigned", "pending", "pending_confirm")
    if cur not in _allowed:
        return {"ok": False, "error": f"工单 {wo_id} 当前状态 {cur}, 不能直接完成"}
    # 从 assigned/pending 直接完成时, 自动补记接单流转
    if cur in ("assigned", "pending", "pending_confirm"):
        wo["accepted_at"] = now()
        wo.setdefault("timeline", {})["accepted_at"] = now()
        wo.setdefault("history", []).append({
            "time": now(), "from": cur, "to": "in_progress",
            "operator": operator or "system", "note": "自动接单(完成前)",
        })

    wo["status"] = "done"
    wo["completed_at"] = now()
    wo["updated_at"] = now()
    wo["completed_by"] = operator or "agent"
    if result_note:
        wo["result_note"] = result_note[:300]
    if photo_urls:
        wo["photo_urls"] = photo_urls

    # 耗时计算
    wo.setdefault("timeline", {})["completed_at"] = wo["completed_at"]
    _calc_duration(wo)
    data_layer.save_table("work_orders", wos)

    # 房态联动
    work_type = wo.get("work_type")
    room_no = wo.get("room_no")
    room_msg = ""
    if work_type in ("清洁", "维修") and room_no:
        rooms = data_layer.load_table("rooms")
        for r in rooms:
            if r.get("room_no") == room_no:
                if r.get("status") in ("维修中", "待打扫"):
                    # 有客人在住 → 保持在住，不改空房
                    if r.get("guest_name"):
                        r["status"] = "在住"
                        room_msg = f" 房间 {room_no} 有客人在住, 维修完成后保持在住状态。"
                    else:
                        r["status"] = "空房"
                        room_msg = f" 房间 {room_no} 已自动还原为空房。"
                    r["updated_at"] = now()
                    r["updated_by"] = wo.get("assignee", "ai")
                    data_layer.save_table("rooms", rooms)
                    try:
                        await safe_sync("rooms_log", {
                            "room_no": room_no, "status": r["status"],
                            "updated_at": r["updated_at"],
                            "updated_by": r["updated_by"],
                            "note": f"工单 {wo_id} 完成联动",
                        })
                    except Exception:
                        pass
                elif r.get("status") == "在住":
                    room_msg = f" 房间 {room_no} 在住中, 房态保持不变。"
                break

    # pending_actions 联动
    try:
        pendings = data_layer.load_table("pending_actions")
        changed = False
        for p in pendings:
            if (p.get("target_table") == "work_orders"
                    and (p.get("target_record") or {}).get("wo_id") == wo_id
                    and p.get("status") == "pending"):
                p["status"] = "confirmed"
                p["confirmed_by"] = wo.get("assignee") or "system"
                p["confirmed_at"] = now()
                p["confirm_note"] = f"工单 {wo_id} 完成, 自动确认"
                changed = True
        if changed:
            data_layer.save_table("pending_actions", pendings)
    except Exception as exc:
        logger.warning("[svc_complete] pending_actions 联动失败: %s", exc)

    # 企微智能表格同步
    try:
        await safe_sync("work_orders", wo)
    except Exception as exc:
        logger.warning("[svc_complete] safe_sync 失败: %s", exc)

    # 通知
    await _notify_change(wo, "完成", operator, tag="svc_complete")

    # 客人客服通知 (客人来源的工单 + 有 guest_id 的工单)
    try:
        is_guest_order = (
            wo.get("data_source") in ("guest", "guest_kf", "guest_h5")
            or wo.get("guest_id")
            or "guest" in str(wo.get("reporter", "")).lower()
        )
        if is_guest_order:
            await wecom_kf.notify_guest_work_order_change(wo, "done")
    except Exception:
        pass

    return {"ok": True, "work_order": wo, "room_msg": room_msg}


# ───────────────────── 接单 ─────────────────────

async def svc_accept_work_order(wo_id: str, operator: str = "") -> Dict[str, Any]:
    """接单: assigned → in_progress (+history/timeline + safe_sync + 通知"接单" + 客人回执)。

    员工端 / HTTP 共用。调用方负责鉴权(本人校验)。
    """
    wos = data_layer.load_table("work_orders")
    wo = None
    for w in wos:
        if w.get("wo_id") == wo_id:
            wo = w
            break
    if not wo:
        return {"ok": False, "error": f"工单 {wo_id} 不存在"}
    if wo.get("status") != "assigned":
        return {"ok": False, "error": f"工单 {wo_id} 当前状态 {wo.get('status')}, 只能接 assigned"}

    wo["status"] = "in_progress"
    wo["accepted_at"] = now()
    wo["updated_at"] = now()
    wo.setdefault("timeline", {})["accepted_at"] = wo["accepted_at"]
    wo.setdefault("history", []).append({
        "time": now(), "from": "assigned", "to": "in_progress",
        "operator": operator, "note": "接受任务",
    })
    if len(wo["history"]) > 20:
        wo["history"] = wo["history"][-20:]
    await _apply_meta(wo, source="manual", operator=operator)
    data_layer.save_table("work_orders", wos)

    try:
        await safe_sync("work_orders", wo)
    except Exception as exc:
        logger.warning("[svc_accept] safe_sync 失败: %s", exc)

    await _notify_change(wo, "接单", operator, tag="svc_accept")

    try:
        is_guest_order = (
            wo.get("data_source") in ("guest", "guest_kf", "guest_h5")
            or wo.get("guest_id")
            or "guest" in str(wo.get("reporter", "")).lower()
        )
        if is_guest_order:
            await wecom_kf.notify_guest_work_order_change(wo, "in_progress")
    except Exception:
        pass

    return {"ok": True, "work_order": wo}


# ───────────────────── 暂停/退回 ─────────────────────

async def svc_pause_work_order(wo_id: str, operator: str = "", reason: str = "") -> Dict[str, Any]:
    """暂停/退回: in_progress → assigned (+safe_sync)。调用方负责鉴权与 reason 校验。"""
    wos = data_layer.load_table("work_orders")
    wo = None
    for w in wos:
        if w.get("wo_id") == wo_id:
            wo = w
            break
    if not wo:
        return {"ok": False, "error": f"工单 {wo_id} 不存在"}
    if wo.get("status") != "in_progress":
        return {"ok": False, "error": f"工单 {wo_id} 当前状态 {wo.get('status')}, 只能暂停 in_progress"}

    wo["status"] = "assigned"
    wo["updated_at"] = now()
    wo["pause_reason"] = reason
    wo.setdefault("history", []).append({
        "time": now(), "from": "in_progress", "to": "assigned",
        "operator": operator, "note": f"暂停: {reason}",
    })
    if len(wo["history"]) > 20:
        wo["history"] = wo["history"][-20:]
    await _apply_meta(wo, source="manual", operator=operator)
    data_layer.save_table("work_orders", wos)
    try:
        await safe_sync("work_orders", wo)
    except Exception as exc:
        logger.warning("[svc_pause] safe_sync 失败: %s", exc)
    return {"ok": True, "work_order": wo}
