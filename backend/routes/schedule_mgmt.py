# -*- coding: utf-8 -*-
"""排班管理 — 酒店标准周排班

酒店7天轮班制：
  每人一周7天各有班次（早/中/晚/休），系统每天自动展开当天在岗人员。
  班次定义：
    M (早班)  06:00-14:00
    A (中班)  14:00-22:00
    N (晚班)  22:00-06:00
    R (休息)  当天休息
  每条排班记录 = 某人某周的完整7天安排 + 负责楼层 + 技能标签
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query

from .. import data_layer
from ._helpers import now, today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedule"])

# ── 常量 ──

SHIFT_DEFS = {
    "M": {"name": "早班", "start": "06:00", "end": "14:00"},
    "A": {"name": "中班", "start": "14:00", "end": "22:00"},
    "N": {"name": "晚班", "start": "22:00", "end": "06:00"},
    "R": {"name": "休息", "start": "", "end": ""},
}
SHIFT_CODES = {"早班": "M", "中班": "A", "晚班": "N", "休息": "R",
               "早": "M", "中": "A", "晚": "N", "休": "R",
               "M": "M", "A": "A", "N": "N", "R": "R"}

DEPT_LABELS = {"engineering": "酒店工程", "housekeeping": "酒店清洁", "frontdesk": "酒店前台"}
DEPT_ID_MAP = {"engineering": ["dept-4"], "housekeeping": ["dept-5", "dept-2"], "frontdesk": ["dept-3"]}

SKILL_PRESETS = {
    "engineering": ["水电", "空调", "门锁", "网络", "电视", "热水器", "综合维修"],
    "housekeeping": ["客房清洁", "深度清洁", "布草", "送物", "补货"],
    "frontdesk": ["接待", "收银", "预订", "投诉处理", "换房"],
}

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _week_start(date_str: str) -> str:
    """给定日期 → 所在周的周一日期"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def _week_dates(week_start: str) -> list:
    """周一日期 → 7天日期列表"""
    d = datetime.strptime(week_start, "%Y-%m-%d")
    return [(d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def _load_staff_map() -> Dict[str, Dict]:
    return {s.get("name", ""): s for s in data_layer.load_table("staff")
            if not s.get("deleted") and s.get("on_duty", True)}


def _get_weekday_idx(date_str: str) -> int:
    """返回 0=周一 ... 6=周日"""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday()


# ── GET /schedules/week — 周排班视图 ──

@router.get("/week")
async def get_week_schedule(
    week: str = Query("", description="周一日期 YYYY-MM-DD，空=本周"),
    dept: str = Query("", description="部门过滤"),
):
    """获取一周排班表"""
    if not week:
        week = _week_start(today())

    dates = _week_dates(week)
    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    result = []
    for s in all_scheds:
        if s.get("deleted"):
            continue
        if s.get("week_start") != week:
            continue
        if dept and s.get("dept") != dept:
            continue
        result.append(s)

    result.sort(key=lambda x: (x.get("dept", ""), x.get("staff_name", "")))

    # 统计每天在岗人数
    daily_coverage = []
    for i in range(7):
        on_duty = sum(1 for s in result if s.get("shifts", [""]*7)[i] in ("M", "A", "N"))
        daily_coverage.append({"date": dates[i], "weekday": WEEKDAY_NAMES[i], "on_duty": on_duty})

    return {
        "ok": True,
        "week_start": week,
        "dates": dates,
        "schedules": result,
        "count": len(result),
        "daily_coverage": daily_coverage,
    }


# ── GET /schedules/today — 今天在岗人员 ──

@router.get("/today")
async def get_today_schedule(dept: str = Query("")):
    """获取今天在岗人员（自动从周排班展开）"""
    d = today()
    week = _week_start(d)
    wd = _get_weekday_idx(d)

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    on_duty = []
    for s in all_scheds:
        if s.get("deleted") or s.get("week_start") != week:
            continue
        if dept and s.get("dept") != dept:
            continue
        shifts = s.get("shifts", [""] * 7)
        code = shifts[wd] if wd < len(shifts) else ""
        if code in ("M", "A", "N"):
            shift_info = SHIFT_DEFS.get(code, {})
            on_duty.append({
                "staff_name": s.get("staff_name", ""),
                "dept": s.get("dept", ""),
                "shift_code": code,
                "shift_name": shift_info.get("name", ""),
                "shift_start": shift_info.get("start", ""),
                "shift_end": shift_info.get("end", ""),
                "floor_zones": s.get("floor_zones", ""),
                "skills": s.get("skills", ""),
            })

    on_duty.sort(key=lambda x: (x["dept"], x["shift_code"], x["staff_name"]))
    return {"ok": True, "date": d, "weekday": WEEKDAY_NAMES[wd], "on_duty": on_duty, "count": len(on_duty)}


# ── POST /schedules — 创建/更新周排班 ──

@router.post("")
async def upsert_schedule(payload: dict = Body(...)):
    """创建或更新某人某周的排班

    Body:
      week_start: "2026-09-22" (周一日期，必填)
      dept: "engineering" (必填)
      staff_name: "Lee" (必填)
      shifts: ["M","M","M","M","M","R","R"] (必填, 7个元素, 周一到周日)
      floor_zones: "1-3" (可选)
      skills: "水电,空调" (可选)
    """
    week_start = payload.get("week_start", "").strip()
    dept = payload.get("dept", "").strip()
    staff_name = payload.get("staff_name", "").strip()
    shifts = payload.get("shifts", [])

    if not week_start or not dept or not staff_name:
        raise HTTPException(400, "week_start, dept, staff_name 必填")
    if not isinstance(shifts, list) or len(shifts) != 7:
        raise HTTPException(400, "shifts 必须是7个元素的数组，如 [\"M\",\"M\",\"M\",\"M\",\"M\",\"R\",\"R\"]")

    # 验证 shift code
    for i, code in enumerate(shifts):
        if code not in SHIFT_DEFS:
            raise HTTPException(400, f"shifts[{i}]=\"{code}\" 无效，有效值: M/A/N/R")

    floor_zones = payload.get("floor_zones", "").strip()
    skills = payload.get("skills", "").strip()

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    # 查找已有记录
    existing = None
    for s in all_scheds:
        if (s.get("week_start") == week_start and s.get("dept") == dept
                and s.get("staff_name") == staff_name and not s.get("deleted")):
            existing = s
            break

    if existing:
        existing["shifts"] = shifts
        existing["floor_zones"] = floor_zones
        existing["skills"] = skills
        existing["updated_at"] = now()
    else:
        all_scheds.append({
            "week_start": week_start,
            "dept": dept,
            "staff_name": staff_name,
            "shifts": shifts,
            "floor_zones": floor_zones,
            "skills": skills,
            "created_at": now(),
            "created_by": payload.get("operator", "admin"),
        })

    data_layer.save_table("schedules", all_scheds)

    # 生成可读摘要
    summary = " ".join(shifts)
    return {"ok": True, "message": f"已排班: {staff_name} {week_start}周 {summary}"}


# ── POST /schedules/bulk — 批量导入 ──

@router.post("/bulk")
async def bulk_import(payload: dict = Body(...)):
    """批量导入周排班

    Body:
      week_start: "2026-09-22"
      schedules: [
        {"dept":"engineering","staff_name":"Lee","shifts":["M","M","M","M","M","R","R"],
         "floor_zones":"1-3","skills":"水电,空调"},
        ...
      ]
      replace: true (替换该周全部)
    """
    week_start = payload.get("week_start", "").strip()
    schedules = payload.get("schedules", [])
    replace = payload.get("replace", False)

    if not week_start:
        raise HTTPException(400, "week_start 必填")

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    if replace:
        for s in all_scheds:
            if s.get("week_start") == week_start and not s.get("deleted"):
                s["deleted"] = True

    added = 0
    for item in schedules:
        shifts = item.get("shifts", [])
        if not isinstance(shifts, list) or len(shifts) != 7:
            continue
        all_scheds.append({
            "week_start": week_start,
            "dept": item.get("dept", ""),
            "staff_name": item.get("staff_name", ""),
            "shifts": shifts,
            "floor_zones": item.get("floor_zones", ""),
            "skills": item.get("skills", ""),
            "created_at": now(),
            "created_by": payload.get("operator", "bulk"),
        })
        added += 1

    data_layer.save_table("schedules", all_scheds)
    return {"ok": True, "added": added, "week_start": week_start}


# ── POST /schedules/copy-week — 复制到下一周 ──

@router.post("/copy-week")
async def copy_week(payload: dict = Body(...)):
    """复制一周排班到下一周"""
    from_week = payload.get("from_week", "").strip()
    to_week = payload.get("to_week", "").strip()
    if not from_week:
        raise HTTPException(400, "from_week 必填")
    if not to_week:
        d = datetime.strptime(from_week, "%Y-%m-%d") + timedelta(days=7)
        to_week = d.strftime("%Y-%m-%d")

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    source = [s for s in all_scheds
              if s.get("week_start") == from_week and not s.get("deleted")]
    copied = 0
    for s in source:
        all_scheds.append({
            "week_start": to_week,
            "dept": s.get("dept", ""),
            "staff_name": s.get("staff_name", ""),
            "shifts": s.get("shifts", []),
            "floor_zones": s.get("floor_zones", ""),
            "skills": s.get("skills", ""),
            "created_at": now(),
            "created_by": "copy_week",
        })
        copied += 1

    data_layer.save_table("schedules", all_scheds)
    return {"ok": True, "copied": copied, "from_week": from_week, "to_week": to_week}


# ── GET /schedules/dispatch-preview — 派单预览 ──

@router.get("/dispatch-preview")
async def dispatch_preview(
    room_no: str = Query(""),
    work_type: str = Query(""),
    date: str = Query("", description="日期，空=今天"),
    dept: str = Query(""),
):
    """派单预览：根据当天排班+楼层+技能+负载推荐人选"""
    if not date:
        date = today()

    _WORK_TYPE_DEPT = {"维修": "engineering", "清洁": "housekeeping",
                       "补充消耗品": "housekeeping", "送物": "frontdesk", "投诉": "frontdesk"}
    if not dept:
        dept = _WORK_TYPE_DEPT.get(work_type, "frontdesk")

    week = _week_start(date)
    wd = _get_weekday_idx(date)

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    # 当天排班
    day_scheds = [s for s in all_scheds
                  if s.get("week_start") == week and s.get("dept") == dept
                  and not s.get("deleted")]

    # 负载
    try:
        wos = data_layer.load_table("work_orders")
    except Exception:
        wos = []
    active_load = {}
    for w in wos:
        if w.get("status") not in ("done", "rejected", "cancelled"):
            a = w.get("assignee", "")
            if a:
                active_load[a] = active_load.get(a, 0) + 1

    candidates = []
    for s in day_scheds:
        shifts = s.get("shifts", [""] * 7)
        code = shifts[wd] if wd < len(shifts) else ""
        if code not in ("M", "A", "N"):
            continue

        name = s.get("staff_name", "")
        fz = s.get("floor_zones", "")
        skills = s.get("skills", "")

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

        skill_ok = True
        if skills and work_type:
            skill_ok = any(sk.strip() in work_type for sk in skills.split(",") if sk.strip())

        load = active_load.get(name, 0)
        score = (2 if skill_ok else 0) + (2 if floor_ok else 0) - load
        shift_info = SHIFT_DEFS.get(code, {})

        candidates.append({
            "name": name, "shift": shift_info.get("name", code),
            "shift_start": shift_info.get("start", ""), "shift_end": shift_info.get("end", ""),
            "floor_zones": fz, "skills": skills,
            "floor_match": floor_ok, "skill_match": skill_ok, "active_load": load, "score": score,
        })

    candidates.sort(key=lambda x: (-x["score"], x["active_load"]))
    best = candidates[0]["name"] if candidates else ""

    return {"ok": True, "date": date, "room_no": room_no, "work_type": work_type,
            "dept": dept, "candidates": candidates, "recommended": best}


# ── GET /schedules/overview — 排班总览 ──

@router.get("/overview")
async def schedule_overview(week: str = Query("")):
    """排班总览（周维度）"""
    if not week:
        week = _week_start(today())

    staff_map = _load_staff_map()
    dates = _week_dates(week)

    try:
        all_scheds = data_layer.load_table("schedules")
    except Exception:
        all_scheds = []

    week_scheds = [s for s in all_scheds
                   if s.get("week_start") == week and not s.get("deleted")]

    # 按部门汇总
    dept_summary = {}
    for dept_key, dept_label in DEPT_LABELS.items():
        dept_scheds = [s for s in week_scheds if s.get("dept") == dept_key]
        dept_staff_ids = DEPT_ID_MAP.get(dept_key, [])
        all_dept_staff = [s for s in staff_map.values() if s.get("department_id") in dept_staff_ids]

        scheduled_names = {s.get("staff_name") for s in dept_scheds}
        unscheduled = [s.get("name") for s in all_dept_staff if s.get("name") not in scheduled_names]

        # 每天在岗数
        daily = []
        for i in range(7):
            on_duty = sum(1 for s in dept_scheds if s.get("shifts", [""]*7)[i] in ("M", "A", "N"))
            daily.append(on_duty)

        # 楼层+技能
        floors = set()
        skills = {}
        for s in dept_scheds:
            if s.get("floor_zones"):
                for f in s["floor_zones"].split(","):
                    floors.add(f.strip())
            if s.get("skills"):
                for sk in s["skills"].split(","):
                    sk = sk.strip()
                    if sk:
                        skills[sk] = skills.get(sk, 0) + 1

        dept_summary[dept_key] = {
            "label": dept_label,
            "count": len(dept_scheds),
            "total_staff": len(all_dept_staff),
            "unscheduled": unscheduled,
            "schedules": dept_scheds,
            "daily_coverage": daily,
            "floor_coverage": sorted(floors),
            "skill_distribution": skills,
        }

    # 缺口
    gaps = []
    for dept_key, info in dept_summary.items():
        if info["count"] == 0 and info["total_staff"] > 0:
            gaps.append({"dept": dept_key, "message": f"{info['label']}本周无排班（{info['total_staff']}人在职）"})
        elif min(info["daily_coverage"]) == 0 and info["count"] > 0:
            zero_days = [WEEKDAY_NAMES[i] for i, v in enumerate(info["daily_coverage"]) if v == 0]
            gaps.append({"dept": dept_key, "message": f"{info['label']}{''.join(zero_days)}无人排班"})

    return {"ok": True, "week_start": week, "dates": dates,
            "departments": dept_summary, "gaps": gaps,
            "total": len(week_scheds), "shift_defs": SHIFT_DEFS}


def register_routes(app):
    if app:
        app.include_router(router)
