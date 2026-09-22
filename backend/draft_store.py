"""DomHotel v2 微信客服需求草稿状态存储

- 按 guest_userid（企微外部联系人 wm…）存储草稿
- 字段：description, contact_name, contact_phone, room_no
- 支持追问补齐、确认菜单、幂等去重
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_layer

logger = logging.getLogger(__name__)

_DRAFT_FILE = data_layer.DATA_DIR / "request_drafts.json"
_LOCK = threading.RLock()

REQUIRED_FIELDS = ["description", "contact_name", "contact_phone", "room_no"]


def _load() -> Dict[str, Any]:
    with _LOCK:
        if not _DRAFT_FILE.exists():
            return {}
        try:
            return json.loads(_DRAFT_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[draft] 读取草稿文件失败: %s", exc)
            return {}


def _save(data: Dict[str, Any]) -> None:
    with _LOCK:
        _DRAFT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_draft(guest_userid: str) -> Optional[Dict[str, Any]]:
    """获取客人当前草稿；过期返回 None"""
    data = _load()
    draft = data.get(guest_userid)
    if not draft:
        return None
    updated = draft.get("updated_at", "")
    try:
        if updated and datetime.now(ZoneInfo("Asia/Shanghai")) - datetime.fromisoformat(updated) > timedelta(hours=24):
            clear_draft(guest_userid)
            return None
    except Exception:
        pass
    return draft


def update_draft(guest_userid: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """更新/创建草稿，合并字段"""
    data = _load()
    draft = data.get(guest_userid, {
        "guest_userid": guest_userid,
        "description": "",
        "contact_name": "",
        "contact_phone": "",
        "room_no": "",
        "confirmed": False,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    })
    for k, v in fields.items():
        if k in REQUIRED_FIELDS or k in ("notes", "source"):
            draft[k] = str(v).strip()
    draft["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data[guest_userid] = draft
    _save(data)
    return draft


def clear_draft(guest_userid: str) -> None:
    data = _load()
    data.pop(guest_userid, None)
    _save(data)


def missing_fields(draft: Dict[str, Any]) -> list:
    return [f for f in REQUIRED_FIELDS if not draft.get(f)]


def is_complete(draft: Dict[str, Any]) -> bool:
    return not missing_fields(draft)


def ask_for_missing(draft: Dict[str, Any]) -> str:
    """生成追问文案"""
    missing = missing_fields(draft)
    prompt_map = {
        "description": "请描述您的具体需求（如：空调不制冷、需要多一套毛巾等）",
        "contact_name": "请问怎么称呼您",
        "contact_phone": "请留下您的联系电话",
        "room_no": "请告诉我您的房号",
    }
    if not missing:
        return "信息已齐全，稍后为您确认。"
    if len(missing) == 1:
        return prompt_map.get(missing[0], f"请补充：{missing[0]}")
    parts = [prompt_map.get(f, f) for f in missing]
    return "还需要您补充以下信息：\n" + "\n".join(f"{i+1}. {p}" for i, p in enumerate(parts))
