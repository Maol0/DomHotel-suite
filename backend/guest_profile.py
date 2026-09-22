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
