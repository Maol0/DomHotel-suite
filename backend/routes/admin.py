# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 后台配置中心路由

管理 4 类资源:departments / staff / room_types / floors
另有 1 个特殊路由:POST /admin/rooms/bulk_upsert (批量新增/更新房间,不删除)
"""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Body

from .. import data_layer
from .. import auth
from .. import guest_profile
from .. import wecom_sync
from .. import meta as _meta
from ._helpers import now, new_id


# v2.3.0: 房号 → 楼栋 推导规则 (与 rooms.py _ensure_building 同步, 避免循环依赖)
def _ensure_building_local(room: Dict[str, Any]) -> Dict[str, Any]:
    """根据 room_no 自动推导并补全 building 字段（不修改原 dict）"""
    if room.get("building"):
        return room
    room_no = str(room.get("room_no", "")).strip()
    if not room_no:
        room["building"] = ""
        return room
    if len(room_no) == 4 and room_no.isdigit() and room_no[0] in "12345":
        room["building"] = f"{room_no[0]}号楼"
        room["building_block"] = f"BLOCK {room_no[0]}"
        return room
    if len(room_no) == 4 and room_no.isdigit() and room_no[0] == "0":
        b = room_no[1]
        if b in "12345":
            room["building"] = f"{b}号楼"
            room["building_block"] = f"BLOCK {b}"
            return room
    if len(room_no) == 3 and room_no.isdigit():
        b = room_no[0]
        if b in "12345":
            room["building"] = f"{b}号楼"
            room["building_block"] = f"BLOCK {b}"
            return room
    if room_no and room_no[0].isalpha():
        alpha_map = {"A": "1号楼", "B": "2号楼", "C": "3号楼", "D": "4号楼", "E": "5号楼"}
        if room_no[0] in alpha_map:
            room["building"] = alpha_map[room_no[0]]
            room["building_block"] = f"BLOCK {room_no[0]}"
            return room
    room["building"] = ""
    return room

logger = logging.getLogger(__name__)

# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()


ADMIN_RESOURCES = {"departments", "staff", "room_types", "floors"}
ADMIN_REQUIRED = {
    "departments": ["name"],
    "staff": ["name", "department_id"],
    "room_types": ["name"],
    "floors": ["name"],
}

# Phase 5.1: 企微通讯录对齐字段（暂未激活拉取逻辑,只留 schema 钩子）
STAFF_WECOM_FIELDS = {
    "wecom_userid": None,         # 企微 userid,可空（手工对齐 / 未来通讯录同步）
    "wecom_sync_status": "local_only",  # local_only / synced / pending / left
    "wecom_last_synced_at": None, # 最近一次与企微对账的时间戳
}


def _admin_load(resource: str, include_deleted: bool = False) -> List[Dict[str, Any]]:
    rows = data_layer.load_table(resource)
    if not include_deleted:
        rows = [r for r in rows if not r.get("deleted")]
    return rows


def _admin_find(resource: str, item_id: str):
    for r in _admin_load(resource, include_deleted=True):
        if r.get("id") == item_id:
            return r
    return None


def register_routes(app) -> None:
    """注册后台配置路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/admin/diag")
    async def diag():
        """查看当前版本的数据目录与文件清单(健康诊断)

        ⚠️ 必须放在 /admin/{resource} 之前,否则 FastAPI 会
        把 'diag' 当成 resource 匹配走 admin_list,返回 400。
        """
        return data_layer.version_info()

    # v1.4.1: 知识库管理（必须在 /admin/{resource} 通配之前注册）
    _KB_PATH = Path(data_layer.DATA_DIR) / "knowledge_base.md"

    @router.get("/admin/knowledge-base")
    async def admin_get_knowledge_base(ctx=Depends(get_ctx)):
        """读取酒店知识库（Markdown 格式）"""
        if not _KB_PATH.exists():
            return {"ok": True, "content": "", "path": str(_KB_PATH)}
        return {
            "ok": True,
            "content": _KB_PATH.read_text(encoding="utf-8"),
            "path": str(_KB_PATH),
        }

    @router.post("/admin/knowledge-base")
    async def admin_save_knowledge_base(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
    ):
        """保存酒店知识库（Markdown 格式）

        Request Body:
          {"content": "# 标题\n..."}
        """
        content = payload.get("content", "")
        if not isinstance(content, str):
            raise HTTPException(400, detail="content 必须是字符串")
        try:
            _KB_PATH.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(_KB_PATH), "length": len(content)}
        except Exception as exc:
            raise HTTPException(500, detail=f"保存失败: {exc}")

    # v1.4.1: 客人档案查询（必须在 /admin/{resource} 通配之前注册）
    @router.get("/admin/guest-profiles")
    async def admin_guest_profiles(
        ctx=Depends(get_ctx),
        room_no: str = "",
        guest_name: str = "",
        external_userid: str = "",
    ):
        """查询客人档案（历史需求、偏好）"""
        profile = guest_profile.get_guest_profile(
            external_userid=external_userid,
            room_no=room_no,
            guest_name=guest_name,
        )
        if not profile:
            return {"ok": True, "found": False, "profile": None}
        return {"ok": True, "found": True, "profile": profile}

    # v1.4.1: 全链路冒烟检测
    @router.get("/admin/smoke-test")
    async def admin_smoke_test(ctx=Depends(get_ctx)):
        """全链路配置与健康检查

        检查项：数据目录、凭证、企微 API、智能表格、微信客服、回调可达性。
        返回每个检查项的状态和下一步指引。
        """
        import os, httpx
        try:
            from .. import wecom_kf
        except Exception as exc:
            wecom_kf = None
            logger.warning("[admin/smoke-test] wecom_kf 导入失败: %s", exc)

        checks = []
        overall = True

        # 1. 数据目录可写
        try:
            test_file = data_layer.DATA_DIR / ".smoke_write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            checks.append({"name": "data_dir_writable", "ok": True, "message": "数据目录可写"})
        except Exception as exc:
            overall = False
            checks.append({"name": "data_dir_writable", "ok": False, "message": f"数据目录不可写: {exc}"})

        # 2. 敏感凭证文件权限
        try:
            secrets_path = Path(data_layer.DATA_DIR) / ".wecom_secrets"
            if secrets_path.exists():
                mode = secrets_path.stat().st_mode & 0o777
                ok = mode == 0o600
                checks.append({"name": "secrets_file_permission", "ok": ok, "message": f".wecom_secrets 权限 {oct(mode)}"})
                if not ok:
                    overall = False
            else:
                checks.append({"name": "secrets_file_permission", "ok": True, "message": "未使用 .wecom_secrets"})
        except Exception as exc:
            checks.append({"name": "secrets_file_permission", "ok": False, "message": str(exc)})
            overall = False

        # 3. 企微 API 链路探测
        try:
            probe = await wecom_sync.live_probe(force=True)
            if probe.get("api_ok"):
                checks.append({"name": "wecom_api", "ok": True, "message": "企微 API 链路全通"})
            elif probe.get("token_ok") and probe.get("errcode") == 60020:
                overall = False
                checks.append({"name": "wecom_api", "ok": False, "message": f"出口 IP {probe.get('client_ip')} 不在企微可信白名单", "client_ip": probe.get("client_ip")})
            else:
                overall = False
                checks.append({"name": "wecom_api", "ok": False, "message": f"企微 API 探测失败: {probe.get('errmsg')} (errcode={probe.get('errcode')})"})
        except Exception as exc:
            overall = False
            checks.append({"name": "wecom_api", "ok": False, "message": f"探测异常: {exc}"})

        # 4. 微信客服配置（直接读运行时配置，不依赖 wecom_kf 模块导入）
        try:
            kf_id = wecom_sync.get_kf_setting("WECOM_KF_ID") or os.environ.get("WECOM_KF_ID", "")
            kf_token = wecom_sync.get_kf_setting("WECOM_KF_TOKEN") or os.environ.get("WECOM_KF_TOKEN", "")
            kf_aes = wecom_sync.get_kf_setting("WECOM_KF_ENCODING_AES_KEY") or os.environ.get("WECOM_KF_ENCODING_AES_KEY", "")
            kf_ok = bool(kf_id and kf_token and kf_aes)
            missing = []
            if not kf_id:
                missing.append("WECOM_KF_ID")
            if not kf_token:
                missing.append("WECOM_KF_TOKEN")
            if not kf_aes:
                missing.append("WECOM_KF_ENCODING_AES_KEY")
            checks.append({"name": "kf_configured", "ok": kf_ok, "message": "微信客服已配置" if kf_ok else f"缺少: {', '.join(missing)}"})
            if not kf_ok:
                overall = False
        except Exception as exc:
            overall = False
            checks.append({"name": "kf_configured", "ok": False, "message": f"检查异常: {exc}"})

        # 5. 回调 URL 可达性
        try:
            base_url = wecom_sync.get_kf_setting("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")
            callback_url = f"{base_url}/api/domhotel-suite/kf/callback"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(callback_url, params={"msg_signature": "x", "timestamp": "1", "nonce": "x", "echostr": "x"})
            # 403 表示接口存在但签名不对，这是正常的
            checks.append({"name": "kf_callback_reachable", "ok": r.status_code in (200, 403), "message": f"回调 URL 返回 {r.status_code}"})
            if r.status_code not in (200, 403):
                overall = False
        except Exception as exc:
            overall = False
            checks.append({"name": "kf_callback_reachable", "ok": False, "message": f"回调 URL 不可达: {exc}"})

        # 6. AI 智能体配置（直接读运行时配置）
        try:
            agent_id = wecom_sync.get_kf_setting("KF_AI_AGENT_ID") or os.environ.get("KF_AI_AGENT_ID", "hotel-ai-guest-service")
            checks.append({"name": "ai_agent_configured", "ok": bool(agent_id), "message": f"AI 智能体: {agent_id}"})
        except Exception as exc:
            checks.append({"name": "ai_agent_configured", "ok": False, "message": f"检查异常: {exc}"})

        # 生成 next_steps
        next_steps = []
        for c in checks:
            if c["ok"]:
                continue
            if c["name"] == "wecom_api" and "可信白名单" in c.get("message", ""):
                next_steps.append(f"请在企微后台 → 应用管理 → 自建应用 → 企业可信 IP 添加: {c.get('client_ip', '')}")
            elif c["name"] == "kf_configured":
                next_steps.append("请配置 WECOM_KF_ID / WECOM_KF_TOKEN / WECOM_KF_ENCODING_AES_KEY")
            elif c["name"] == "kf_callback_reachable":
                next_steps.append("请检查回调 URL 域名解析和服务器网络，确保企微能访问")
            elif c["name"] == "data_dir_writable":
                next_steps.append("请检查数据目录挂载权限")
            elif c["name"] == "secrets_file_permission":
                next_steps.append("请执行 chmod 600 /app/working/dompaw-data-backup/.wecom_secrets")

        if not next_steps and overall:
            next_steps.append("全链路检测通过，系统可正常使用")

        return {
            "ok": overall,
            "checked_at": _meta.now() if hasattr(_meta, "now") else "",
            "checks": checks,
            "next_steps": next_steps,
        }

    # v1.4.1: 客服会话质检（必须在 /admin/{resource} 通配之前注册）
    # 直接读 JSONL 文件，不依赖 wecom_kf 模块导入（避免循环导入问题）

    @router.get("/admin/kf-sessions")
    async def admin_kf_sessions(ctx=Depends(get_ctx), limit: int = 100):
        """列出最近有会话的微信客服客人"""
        sessions = []
        log_path = Path(data_layer.DATA_DIR) / "kf_sessions.jsonl"
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8").strip().splitlines()
                seen = set()
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    uid = r.get("external_userid")
                    if uid and uid not in seen:
                        seen.add(uid)
                        sessions.append({
                            "external_userid": uid,
                            "last_ts": r.get("ts"),
                            "last_role": r.get("role"),
                            "last_content": r.get("content"),
                        })
                        if len(sessions) >= limit:
                            break
            except Exception as exc:
                logger.warning("[admin/kf-sessions] 读取失败: %s", exc)
        return {"ok": True, "sessions": sessions}

    @router.get("/admin/kf-sessions/{external_userid}")
    async def admin_kf_session_detail(
        ctx=Depends(get_ctx),
        external_userid: str = "",
        limit: int = 50,
    ):
        """查看单个客人的会话历史（含 AI/人工/接管事件）"""
        messages = []
        log_path = Path(data_layer.DATA_DIR) / "kf_sessions.jsonl"
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8").strip().splitlines()
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("external_userid") == external_userid:
                        messages.append(r)
                        if len(messages) >= limit:
                            break
                messages.reverse()
            except Exception as exc:
                logger.warning("[admin/kf-sessions] 读取失败: %s", exc)
        return {
            "ok": True,
            "external_userid": external_userid,
            "messages": messages,
        }

    @router.get("/admin/{resource}")
    async def admin_list(
        ctx=Depends(get_ctx),
        resource: str = "",
        include_deleted: str = "false",
    ):
        # v2.1.16-hotfix4: 把 auth/permission 路由让给 auth.py 里的具体路由
        if resource in ("auth", "permission"):
            raise HTTPException(status_code=404, detail=f"{resource} 不在 admin 列表(应走 auth.py 的具体路由)")
        if resource not in ADMIN_RESOURCES:
            raise HTTPException(status_code=400, detail=f"未知资源:{resource}")
        return _admin_load(resource, include_deleted=include_deleted.lower() == "true")

    @router.get("/admin/{resource}/{item_id}")
    async def admin_get(
        ctx=Depends(get_ctx),
        resource: str = "",
        item_id: str = "",
    ):
        # v2.1.16-hotfix4: 同 admin_list, 让位给 auth.py 的具体路由
        if resource in ("auth", "permission"):
            raise HTTPException(status_code=404, detail=f"{resource}/{item_id} 不在 admin get(应走 auth.py 的具体路由)")
        if resource not in ADMIN_RESOURCES:
            raise HTTPException(status_code=400, detail=f"未知资源:{resource}")
        item = _admin_find(resource, item_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"{resource} {item_id} 不存在")
        return item

    @router.post("/admin/{resource}")
    async def admin_create(
        ctx=Depends(get_ctx),
        resource: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ):
        if resource not in ADMIN_RESOURCES:
            raise HTTPException(status_code=400, detail=f"未知资源:{resource}")
        required = ADMIN_REQUIRED[resource]
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise HTTPException(status_code=400, detail=f"缺少字段:{missing}")
        rows = data_layer.load_table(resource)
        # 业务校验
        if resource == "departments":
            if any(r.get("name") == payload.get("name") and not r.get("deleted") for r in rows):
                raise HTTPException(status_code=409, detail=f"部门名 {payload.get('name')} 已存在")
        elif resource == "staff":
            # 必填: phone (后端兜底,前端已经拦过)
            phone = (payload.get("phone") or "").strip()
            if not phone:
                raise HTTPException(status_code=400, detail="手机号 必填")
            if not re.match(r"^1[3-9]\d{9}$", phone):
                raise HTTPException(status_code=400, detail="手机号格式不对 (11位,1开头)")
            # 校验部门存在
            dept_id = payload.get("department_id")
            if dept_id:
                depts = data_layer.load_table("departments")
                if not any(d.get("id") == dept_id and not d.get("deleted") for d in depts):
                    raise HTTPException(status_code=400, detail=f"部门 {dept_id} 不存在")
            # 校验员工名唯一
            staff_name = payload.get("name", "").strip()
            if staff_name and any(r.get("name") == staff_name and not r.get("deleted") for r in rows):
                raise HTTPException(status_code=409, detail=f"员工名 {staff_name} 已存在")
            # 校验手机号唯一
            if any(r.get("phone") == phone and not r.get("deleted") for r in rows):
                raise HTTPException(status_code=409, detail=f"手机号 {phone} 已被登记")
            # 默认 on_duty=True
            if "on_duty" not in payload:
                payload["on_duty"] = True
            # Phase 5.1: 企微对齐字段默认值
            for k, default in STAFF_WECOM_FIELDS.items():
                if k not in payload:
                    payload[k] = default
        elif resource == "room_types":
            if any(r.get("name") == payload.get("name") and not r.get("deleted") for r in rows):
                raise HTTPException(status_code=409, detail=f"房型名 {payload.get('name')} 已存在")
        elif resource == "floors":
            if any(r.get("name") == payload.get("name") and not r.get("deleted") for r in rows):
                raise HTTPException(status_code=409, detail=f"楼层名 {payload.get('name')} 已存在")
        prefix_map = {
            "departments": "dept",
            "staff": "staff",
            "room_types": "rt",
            "floors": "floor",
        }
        item = {
            "id": new_id(prefix_map[resource]),
            **{k: v for k, v in payload.items() if k != "id"},
            "created_at": now(),
            "deleted": False,
        }
        # v2.1.18: 标记来源 (manual 因为是 admin UI 创建)
        _meta.apply_meta_on_create(item, source="manual", operator=sess.get("user_id", ""))
        rows.append(item)
        data_layer.save_table(resource, rows)
        # 推企微 (Phase 5: 新增/更新都用 add, 企微同 record_id 会覆盖)
        try:
            from ..wecom_sync import sync_now
            sync_now(resource, "add", item)
        except Exception as exc:
            logger.warning("admin_create safe_sync failed: %s", exc)
        return {"ok": True, "item": item}

    @router.put("/admin/{resource}/{item_id}")
    async def admin_update(
        ctx=Depends(get_ctx),
        resource: str = "",
        item_id: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ):
        if resource not in ADMIN_RESOURCES:
            raise HTTPException(status_code=400, detail=f"未知资源:{resource}")
        rows = data_layer.load_table(resource)
        for r in rows:
            if r.get("id") == item_id:
                for k, v in payload.items():
                    if k != "id":
                        r[k] = v
                r["updated_at"] = now()
                # v2.1.18: 标记来源 + 版本号
                _meta.apply_meta_on_update(r, source="manual", operator=sess.get("user_id", ""))
                data_layer.save_table(resource, rows)
                # 推企微 (按规范: 更新用 add, 企微同 record_id 覆盖)
                try:
                    from ..wecom_sync import sync_now
                    sync_now(resource, "add", r)
                except Exception as exc:
                    logger.warning("admin_update safe_sync failed: %s", exc)
                return {"ok": True, "item": r}
        raise HTTPException(status_code=404, detail=f"{resource} {item_id} 不存在")

    @router.delete("/admin/{resource}/{item_id}")
    async def admin_delete(
        ctx=Depends(get_ctx),
        resource: str = "",
        item_id: str = "",
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ):
        if resource not in ADMIN_RESOURCES:
            raise HTTPException(status_code=400, detail=f"未知资源:{resource}")
        rows = data_layer.load_table(resource)
        for r in rows:
            if r.get("id") == item_id:
                r["deleted"] = True
                r["deleted_at"] = now()
                data_layer.save_table(resource, rows)
                # 按规范: 软删除不推企微 (保留历史, 企微端不动)
                # 如未来需推删除, 改这里为 sync_now(resource, "delete", r)
                logger.info("admin_delete: soft delete %s/%s (not pushed to wecom)", resource, item_id)
                return {"ok": True, "item_id": item_id}
        raise HTTPException(status_code=404, detail=f"{resource} {item_id} 不存在")

    @router.post("/admin/rooms/bulk_upsert")
    async def bulk_upsert_rooms(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ):
        """批量新增/更新房间(不删除任何已有记录,保护工单历史)

        payload 两种格式都支持:
          1) {"rooms": [{"room_no":"101","floor":1,"room_type":"单人间","status":"空房"}, ...]}
          2) {"csv": "room_no,floor,room_type,status\\n101,1,单人间,空房\\n..."}  ← 直接贴 Excel 复制

        payload 可选参数:
          - force_vacant: bool = True  (默认 True, v2.3.0 新增)
            True  → 导入时**忽略** payload 里的 status 字段, 强制把所有房设为"空房"
                    (避免历史房态图导入时覆盖现有 在住/脏房/维修 状态)
            False → 保留 payload 里的 status (与 v2.2.0 行为一致)
          - building: str = ""  (可选, 显式指定楼栋名; 不填则从 room_no 推导)
          - building_block: str = ""  (可选, 显式指定 BLOCK)

        行为:
          - room_no 已存在(且未删除) → 更新 floor/room_type; **status 由 force_vacant 决定**; 其他字段不动
          - room_no 已存在但已软删   → 复活(deleted=false, 保留历史 created_at)
          - room_no 不存在            → 新建; status 默认 "空房"
          - 不删除任何已存在的房间    ← 关键!保护工单历史关联

        返回:
          {"ok": true, "created": N, "updated": M, "resurrected": K, "skipped": [...]}
        """
        rooms_in: List[Dict[str, Any]] = []

        # 格式 1: JSON 数组
        if isinstance(payload.get("rooms"), list):
            rooms_in = payload["rooms"]
        # 格式 2: CSV 字符串
        elif isinstance(payload.get("csv"), str) and payload["csv"].strip():
            try:
                reader = csv.DictReader(io.StringIO(payload["csv"]))
                rooms_in = [dict(row) for row in reader]
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"CSV 解析失败:{exc}")
        else:
            raise HTTPException(
                status_code=400,
                detail="payload 必须含 'rooms' (list) 或 'csv' (str) 之一",
            )

        if not rooms_in:
            raise HTTPException(status_code=400, detail="rooms 列表为空")

        # v2.3.0: 读取 force_vacant 参数 (默认 True - 导入时强制空房, 不覆盖现有业务状态)
        force_vacant = bool(payload.get("force_vacant", True))
        explicit_building = str(payload.get("building", "")).strip()
        explicit_block = str(payload.get("building_block", "")).strip()

        # 合法房型 / 状态
        valid_statuses = {"空房", "在住", "脏房", "待打扫", "维修中", "已退未查"}
        valid_room_types: set = set()
        try:
            for rt in data_layer.load_table("room_types"):
                if not rt.get("deleted"):
                    valid_room_types.add(rt.get("name"))
        except Exception:
            pass

        # 读取现有 rooms(包含已软删的,用于复活判断)
        existing = data_layer.load_table("rooms")
        by_room_no: Dict[str, Dict[str, Any]] = {}
        for r in existing:
            by_room_no[r.get("room_no", "")] = r

        created: List[str] = []
        updated: List[str] = []
        resurrected: List[str] = []
        skipped: List[Dict[str, Any]] = []

        for item in rooms_in:
            room_no = str(item.get("room_no", "")).strip()
            if not room_no:
                skipped.append({"row": item, "reason": "缺少 room_no"})
                continue

            # 字段清洗
            try:
                floor_raw = item.get("floor", 1)
                floor = int(str(floor_raw).strip())
            except Exception:
                skipped.append({"row": item, "reason": f"floor 不是整数 ({floor_raw})"})
                continue
            room_type = str(item.get("room_type", "")).strip()
            if not room_type:
                skipped.append({"row": item, "reason": "缺少 room_type"})
                continue
            # v2.3.0: 如果 force_vacant=True, 强制使用"空房"(忽略 payload 里的 status, 也不做合法性检查)
            if force_vacant:
                status = "空房"
            else:
                status = str(item.get("status", "空房")).strip() or "空房"
                if status not in valid_statuses:
                    skipped.append({"row": item, "reason": f"status 不合法 ({status}),合法值:{sorted(valid_statuses)}"})
                    continue

            # v2.3.0: 推导或使用显式 building
            if explicit_building:
                building = explicit_building
                building_block = explicit_block or f"BLOCK {building[0]}"
            else:
                # 复用 _ensure_building 逻辑 (从 room_no 推导)
                _probe: Dict[str, Any] = {"room_no": room_no}
                _ensure_building_local(_probe)
                building = _probe.get("building", "")
                building_block = _probe.get("building_block", "")

            if room_no in by_room_no:
                old = by_room_no[room_no]
                if old.get("deleted"):
                    # 复活
                    old["deleted"] = False
                    old["deleted_at"] = None
                    old["floor"] = floor
                    old["room_type"] = room_type
                    old["building"] = building
                    old["building_block"] = building_block
                    if force_vacant:
                        # 强制空房 + 清空入住信息
                        old["status"] = "空房"
                        old["guest_name"] = ""
                        old["guest_phone"] = ""
                        old["check_in_at"] = ""
                        old["expected_checkout"] = ""
                    else:
                        old["status"] = status
                    old["updated_at"] = now()
                    resurrected.append(room_no)
                else:
                    # 更新(不动 created_at, 不动 guest/checkin 信息除非 force_vacant)
                    old["floor"] = floor
                    old["room_type"] = room_type
                    old["building"] = building
                    old["building_block"] = building_block
                    if force_vacant:
                        # ★ 关键: 强制空房 + 清空入住信息
                        old["status"] = "空房"
                        old["guest_name"] = ""
                        old["guest_phone"] = ""
                        old["check_in_at"] = ""
                        old["expected_checkout"] = ""
                    else:
                        old["status"] = status
                    old["updated_at"] = now()
                    updated.append(room_no)
            else:
                new_rec = {
                    "room_no": room_no,
                    "floor": floor,
                    "room_type": room_type,
                    "building": building,
                    "building_block": building_block,
                    "status": status,
                    "guest_name": "",
                    "guest_phone": "",
                    "check_in_at": "",
                    "expected_checkout": "",
                    "notes": "",
                    "created_at": now(),
                    "updated_at": now(),
                    "deleted": False,
                }
                existing.append(new_rec)
                by_room_no[room_no] = new_rec
                created.append(room_no)

        # 写盘
        data_layer.save_table("rooms", existing)

        # 推企微(只推 created/updated,resurrected 也按新增推)
        try:
            from ..wecom_sync import sync_now
            for rn in created + resurrected:
                sync_now("rooms", "add", by_room_no[rn])
            for rn in updated:
                sync_now("rooms", "add", by_room_no[rn])
        except Exception as exc:
            logger.warning("bulk_upsert_rooms wecom sync failed: %s", exc)

        logger.info(
            "bulk_upsert_rooms: created=%d updated=%d resurrected=%d skipped=%d",
            len(created), len(updated), len(resurrected), len(skipped),
        )
        return {
            "ok": True,
            "created": created,
            "updated": updated,
            "resurrected": resurrected,
            "skipped": skipped,
            "summary": {
                "created": len(created),
                "updated": len(updated),
                "resurrected": len(resurrected),
                "skipped": len(skipped),
                "total_in": len(rooms_in),
                "total_rooms_now": len([r for r in existing if not r.get("deleted")]),
            },
        }

    @router.post("/admin/staff/sync_from_wecom")
    async def sync_staff_from_wecom(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
    ):
        """Phase 5.1 占位：从企业微信通讯录拉成员,自动建/对齐 staff 记录。

        启用前置条件（用户手动配置）:
          1) 企业微信后台 → 我的企业 → 权限管理 → 给应用开"通讯录管理"
          2) 我的企业 → 安全与保密 → 设置可信任 IP（plugin 服务器公网 IP）
          3) plugin 启动环境变量:
               WECOM_CONTACTS_SECRET=<通讯录同步 secret,跟 WECOM_AGENT_SECRET 不同>
               WECOM_TRUSTED_IP=<已配白名单的 IP>
          4) 重启 plugin 后,前端调此接口触发拉取

        当前返回 501 + 完整提示,不影响现有功能。
        """
        import os

        contacts_secret = os.environ.get("WECOM_CONTACTS_SECRET", "")
        trusted_ip = os.environ.get("WECOM_TRUSTED_IP", "")
        missing = []
        if not contacts_secret:
            missing.append("WECOM_CONTACTS_SECRET")
        if not trusted_ip:
            missing.append("WECOM_TRUSTED_IP")

        return {
            "ok": False,
            "status": "not_implemented",
            "missing_env": missing,
            "hint": (
                "需要先在企业微信后台："
                "1) 我的企业 → 权限管理 → 给应用开『通讯录管理』；"
                "2) 我的企业 → 安全与保密 → 设置可信任 IP；"
                "3) 在 plugin 启动环境变量里设 WECOM_CONTACTS_SECRET=<secret> "
                "和 WECOM_TRUSTED_IP=<ip>；"
                "4) 重启 plugin 后再调此接口。"
                "当前 staff 表已预留 wecom_userid / wecom_sync_status / "
                "wecom_last_synced_at 字段,可手工对齐。"
            ),
        }

    # ═══════════════════════════════════════════════════════════
    # Phase 5.2 占位: work_orders → 企微消息通知 (4 个端点)
    # ═══════════════════════════════════════════════════════════
    # 现状: 数据已通过 wecom_sync.safe_sync() 推到企微智能表格(给老板看),
    #       但派单后员工没收到企微通知(在走廊/客房里看不到),
    #       待补完后工单变更能实时推到师傅手机.
    #
    # 启用前置条件(用户手动配置):
    #   1) 企业微信后台 → 应用管理 → 自建应用 → 开通"发送应用消息"能力
    #   2) plugin 启动环境变量:
    #        WECOM_AGENT_ID=<应用 ID, 跟现有 WECOM_AGENT_SECRET 配套>
    #   3) staff 表里 wecom_userid 字段必须已填(可走 /admin/staff/sync_from_wecom 对齐)
    #   4) 重启 plugin 后生效

    @router.post("/wecom/notify/work_order/{wo_id}")
    async def notify_work_order_to_wecom(
        ctx=Depends(get_ctx),
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
    ):
        """手动触发：给工单执行人发送企微模板卡片通知

        v1.4.1: 已实现。自动派单/新建时由 work_orders._notify_staff_wo_change 调用，
        此处供管理后台手动补发。
        """
        import os, httpx, json
        from pathlib import Path

        # 读取运行时配置
        cfg_path = Path("/app/working/dompaw-data-backup") / "wecom_runtime_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        corp_id = cfg.get("WECOM_CORP_ID") or os.environ.get("WECOM_CORP_ID", "")
        agent_secret = os.environ.get("WECOM_AGENT_SECRET", "")
        agent_id = cfg.get("WECOM_AGENT_ID") or os.environ.get("WECOM_AGENT_ID", "")
        base_url = cfg.get("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")
        if not base_url:
            base_url = "https://guishan.paw.domai.fun"

        if not all([corp_id, agent_secret, agent_id]):
            raise HTTPException(400, detail="企微凭证未配置完整")

        # 查工单
        wo = None
        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                wo = w
                break
        if not wo:
            raise HTTPException(404, detail=f"工单 {wo_id} 不存在")

        # 解析通知对象
        target = wo.get("assignee_id") or wo.get("assignee") or ""
        userids = []
        if target:
            staffs = data_layer.load_table("staff")
            for s in staffs:
                if s.get("id") == target or s.get("name") == target:
                    if s.get("wecom_userid"):
                        userids.append(s["wecom_userid"])
                    break
        if not userids:
            notify = cfg.get("KF_NOTIFY_USERIDS") or os.environ.get("KF_NOTIFY_USERIDS", "")
            userids = [u.strip() for u in notify.replace("，", ",").split(",") if u.strip()]
        if not userids:
            raise HTTPException(400, detail="无通知对象（执行人无 wecom_userid 且未配 KF_NOTIFY_USERIDS）")

        # 获取 access_token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": corp_id, "corpsecret": agent_secret},
            )
            token_data = r.json()
        if token_data.get("errcode") != 0:
            raise HTTPException(500, detail=f"获取 access_token 失败: {token_data}")
        token = token_data["access_token"]

        event = payload.get("event", "assigned")
        action_map = {
            "assigned": "工单已派单",
            "status_changed": "工单状态变更",
            "completed": "工单已完成",
            "urgent": "紧急工单",
        }
        action = action_map.get(event, "工单通知")

        room_no = wo.get("room_no", "—")
        work_type = wo.get("work_type", "—")
        priority = wo.get("priority", "normal")
        priority_label = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}.get(priority, priority)
        description = wo.get("description", "") or "暂无描述"
        assignee = wo.get("assignee") or wo.get("assignee_id") or "未分配"

        url = f"{base_url}/api/domhotel-suite/ui/staff-h5.html?wo_id={wo_id}"
        body = {
            "touser": "|".join(userids),
            "msgtype": "template_card",
            "agentid": int(agent_id) if str(agent_id).isdigit() else agent_id,
            "template_card": {
                "card_type": "text_notice",
                "source": {"desc": "桂山大酒店", "desc_color": 0},
                "main_title": {
                    "title": f"{action} — {room_no} · {work_type}",
                    "desc": f"优先级：{priority_label}｜当前处理人：{assignee}",
                },
                "quote_area": {"type": 0, "text": description[:120]},
                "jump_list": [
                    {"type": 1, "url": url, "title": "查看工单详情"},
                ],
                "card_action": {
                    "type": 1,
                    "url": url,
                },
            },
        }

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                json=body,
            )
            send_data = r.json()
        if send_data.get("errcode") != 0:
            raise HTTPException(500, detail=f"发送模板卡片失败: {send_data}")

        return {
            "ok": True,
            "wo_id": wo_id,
            "event": event,
            "notified_userids": userids,
            "msgid": send_data.get("msgid"),
        }

    @router.get("/wecom/staff/{staff_id}/wecom_userid")
    async def get_staff_wecom_userid(
        ctx=Depends(get_ctx),
        staff_id: str = "",
    ):
        """查员工的企微 userid(发消息要用).

        返回 staff 表的 wecom_userid / wecom_sync_status / wecom_last_synced_at 字段,
        当前 phase 5.2 没真同步, 这三个字段都是空, 用于排查为啥通知发不出去.
        """
        try:
            staffs = data_layer.load_table("staff")
            for s in staffs:
                if s.get("id") == staff_id:
                    return {
                        "ok": True,
                        "staff_id": staff_id,
                        "name": s.get("name"),
                        "wecom_userid": s.get("wecom_userid", ""),
                        "wecom_sync_status": s.get("wecom_sync_status", ""),
                        "wecom_last_synced_at": s.get("wecom_last_synced_at", ""),
                    }
            raise HTTPException(status_code=404, detail=f"员工 {staff_id} 不存在")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/wecom/notify/work_order/{wo_id}/test")
    async def test_notify_work_order(
        ctx=Depends(get_ctx),
        wo_id: str = "",
    ):
        """手动测试用: 不管前置条件, 直接返回"将要发什么"的预览.

        给前端调试用: 点一下能看到企微消息预览(标题/内容/按钮),
        不依赖 WECOM_AGENT_ID 是否配置.
        """
        wo = None
        try:
            wos = data_layer.load_table("work_orders")
            for w in wos:
                if w.get("wo_id") == wo_id:
                    wo = w
                    break
        except Exception:
            pass
        if not wo:
            raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

        return {
            "ok": True,
            "preview_mode": True,
            "wo_id": wo_id,
            "message_preview": {
                "msgtype": "template_card",
                "touser": f"<{wo.get('assignee') or wo.get('assignee_id')} 的 wecom_userid>",
                "agentid": "${WECOM_AGENT_ID}",
                "template_card": {
                    "card_type": "news_notice",
                    "main_title": {
                        "title": f"🔧 您有新工单 {wo.get('wo_id')}",
                        "desc": f"房间 {wo.get('room_no')} · {wo.get('work_type')} · 优先级 {wo.get('priority')}",
                    },
                    "emphasis_content": {
                        "title": wo.get("work_type"),
                        "desc": wo.get("room_no"),
                    },
                    "quote_area": {
                        "type": 1,
                        "text": wo.get("description", ""),
                    },
                    "action_menu": {
                        "desc": "请尽快处理",
                        "action_list": [
                            {"text": "✅ 接单", "key": f"WO_ACCEPT:{wo.get('wo_id')}"},
                            {"text": "📞 联系前台", "key": f"WO_CALL:{wo.get('wo_id')}"},
                        ],
                    },
                },
            },
            "hint": "这是预览模式, 实际不会发出去. 配置好 WECOM_AGENT_ID + staff.wecom_userid 后, 调 /wecom/notify/work_order/{id} 才会真发.",
        }

    @router.post("/wecom/notify/callback")
    async def wecom_notify_callback(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
    ):
        """接收企微应用消息的回调(模板卡片"接单/完成"按钮被点时).

        v1.4.1: 已实现 EventKey 解析 + 工单状态更新。
        注意: 当前模板卡片使用 card_action URL 跳转, 不会触发此回调;
        如需企微内一键按钮, 需改用 button_interaction 模板卡片,
        并在企微后台 → 应用 → 接收消息 设置回调 URL 指向本端点。

        企微回调 payload 格式(简化):
          {
            "Event": "template_card_event",
            "EventKey": "WO_ACCEPT:WO-20260815-xxxxxx",
            "FromUserName": "USERID"
          }
        """
        event = payload.get("Event", "")
        if event != "template_card_event":
            return {"ok": True, "ignored": True, "reason": "非模板卡片事件"}

        event_key = payload.get("EventKey", "")
        wecom_userid = payload.get("FromUserName", "")
        if not event_key or not wecom_userid:
            return {"ok": False, "reason": "缺少 EventKey 或 FromUserName"}

        # 解析动作和工单号
        if ":" not in event_key:
            return {"ok": False, "reason": "EventKey 格式错误"}
        action, wo_id = event_key.split(":", 1)

        # 通过 wecom_userid 找员工
        staff = None
        try:
            staffs = data_layer.load_table("staff")
            for s in staffs:
                if s.get("wecom_userid") == wecom_userid:
                    staff = s
                    break
        except Exception as exc:
            return {"ok": False, "reason": f"读取员工表失败: {exc}"}

        if not staff:
            return {"ok": False, "reason": f"未找到 wecom_userid={wecom_userid} 的员工"}

        staff_id = staff.get("id", "")
        staff_name = staff.get("name", "")

        # 更新工单
        try:
            wos = data_layer.load_table("work_orders")
            wo = None
            for w in wos:
                if w.get("wo_id") == wo_id:
                    wo = w
                    break
            if not wo:
                return {"ok": False, "reason": f"工单 {wo_id} 不存在"}

            if action == "WO_ACCEPT":
                if wo.get("status") != "pending":
                    return {"ok": False, "reason": "工单不是待接单状态"}
                wo["assignee_id"] = staff_id
                wo["assignee"] = staff_name
                wo["status"] = "processing"
                wo["updated_at"] = _meta.now() if hasattr(_meta, "now") else __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not wo.get("timeline"):
                    wo["timeline"] = {}
                wo["timeline"]["accepted_at"] = wo["updated_at"]
                wo["timeline"]["accepted_by"] = staff_id
                msg = f"✅ 已接单\n处理人：{staff_name}\n工单号：{wo_id}"

            elif action == "WO_DONE":
                if wo.get("status") != "processing":
                    return {"ok": False, "reason": "工单不是处理中状态"}
                wo["status"] = "done"
                wo["updated_at"] = _meta.now() if hasattr(_meta, "now") else __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                wo["completed_at"] = wo["updated_at"]
                if not wo.get("timeline"):
                    wo["timeline"] = {}
                wo["timeline"]["completed_at"] = wo["updated_at"]
                wo["timeline"]["completed_by"] = staff_id
                msg = f"✅ 工单已完成\n完成人：{staff_name}\n工单号：{wo_id}"

            else:
                return {"ok": False, "reason": f"未知动作: {action}"}

            data_layer.save_table("work_orders", wos)
        except Exception as exc:
            return {"ok": False, "reason": f"更新工单失败: {exc}"}

        # 企微事件回调建议返回 "success" 字符串, 但这里返回 JSON 也常被接受
        return {"ok": True, "action": action, "wo_id": wo_id, "staff": staff_name, "message": msg}

    app.include_router(router)
    logger.info("[routes/admin] 已注册 5 个后台配置路由 + 1 个 sync 路由 + 1 个 contacts 占位 + 4 个 work_order 企微通知端点 (v1.4.1 模板卡片已实装) + 知识库管理")
