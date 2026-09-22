# -*- coding: utf-8 -*-
"""DomHotel Suite — 酒店初始化智能体工具 (v1.3.2)

供 QwenPaw 智能体调用, 让用户在对话中直接口述配置:
  - hotel_rooms_setup: 批量生成酒店房间 (复杂房型结构传 config_json)

配合 wecom_tools 的 wecom_config_set / wecom_status, 智能体可完成整套初始化:
  酒店信息 + 房态生成 + 企微凭证 + 重推失败队列。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("domhotel-suite.hotel_tools")


async def hotel_rooms_setup(
    config_json: str = "",
    mode: str = "auto",
    buildings: int = 1,
    floors: str = "1,2,3",
    rooms_per_floor: int = 8,
    room_type: str = "标准双床房",
    hotel_name: str = "",
    hotel_address: str = "",
    hotel_phone: str = "",
) -> Dict[str, Any]:
    """为酒店批量生成房间 (配置向导第 3 步的智能体入口), 可顺带保存酒店基本信息。

    何时调用: 用户在对话中描述酒店结构并希望由智能体代为配置时调用,
    例如「帮我建两栋楼, 每栋 3 层, 每层 10 间标准双床房」或「1-2 楼是标间, 3 楼是套房」。

    两种用法 (二选一, config_json 优先):
    1. 简单场景 — 快捷参数: mode="auto", buildings=楼栋数(1-5),
       floors="楼层逗号分隔"(1-9), rooms_per_floor=每层房数(1-99), room_type=房型名。
       房号自动 = 楼栋序+楼层+序号, 如 1201 = 1号楼2层01房。
    2. 复杂场景 — config_json 传结构化配置 (不同楼层不同房型 / 自定义房号):
       {"buildings": [
           {"name": "1号楼",
            "floors": [
              {"floor": 1, "rooms": [
                 {"count": 10, "room_type": "标准双床房"},
                 {"count": 2,  "room_type": "豪华大床房"}]},
              {"floor": 2, "rooms": [{"count": 8}]}],
            "custom_rooms": [{"room_no": "1101V", "room_type": "行政套房"}]}],
        "rooms": [{"room_no": "VIP-01", "room_type": "花园别墅"}]}

    可选: hotel_name / hotel_address / hotel_phone 顺带保存酒店基本信息 (向导第 2 步)。

    幂等: 已存在的房号自动跳过 (不覆盖在住信息), 可安全重复调用。
    """
    from . import wizard

    result: Dict[str, Any] = {}

    # ── 可选: 先保存酒店基本信息 ──
    if hotel_name and hotel_name.strip():
        try:
            row = wizard._save_config_row("hotel", {
                "name": hotel_name.strip()[:60],
                "address": (hotel_address or "").strip()[:200],
                "phone": (hotel_phone or "").strip()[:40],
                "updated_at": wizard.data_layer.now_str(),
                "updated_by": "agent",
            })
            result["hotel"] = {
                "name": row.get("name", ""),
                "address": row.get("address", ""),
                "phone": row.get("phone", ""),
            }
            result["hotel_saved"] = True
        except Exception as e:  # 酒店信息失败不阻断房间生成
            result["hotel_saved"] = False
            result["hotel_error"] = str(e)[:200]

    # ── 生成房间 (与 POST /wizard/rooms 同一实现) ──
    payload: Dict[str, Any] = {}
    if config_json and config_json.strip():
        payload = {"mode": "advanced", "config": config_json}
    else:
        payload = {
            "mode": mode or "auto",
            "buildings": buildings,
            "floors": floors,
            "rooms_per_floor": rooms_per_floor,
            "room_type": room_type or "标准双床房",
        }
    try:
        rooms_result = wizard.generate_rooms(payload, "agent")
        result.update(rooms_result)
        result["next_step"] = _next_step_after_rooms(rooms_result)
    except ValueError as e:
        return {
            "ok": False,
            "error": str(e),
            "hint": "请修正参数后重试。简单场景用 mode=auto + buildings/floors/"
                    "rooms_per_floor/room_type; 复杂楼层房型结构传 config_json。",
            "config_example": wizard.advanced_config_example(),
        }

    logger.info("[hotel_tools] hotel_rooms_setup: %s", result.get("message", ""))
    return result


def _next_step_after_rooms(rooms_result: Dict[str, Any]) -> str:
    """生成房间后的下一步指引 (对齐配置向导流程)"""
    if rooms_result.get("created", 0) == 0 and rooms_result.get("skipped", 0) > 0:
        return "所有房号均已存在, 无新增。如需调整可在房态看板微调, 或让用户在「🧭 配置向导」重跑。"
    return ("房间已生成。下一步建议引导用户完成企业微信部署 (向导第 4 步): "
            "可调用 wecom_status 检查链路状态, 未配置凭证时用 wecom_config_set 写入; "
            "完成后提示用户刷新页面进入工作台。")


def register_tools(app) -> int:
    """routes/__init__.py 的 register_all 阶段调用: 注册智能体工具"""
    app.tool(
        "hotel_rooms_setup",
        description="为酒店批量生成房间 (配置向导第 3 步的智能体入口)。"
                    "简单场景传 mode=auto + buildings/floors/rooms_per_floor/room_type;"
                    "复杂楼层房型结构传 config_json; 可顺带保存 hotel_name 等酒店信息。"
                    "幂等可重复调用。",
        icon="🏨",
    )(hotel_rooms_setup)
    return 1
