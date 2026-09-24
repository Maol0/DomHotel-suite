# -*- coding: utf-8 -*-
"""客人档案模块 — 记录偏好与历史需求，用于 AI 个性化应答

v1.4.1: 轻量实现，按 external_userid 或 room_no+guest_name 聚合。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import data_layer

_PROFILE_FILE = Path(data_layer.DATA_DIR) / "guest_profiles.json"
_MAX_HISTORY = 10


def _load_profiles() -> Dict[str, Any]:
    if not _PROFILE_FILE.exists():
        return {}
    try:
        data = json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_profiles(profiles: Dict[str, Any]) -> None:
    _PROFILE_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")


def _profile_key(external_userid: str = "", room_no: str = "", guest_name: str = "") -> str:
    if external_userid:
        return f"kf:{external_userid}"
    if room_no and guest_name:
        return f"room:{room_no}:{guest_name}"
    if room_no:
        return f"room:{room_no}"
    return ""


def update_guest_profile(
    work_order: Dict[str, Any],
    external_userid: str = "",
    room_no: str = "",
    guest_name: str = "",
) -> None:
    """创建/更新客人档案

    在工单创建时调用，把本次需求追加到客人历史。
    """
    key = _profile_key(external_userid, room_no, guest_name)
    if not key:
        return

    profiles = _load_profiles()
    profile = profiles.setdefault(key, {
        "external_userid": external_userid,
        "room_no": room_no,
        "guest_name": guest_name,
        "first_seen": work_order.get("created_at", ""),
        "preferences": {},
        "history": [],
    })

    profile["last_seen"] = work_order.get("created_at", "")
    profile["room_no"] = room_no or profile.get("room_no", "")
    profile["guest_name"] = guest_name or profile.get("guest_name", "")

    # 追加历史
    history = profile.setdefault("history", [])
    history.insert(0, {
        "wo_id": work_order.get("wo_id"),
        "work_type": work_order.get("work_type"),
        "description": work_order.get("description"),
        "status": work_order.get("status"),
        "created_at": work_order.get("created_at"),
    })
    profile["history"] = history[:_MAX_HISTORY]

    # 简单偏好统计：需求类型频次
    work_type = work_order.get("work_type", "")
    if work_type:
        prefs = profile.setdefault("preferences", {})
        prefs.setdefault("work_types", {})
        prefs["work_types"][work_type] = prefs["work_types"].get(work_type, 0) + 1

    _save_profiles(profiles)


def get_guest_profile_context(
    external_userid: str = "",
    room_no: str = "",
    guest_name: str = "",
) -> str:
    """生成供 AI 参考的客人档案上下文"""
    key = _profile_key(external_userid, room_no, guest_name)
    if not key:
        return ""

    profiles = _load_profiles()
    profile = profiles.get(key)
    if not profile:
        return ""

    history = profile.get("history", [])
    if not history:
        return ""

    lines = ["【客人档案】"]
    lines.append(f"历史需求：")
    for h in history[:5]:
        lines.append(f"- {h.get('created_at','')} {h.get('work_type','')}：{h.get('description','')}")

    prefs = profile.get("preferences", {})
    wt_prefs = prefs.get("work_types", {})
    if wt_prefs:
        top = sorted(wt_prefs.items(), key=lambda x: x[1], reverse=True)[:3]
        lines.append(f"常见需求：{', '.join(f'{k}({v}次)' for k, v in top)}")

    return "\n".join(lines) + "\n"


def get_guest_profile(
    external_userid: str = "",
    room_no: str = "",
    guest_name: str = "",
) -> Optional[Dict[str, Any]]:
    """获取完整客人档案"""
    key = _profile_key(external_userid, room_no, guest_name)
    if not key:
        return None
    return _load_profiles().get(key)



# ── v2.4-identity: 身份握手 (external_userid ↔ openid ↔ 前台入住 PMS) ──────────
# 退房不清档: stays/identity 永久保留, 回访凭 external_userid 带出上一住房型/偏好。
import re as _re
from datetime import datetime, timezone, timedelta

_ZI = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_ZI).strftime("%Y-%m-%d %H:%M:%S")


def _np(p: str) -> str:
    return _re.sub(r"\D", "", str(p or ""))


def _room_type_of(room_no: str) -> str:
    if not room_no:
        return ""
    try:
        for r in data_layer.load_table("rooms"):
            if str(r.get("room_no", "")).strip() == str(room_no).strip():
                return str(r.get("room_type", "") or "")
    except Exception:
        pass
    return ""


def _find_pms_checkin(phone: str, name: str) -> Dict[str, Any]:
    """与前台入住表(checkins)握手: 归一手机号精确优先, 其次姓名唯一命中。
    优先 in_house, 否则全部里取最近一条。返回 checkin dict 或 {}。"""
    try:
        rows = data_layer.load_table("checkins")
    except Exception:
        return {}
    np_ = _np(phone)
    name = (name or "").strip()
    in_house = [c for c in rows if c.get("status") == "in_house"]
    for pool in ([in_house, rows]):
        if not pool:
            continue
        if np_:
            m = [c for c in pool if _np(c.get("phone")) == np_]
            if m:
                return sorted(m, key=lambda c: str(c.get("checkin_time") or c.get("created_at") or ""), reverse=True)[0]
        if name:
            m = [c for c in pool if str(c.get("guest_name") or "").strip() == name]
            if len(m) == 1:
                return m[0]
    return {}


def _get_or_create_profile(profiles, ext, guest_name="", guest_phone=""):
    key = "kf:" + ext
    p = profiles.get(key)
    if p is None:
        p = {"external_userid": ext, "identity": {}, "preferences": {},
             "history": [], "stays": []}
        profiles[key] = p
    p["external_userid"] = ext
    p.setdefault("identity", {})
    p.setdefault("stays", [])
    ident = p["identity"]
    if guest_name:
        ident["guest_name"] = guest_name
    if guest_phone:
        ident["phone"] = guest_phone
    return key, p


def record_openid(external_userid, openid):
    """缓存 openid — 未绑房访客也存, 补齐早期转换结果被丢弃缺口。"""
    if not external_userid or not openid:
        return
    profiles = _load_profiles()
    _, p = _get_or_create_profile(profiles, external_userid)
    p.setdefault("identity", {})["openid"] = openid
    _save_profiles(profiles)


def record_bind(external_userid, room_no, guest_name, guest_phone="", openid=""):
    """绑房握手: 关联前台入住(PMS)+房型, 记录一次住宿到 stays, 跨退房保留。"""
    if not external_userid or not room_no:
        return {}
    profiles = _load_profiles()
    key, p = _get_or_create_profile(profiles, external_userid, guest_name, guest_phone)
    ident = p["identity"]
    if openid:
        ident["openid"] = openid
    pms = _find_pms_checkin(guest_phone or ident.get("phone", ""),
                            guest_name or ident.get("guest_name", ""))
    true_room = str((pms or {}).get("room_no") or room_no).strip() or str(room_no).strip()
    if pms:
        ident["pms_checkin_id"] = pms.get("id", "")
        if pms.get("phone"):
            ident["phone"] = pms["phone"]
        if pms.get("guest_name") and not ident.get("guest_name"):
            ident["guest_name"] = pms["guest_name"]
    rt = _room_type_of(true_room)
    p["last_room_no"] = true_room
    p["last_room_type"] = rt
    p["last_seen"] = _now()
    stays = p.setdefault("stays", [])
    cur = next((s for s in stays
                if str(s.get("room_no")) == true_room and not s.get("checkout_at")), None)
    if cur:
        cur["room_type"] = rt or cur.get("room_type", "")
    else:
        stays.append({"room_no": true_room, "room_type": rt,
                      "checkin_at": _now(), "checkout_at": ""})
    p["stays"] = stays[-30:]
    _save_profiles(profiles)
    return {"key": key, "room_no": true_room, "room_type": rt, "pms_matched": bool(pms)}


def record_checkout(external_userid, room_no=""):
    """退房: 关闭未结 stay, 但保留整条身份与历史(回访识别用)。"""
    if not external_userid:
        return
    profiles = _load_profiles()
    p = profiles.get("kf:" + external_userid)
    if not p:
        return
    ts = _now()
    for s in p.get("stays", []):
        if not s.get("checkout_at") and (not room_no or str(s.get("room_no")) == str(room_no)):
            s["checkout_at"] = ts
    _save_profiles(profiles)


def get_last_stay(external_userid):
    """回访欢迎语用: 上次住的房号/房型 + 入住次数。"""
    if not external_userid:
        return {}
    p = _load_profiles().get("kf:" + external_userid)
    if not p:
        return {}
    stays = p.get("stays") or []
    if not stays:
        return {"room_no": p.get("last_room_no", ""),
                "room_type": p.get("last_room_type", ""), "stay_count": 0}
    last = stays[-1]
    return {"room_no": last.get("room_no", ""),
            "room_type": last.get("room_type", ""), "stay_count": len(stays)}


def get_identity(external_userid):
    """诊断/握手用: 完整身份视图 (openid/phone/PMS 关联/住宿史/偏好)。"""
    p = _load_profiles().get("kf:" + external_userid) if external_userid else None
    if not p:
        return {}
    ident = p.get("identity", {})
    return {
        "external_userid": external_userid,
        "openid": ident.get("openid", ""),
        "phone": ident.get("phone", ""),
        "guest_name": ident.get("guest_name", ""),
        "pms_checkin_id": ident.get("pms_checkin_id", ""),
        "last_room_no": p.get("last_room_no", ""),
        "last_room_type": p.get("last_room_type", ""),
        "stay_count": len(p.get("stays") or []),
        "stays": (p.get("stays") or [])[-10:],
        "preferences": p.get("preferences", {}),
    }
