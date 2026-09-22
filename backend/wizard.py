# -*- coding: utf-8 -*-
"""DomHotel Suite — HUB 配置向导 (wizard) 路由

整合版新增模块:
  - 首次启动向导状态探测 (无需登录, 供前端决定是否弹向导)
  - 酒店名称/地址/电话/星级 保存 (向导 Step: 酒店信息)
  - 酒店房态批量生成 (向导 Step: 楼栋→楼层→房型→房号)
  - 向导完成标记 / 重置

数据存储: data_layer 的 hotel_config 表 (单行 id="hotel" + 单行 id="wizard")
房号生成规则与 rooms.py 的 _ensure_building 兼容:
  - 多栋: 房号 = 楼栋(1-9) + 楼层(1-9) + 序号(01-99), 如 1201 = 1号楼2层01房
  - 单栋: 同上 (楼栋固定为 1)
  - 自定义: 直接粘贴房号列表
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import data_layer
from .. import auth

logger = logging.getLogger("domhotel-suite.wizard")

_BACKEND_PKG_NAME = "__domhotel_suite_backend__"

router = APIRouter()

_DEFAULT_ROOM_TYPE = "标准双床房"


def _load_config_rows() -> List[Dict[str, Any]]:
    return data_layer.load_table("hotel_config")


def _get_config_row(rows: List[Dict[str, Any]], row_id: str) -> Dict[str, Any]:
    for r in rows:
        if r.get("id") == row_id:
            return r
    return {}


def _save_config_row(row_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """upsert hotel_config 表中的单行记录"""
    rows = _load_config_rows()
    row = _get_config_row(rows, row_id)
    if not row:
        row = {"id": row_id}
        rows.append(row)
    row.update(fields)
    data_layer.save_table("hotel_config", rows)
    return row


def _hotel_info() -> Dict[str, Any]:
    row = _get_config_row(_load_config_rows(), "hotel")
    if not row:
        return {"name": "", "address": "", "phone": "", "star_level": ""}
    return {
        "name": row.get("name", ""),
        "address": row.get("address", ""),
        "phone": row.get("phone", ""),
        "star_level": row.get("star_level", ""),
    }


def _wizard_completed() -> bool:
    row = _get_config_row(_load_config_rows(), "wizard")
    return bool(row.get("completed"))


_ADVANCED_EXAMPLE = {
    "buildings": [
        {
            "name": "1号楼",
            "floors": [
                {"floor": 1, "rooms": [
                    {"count": 10, "room_type": "标准双床房"},
                    {"count": 2, "room_type": "豪华大床房"},
                ]},
                {"floor": 2, "rooms": [{"count": 8, "room_type": "标准双床房"}]},
            ],
            "custom_rooms": [{"room_no": "1101V", "room_type": "行政套房"}],
        }
    ],
    "rooms": [{"room_no": "VIP-01", "room_type": "花园别墅"}],
}


def advanced_config_example() -> Dict[str, Any]:
    """advanced 模式的配置示例 (供文档/智能体引用)"""
    return json.loads(json.dumps(_ADVANCED_EXAMPLE, ensure_ascii=False))


def _expand_advanced_config(cfg: Any) -> List[Dict[str, Any]]:
    """展开结构化配置文件 (mode=advanced)

    格式 (全部字段可选, 但至少要能展开出房间):
    {
      "buildings": [            # 多栋 (最多 5, 房号 = 楼栋序+楼层+序号, 如 1201)
        {"name": "1号楼",
         "floors": [
           {"floor": 1,
            "rooms": [{"count": 10, "room_type": "标准双床房"},
                      {"count": 2, "room_type": "豪华大床房"}]},
           {"floor": 2, "rooms": [{"count": 8}]}   # room_type 缺省用 default_room_type
         ],
         "custom_rooms": [{"room_no": "1101V", "room_type": "行政套房"}]
        }
      ],
      "rooms": [{"room_no": "VIP-01", "room_type": "花园别墅"}]  # 顶层自定义房号
    }

    同层多房型段序号连续: 1层 10间标双 + 2间大床 → 1101..1110 标双, 1111..1112 大床
    """
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError as e:
            raise ValueError(f"配置 JSON 解析失败: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("config 必须是 JSON 对象")
    default_rt = str(cfg.get("default_room_type", "")).strip() or _DEFAULT_ROOM_TYPE
    rooms: List[Dict[str, Any]] = []

    def _add_custom(entries: Any) -> None:
        for cr in entries or []:
            if not isinstance(cr, dict):
                raise ValueError("custom_rooms/rooms 每项必须是 {room_no, room_type} 对象")
            no = str(cr.get("room_no", "")).strip()
            if not no:
                continue
            rooms.append({
                "room_no": no,
                "floor": _guess_floor(no),
                "room_type": str(cr.get("room_type", "")).strip() or default_rt,
                "status": "空房",
            })

    buildings = cfg.get("buildings")
    if isinstance(buildings, list) and buildings:
        if len(buildings) > 5:
            raise ValueError("楼栋数最多 5 栋 (房号编码限制)")
        for bi, b in enumerate(buildings, start=1):
            if not isinstance(b, dict):
                raise ValueError(f"buildings[{bi-1}] 必须是对象")
            for fl in b.get("floors") or []:
                if not isinstance(fl, dict):
                    raise ValueError("floors 每项必须是 {floor, rooms} 对象")
                try:
                    f = int(fl.get("floor", 1))
                except (TypeError, ValueError):
                    raise ValueError(f"楼层必须是数字: {fl.get('floor')!r}")
                if not (1 <= f <= 9):
                    raise ValueError(f"楼层范围 1-9 (房号编码限制), got {f}")
                seq = 1
                for seg in fl.get("rooms") or []:
                    if not isinstance(seg, dict):
                        raise ValueError("rooms 每项必须是 {count, room_type} 对象")
                    try:
                        count = int(seg.get("count", 0))
                    except (TypeError, ValueError):
                        raise ValueError("rooms.count 必须是数字")
                    if count < 0:
                        raise ValueError("rooms.count 不能为负")
                    rt = str(seg.get("room_type", "")).strip() or default_rt
                    for _ in range(count):
                        if seq > 99:
                            raise ValueError(f"第 {f} 层房数超过 99 (房号编码限制)")
                        rooms.append({
                            "room_no": f"{bi}{f}{seq:02d}",
                            "floor": f,
                            "room_type": rt,
                            "status": "空房",
                        })
                        seq += 1
            _add_custom(b.get("custom_rooms"))

    _add_custom(cfg.get("rooms"))
    if len(rooms) > 5000:
        raise ValueError("单次生成超过 5000 间, 请分批配置")
    return rooms


def generate_rooms(payload: dict, operator: str) -> Dict[str, Any]:
    """生成房间核心逻辑 — POST /wizard/rooms 与智能体工具 hotel_rooms_setup 共用

    raises ValueError: 参数不合法 (路由层转 400, 智能体工具层转错误 dict)
    """
    mode = str(payload.get("mode", "auto")).strip() or "auto"
    room_type = str(payload.get("room_type", "")).strip() or _DEFAULT_ROOM_TYPE

    new_rooms: List[Dict[str, Any]] = []

    if mode == "skip":
        # v1.3.3: 沿用现有房间 (前端 Step 3 已有房间时的默认选项) — 不生成任何房间。
        # 没有房间时拒绝 (防手改 DOM 绕过前端直接跳过)
        existing = data_layer.load_table("rooms")
        if not existing:
            raise ValueError("当前没有房间，无法「沿用现有」 — 请选择生成方式")
        buildings_now = sorted({str(r.get("room_no", ""))[:1] for r in existing if r.get("room_no")})
        room_types_now = sorted({str(r.get("room_type", "")) for r in existing if r.get("room_type")})
        logger.info("[wizard] 房态生成(skip): 沿用现有 %d 间 (by %s)", len(existing), operator)
        return {
            "ok": True,
            "mode": "skip",
            "created": 0,
            "skipped": 0,
            "skipped_sample": [],
            "total_rooms_now": len(existing),
            "buildings_now": [f"{b}号楼" for b in buildings_now if b.isdigit()],
            "room_types_now": room_types_now,
            "sample_room_nos": [],
            "message": f"沿用现有 {len(existing)} 间房",
        }

    if mode == "custom":
        raw = str(payload.get("custom_rooms", ""))
        seen = set()
        for tok in raw.replace("，", ",").replace("、", ",").replace("\n", ",").split(","):
            room_no = tok.strip()
            if not room_no:
                continue
            if room_no in seen:
                continue
            seen.add(room_no)
            new_rooms.append({
                "room_no": room_no,
                "floor": _guess_floor(room_no),
                "room_type": room_type,
                "status": "空房",
            })
        if not new_rooms:
            raise ValueError("自定义房号列表为空")
    elif mode == "advanced":
        new_rooms = _expand_advanced_config(payload.get("config"))
        if not new_rooms:
            raise ValueError("配置文件未展开出任何房间 (buildings/rooms 至少配一个)")
    else:
        try:
            buildings = int(payload.get("buildings", 1))
            rooms_per_floor = int(payload.get("rooms_per_floor", 8))
        except (TypeError, ValueError):
            raise ValueError("buildings/rooms_per_floor 必须是数字")
        floors_raw = payload.get("floors") or payload.get("floors_list") or [1, 2, 3]
        if isinstance(floors_raw, str):
            floors_raw = [f for f in floors_raw.replace("，", ",").split(",") if f.strip()]
        try:
            floors = sorted({int(f) for f in floors_raw})
        except (TypeError, ValueError):
            raise ValueError("floors 必须是数字列表")
        if not (1 <= buildings <= 5):
            raise ValueError("楼栋数范围 1-5")
        if not (1 <= rooms_per_floor <= 99):
            raise ValueError("每层房数范围 1-99")
        if not floors or any(not (1 <= f <= 9) for f in floors):
            raise ValueError("楼层范围 1-9 (房号编码限制)")
        for b in range(1, buildings + 1):
            for f in floors:
                for i in range(1, rooms_per_floor + 1):
                    new_rooms.append({
                        "room_no": f"{b}{f}{i:02d}",
                        "floor": f,
                        "room_type": room_type,
                        "status": "空房",
                    })

    # upsert: 已存在的 room_no 跳过 (保护已有房态/住客)
    existing = data_layer.load_table("rooms")
    existing_nos = {r.get("room_no") for r in existing}
    created = []
    skipped = []
    seen = set()
    for room in new_rooms:
        no = room["room_no"]
        if no in seen:
            continue
        seen.add(no)
        if no in existing_nos:
            skipped.append(no)
            continue
        room["created_at"] = data_layer.now_str()
        room["created_by"] = operator
        existing.append(room)
        existing_nos.add(no)
        created.append(no)
    if created:
        data_layer.save_table("rooms", existing)
    _save_config_row("wizard", {
        "rooms_generated": len(created) + _get_config_row(_load_config_rows(), "wizard").get("rooms_generated", 0),
        "last_generate_at": data_layer.now_str(),
    })
    buildings_now = sorted({str(r.get("room_no", "")[:1]) for r in existing if r.get("room_no")})
    room_types_now = sorted({str(r.get("room_type", "")) for r in existing if r.get("room_type")})
    logger.info("[wizard] 房态生成(%s): 新增 %d 间 / 跳过 %d 间 (by %s)",
                mode, len(created), len(skipped), operator)
    return {
        "ok": True,
        "mode": mode,
        "created": len(created),
        "skipped": len(skipped),
        "skipped_sample": skipped[:10],
        "total_rooms_now": len(existing),
        "buildings_now": [f"{b}号楼" for b in buildings_now if b.isdigit()],
        "room_types_now": room_types_now,
        "sample_room_nos": list(seen)[:8],
        "message": f"已生成 {len(created)} 间房"
                   + (f"，跳过已存在 {len(skipped)} 间" if skipped else ""),
    }


def register_routes(app) -> None:
    """routes/__init__.py 的合并 router 机制要求: 触发 @router 装饰器"""

    # v1.3.1 向导初始化守卫:
    #   向导是首启初始化流程, 与 auth/setup 首次创建超管同理 —
    #   未完成 (wizard_completed=False) 时放行任何身份 (含未登录 guest),
    #   操作人优先记 cookie 身份, 否则记 "wizard_init";
    #   已完成后重跑向导 (= 修改配置) 才要求 employee 以上权限。
    #   解决: 全新部署还没账号时被 403 卡在「保存酒店名称」步。
    _employee_dep = auth.require_employee()

    async def _wizard_guard(request: Request) -> Dict[str, Any]:
        if not _wizard_completed():
            try:
                sess = auth.get_session(request)
                if sess.get("role") in ("super_admin", "manager", "employee"):
                    return sess
            except Exception:
                pass
            return {"user_id": "", "staff_id": "", "role": "guest", "name": ""}
        return await _employee_dep(request)

    wizard_guard = Depends(_wizard_guard)

    # ─────────────────────────────────────────────
    # 向导状态 (无需登录 — 前端首屏探测用)
    # ─────────────────────────────────────────────
    @router.get("/wizard/status")
    async def wizard_status() -> Dict[str, Any]:
        """向导状态探测: 前端据此决定是否显示配置向导

        - initialized: 权限系统是否已创建超管 (等价 /auth/setup-status)
        - wizard_completed: 本向导是否已完成
        - hotel: 酒店信息 (可能为空)
        - rooms_count: 当前房间数
        - rooms_stats: 楼栋/房型分布 (v1.3.3 — Step 3 读取现有配置用;
          /rooms 需登录而本端点无需登录, 未登录初始化模式也能看到现有统计)
        """
        initialized = False
        try:
            initialized = len(auth.load_staff_rows()) > 0
        except Exception:
            initialized = False
        rooms_rows = data_layer.load_table("rooms")
        rooms_count = len(rooms_rows)
        b_set = set()
        rt_map: Dict[str, int] = {}
        for r in rooms_rows:
            no = str(r.get("room_no", ""))
            if len(no) == 4 and no.isdigit():
                b_set.add(no[0] + "号楼")
            elif len(no) == 3 and no.isdigit():
                b_set.add("1号楼")
            rt = str(r.get("room_type", "")).strip()
            if rt:
                rt_map[rt] = rt_map.get(rt, 0) + 1
        return {
            "ok": True,
            "pawapp": "domhotel-suite",
            "initialized": initialized,
            "wizard_completed": _wizard_completed(),
            "hotel": _hotel_info(),
            "rooms_count": rooms_count,
            "rooms_stats": {
                "buildings": sorted(b_set),
                "room_types": sorted(rt_map.keys(), key=lambda k: -rt_map[k]),
                "type_counts": rt_map,
            },
        }

    # ─────────────────────────────────────────────
    # 读取酒店配置 (需登录)
    # ─────────────────────────────────────────────
    @router.get("/wizard/config")
    async def wizard_config(
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        row = _get_config_row(_load_config_rows(), "wizard")
        return {
            "ok": True,
            "hotel": _hotel_info(),
            "wizard_completed": bool(row.get("completed")),
            "completed_at": row.get("completed_at", ""),
            "rooms_generated": row.get("rooms_generated", 0),
        }

    # ─────────────────────────────────────────────
    # 保存酒店信息 (需登录) — 向导 Step: 酒店名称
    # ─────────────────────────────────────────────
    @router.post("/wizard/hotel")
    async def wizard_save_hotel(
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="酒店名称不能为空")
        if len(name) > 60:
            raise HTTPException(status_code=400, detail="酒店名称过长 (最多 60 字)")
        address = str(payload.get("address", "")).strip()[:200]
        phone = str(payload.get("phone", "")).strip()[:40]
        star_level = str(payload.get("star_level", "")).strip()[:10]
        row = _save_config_row("hotel", {
            "name": name,
            "address": address,
            "phone": phone,
            "star_level": star_level,
            "updated_at": data_layer.now_str(),
            "updated_by": sess.get("name") or sess.get("staff_id") or "wizard_init",
        })
        logger.info("[wizard] 酒店信息已保存: %s (by %s)", name, row.get("updated_by"))
        return {"ok": True, "hotel": _hotel_info(),
                "message": f"酒店信息已保存: {name}"}

    # ─────────────────────────────────────────────
    # 生成酒店房态 (需登录) — 向导 Step: 房态配置
    # ─────────────────────────────────────────────
    @router.post("/wizard/rooms")
    async def wizard_generate_rooms(
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        """按 楼栋→楼层→房型→房号 批量生成房间

        mode=auto:
          buildings: 楼栋数 1-5 (int)
          floors: 楼层列表 (list[int], 每项 1-9, 如 [1,2,3,5,6])
          rooms_per_floor: 每层房数 1-99 (int)
          room_type: 房型名 (默认 标准双床房)
        mode=custom:
          custom_rooms: 房号文本 (逗号/换行分隔), 房型用 room_type
        mode=advanced (v1.3.2):
          config: 结构化配置 (dict 或 JSON 字符串) — 不同楼层不同房型/自定义房号,
          格式见 advanced_config_example(); 也可由智能体工具 hotel_rooms_setup 生成
        幂等: 已存在的 room_no 跳过 (不覆盖已有房态/在住客人)
        """
        operator = sess.get("name") or sess.get("staff_id") or "wizard_init"
        try:
            return generate_rooms(payload, operator)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/wizard/complete")
    async def wizard_complete(
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        _save_config_row("wizard", {
            "completed": True,
            "completed_at": data_layer.now_str(),
            "completed_by": sess.get("name") or sess.get("staff_id") or "wizard_init",
        })
        rooms_count = len(data_layer.load_table("rooms"))
        if rooms_count == 0:
            return {"ok": True, "warning": "向导已完成，但尚未生成任何房间",
                    "rooms_count": 0}
        return {"ok": True, "rooms_count": rooms_count,
                "hotel": _hotel_info(),
                "message": "配置完成，欢迎进入工作台"}

    @router.post("/wizard/reset")
    async def wizard_reset(
        sess: Dict[str, Any] = Depends(auth.require_super_admin()),
    ) -> Dict[str, Any]:
        """重置向导 (重新跑一遍酒店名称/房态配置)"""
        _save_config_row("wizard", {
            "completed": False,
            "completed_at": "",
            "completed_by": "",
        })
        return {"ok": True, "message": "向导已重置，刷新页面重新进入配置"}

    # ─────────────────────────────────────────────
    # v1.4.1: AI 助手部署（向导 Step 5）
    # ─────────────────────────────────────────────
    @router.post("/wizard/ai-deploy")
    async def wizard_ai_deploy(
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        """一键部署 4 个 AI 助手智能体模板"""
        import importlib
        try:
            ai_deploy = importlib.import_module(f"{_BACKEND_PKG_NAME}.routes.ai_deploy")
            result = await ai_deploy.deploy_all()
            return result
        except Exception as exc:
            logger.exception("[wizard] AI 助手部署异常")
            return {"ok": False, "error": str(exc)[:200]}

    # ─────────────────────────────────────────────
    # v1.4.1: 全链路冒烟检测（向导 Step 6）
    # ─────────────────────────────────────────────
    @router.get("/wizard/smoke")
    async def wizard_smoke(
        sess: Dict[str, Any] = wizard_guard,
    ) -> Dict[str, Any]:
        """向导冒烟检测：复用 /admin/smoke-test 逻辑"""
        import os, httpx
        from . import wecom_sync

        checks = []
        overall = True

        # 1. 数据目录
        try:
            test_file = data_layer.DATA_DIR / ".wizard_smoke_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            checks.append({"name": "data_dir", "ok": True, "message": "数据目录可写"})
        except Exception as exc:
            overall = False
            checks.append({"name": "data_dir", "ok": False, "message": f"数据目录不可写: {exc}"})

        # 2. 凭证文件
        try:
            secrets_path = data_layer.DATA_DIR / ".wecom_secrets"
            if secrets_path.exists():
                mode = secrets_path.stat().st_mode & 0o777
                ok = mode == 0o600
                checks.append({"name": "secrets", "ok": ok, "message": f".wecom_secrets 权限 {oct(mode)}"})
                if not ok:
                    overall = False
            else:
                checks.append({"name": "secrets", "ok": True, "message": "未使用独立 secrets 文件"})
        except Exception as exc:
            checks.append({"name": "secrets", "ok": False, "message": str(exc)})
            overall = False

        # 3. 企微 API
        try:
            probe = await wecom_sync.live_probe(force=True)
            if probe.get("api_ok"):
                checks.append({"name": "wecom_api", "ok": True, "message": "企微 API 链路全通"})
            elif probe.get("token_ok") and probe.get("errcode") == 60020:
                overall = False
                checks.append({"name": "wecom_api", "ok": False, "message": f"出口 IP {probe.get('client_ip')} 不在企微可信白名单", "client_ip": probe.get("client_ip")})
            else:
                overall = False
                checks.append({"name": "wecom_api", "ok": False, "message": f"企微 API 探测失败: {probe.get('errmsg')}"})
        except Exception as exc:
            overall = False
            checks.append({"name": "wecom_api", "ok": False, "message": f"探测异常: {exc}"})

        # 4. 微信客服
        try:
            kf_id = wecom_sync.get_kf_setting("WECOM_KF_ID") or os.environ.get("WECOM_KF_ID", "")
            kf_token = wecom_sync.get_kf_setting("WECOM_KF_TOKEN") or os.environ.get("WECOM_KF_TOKEN", "")
            kf_aes = wecom_sync.get_kf_setting("WECOM_KF_ENCODING_AES_KEY") or os.environ.get("WECOM_KF_ENCODING_AES_KEY", "")
            kf_ok = bool(kf_id and kf_token and kf_aes)
            checks.append({"name": "kf_config", "ok": kf_ok, "message": "微信客服已配置" if kf_ok else "微信客服未配置完整"})
            if not kf_ok:
                overall = False
        except Exception as exc:
            overall = False
            checks.append({"name": "kf_config", "ok": False, "message": str(exc)})

        # 5. 回调 URL
        try:
            base_url = wecom_sync.get_kf_setting("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")
            if base_url:
                callback_url = f"{base_url}/api/domhotel-suite/kf/callback"
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(callback_url, params={"msg_signature": "x", "timestamp": "1", "nonce": "x", "echostr": "x"})
                checks.append({"name": "callback", "ok": r.status_code in (200, 403), "message": f"回调 URL 返回 {r.status_code}"})
                if r.status_code not in (200, 403):
                    overall = False
            else:
                checks.append({"name": "callback", "ok": False, "message": "未配置回调 URL"})
                overall = False
        except Exception as exc:
            overall = False
            checks.append({"name": "callback", "ok": False, "message": f"回调 URL 不可达: {exc}"})

        # 6. AI 智能体
        try:
            agent_id = wecom_sync.get_kf_setting("KF_AI_AGENT_ID") or os.environ.get("KF_AI_AGENT_ID", "hotel-ai-guest-service")
            checks.append({"name": "ai_agent", "ok": bool(agent_id), "message": f"AI 智能体: {agent_id}"})
        except Exception as exc:
            checks.append({"name": "ai_agent", "ok": False, "message": str(exc)})

        next_steps = []
        for c in checks:
            if c["ok"]:
                continue
            if c["name"] == "wecom_api" and "可信白名单" in c.get("message", ""):
                next_steps.append(f"请在企微后台添加可信 IP: {c.get('client_ip', '')}")
            elif c["name"] == "kf_config":
                next_steps.append("请配置 WECOM_KF_ID / WECOM_KF_TOKEN / WECOM_KF_ENCODING_AES_KEY")
            elif c["name"] == "callback":
                next_steps.append("请检查回调 URL 配置")
            elif c["name"] == "data_dir":
                next_steps.append("请检查数据目录权限")
        if not next_steps and overall:
            next_steps.append("全链路检测通过！")

        return {
            "ok": overall,
            "checks": checks,
            "next_steps": next_steps,
        }


def _guess_floor(room_no: str) -> int:
    """从房号猜测楼层 (custom 模式用, 兼容 _ensure_building 规则)"""
    digits = "".join(c for c in str(room_no) if c.isdigit())
    if len(digits) == 4 and digits[0] in "012345":
        # 4 位: 楼栋+楼层+序号
        try:
            f = int(digits[1])
            if f > 0:
                return f
        except ValueError:
            pass
    if len(digits) == 3:
        try:
            f = int(digits[0])
            if f > 0:
                return f
        except ValueError:
            pass
    return 1
