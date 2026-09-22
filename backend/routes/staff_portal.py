# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 员工端 API (staff_portal)

给客房阿姨、工程师傅等一线工作人员使用，通常通过企业微信应用或移动端访问。

路由清单:
  GET  /my/info                  — 我的基本信息
  GET  /my/tasks                 — 我的待办任务列表（按 assignee_id 过滤）
  GET  /my/tasks/{wo_id}         — 单个任务详情
  POST /my/tasks/{wo_id}/accept  — 接受任务（assigned → in_progress）
  POST /my/tasks/{wo_id}/done    — 完成任务（in_progress → done）
  POST /my/tasks/{wo_id}/pause   — 暂停任务（in_progress → assigned，退回队列）
  POST /my/supply-request        — 申领耗材（创建补货工单）
  GET  /my/stats                 — 我的工作统计（今日/本周/本月）
  GET  /my/rooms                 — 我负责的房间列表（清洁阿姨看脏房/待打扫）

鉴权:
  - 员工 cookie 登录（hotel_uid）
  - AI 助手 X-Agent-Id 头
  - 企微 userid 头（X-Wecom-Userid，用于企微应用免登）
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import RedirectResponse

from .. import data_layer
from .. import auth
from .. import wecom_kf
from ..wecom_sync import safe_sync
from ._helpers import now, create_work_order, new_id
from .work_order_svc import (
    svc_create_work_order, svc_complete_work_order,
    svc_accept_work_order, svc_pause_work_order,
)


WECOM_BASE_URL = "https://qyapi.weixin.qq.com"


def _get_work_orders_module():
    """获取 work_orders 模块（绕过相对导入在插件环境下的兼容问题）"""
    import sys
    for k, v in sys.modules.items():
        if k.endswith("routes.work_orders") and hasattr(v, "_notify_staff_wo_change"):
            return v
    return None


def _wecom_runtime_cfg() -> Dict[str, Any]:
    """读取企微运行时配置（直接读文件，避免相对导入问题）"""
    candidates = [
        Path("/app/working/dompaw-data-backup") / "wecom_runtime_config.json",
        Path.cwd() / "data" / "v20" / "wecom_runtime_config.json",
    ]
    for p in candidates:
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _get_corp_id() -> str:
    return _wecom_runtime_cfg().get("WECOM_CORP_ID") or os.environ.get("WECOM_CORP_ID", "")


def _get_agent_secret() -> str:
    return _wecom_runtime_cfg().get("WECOM_AGENT_SECRET") or os.environ.get("WECOM_AGENT_SECRET", "")


def _get_callback_base_url() -> str:
    return (_wecom_runtime_cfg().get("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")).rstrip("/")


async def _get_access_token() -> str:
    corp_id = _get_corp_id()
    secret = _get_agent_secret()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{WECOM_BASE_URL}/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}",
            timeout=15,
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    return data["access_token"]

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────
# 鉴权: 支持 3 种身份识别方式
# ─────────────────────────────────────────────

def _get_staff_session(request: Request) -> Dict[str, Any]:
    """员工端鉴权: cookie / X-Agent-Id / X-Wecom-Userid

    返回统一的 session dict，包含 user_id, name, role, staff 等
    """
    # 1. 先试 X-Agent-Id（AI 助手）
    agent_id = (request.headers.get("x-agent-id") or "").strip()
    if agent_id:
        try:
            return auth.get_session(request)
        except Exception:
            pass

    # 2. 试 X-Wecom-Userid（企微免登）
    wecom_uid = (request.headers.get("x-wecom-userid") or "").strip()
    if wecom_uid:
        staffs = data_layer.load_table("staff")
        for s in staffs:
            if s.get("deleted"):
                continue
            if s.get("wecom_userid") == wecom_uid:
                role = s.get("role", "employee")
                return {
                    "user_id": s.get("id", ""),
                    "name": s.get("name", ""),
                    "role": role,
                    "staff": s,
                    "is_super_admin": role == "super_admin",
                    "is_manager": role in ("super_admin", "manager"),
                    "is_employee": True,
                    "is_guest": False,
                    "_source": "wecom",
                }
        # 企微 userid 找不到对应员工 → 降级为 guest
        return {
            "user_id": f"wecom:{wecom_uid}",
            "name": "",
            "role": "guest",
            "staff": None,
            "is_super_admin": False,
            "is_manager": False,
            "is_employee": False,
            "is_guest": True,
            "_source": "wecom_unknown",
        }

    # 3. 最后走标准 cookie 登录
    sess = auth.get_session(request)
    if sess.get("is_guest") and not sess.get("user_id"):
        raise HTTPException(status_code=401, detail="未登录，请先登录或通过企业微信访问")
    return sess


def register_routes(app) -> None:
    """注册员工端路由"""

    # ─────────────────────────────────────────
    # GET /h5/staff/oauth — 企微 OAuth 免登入口
    # ─────────────────────────────────────────

    @router.get("/h5/staff/oauth")
    async def staff_oauth(request: Request, code: str = "", state: str = ""):
        """企微网页授权免登

        调用链:
          员工在企微里点击应用 → 企微带 code 访问本端点
          → 用 code 换企微 userid
          → 在 t_staff 按 wecom_userid 找员工
          → 种 hotel_uid cookie
          → 302 重定向到员工 H5
        """
        h5_url = "/api/domhotel-suite/ui/staff-h5.html"
        logger.warning("[staff_oauth] 收到回调: code=%s state=%s ua=%s", bool(code), state, request.headers.get("user-agent", "")[:80])
        if not code:
            logger.warning("[staff_oauth] 没有 code，直接回 H5")
            return RedirectResponse(url=h5_url)

        try:
            token = await _get_access_token()
            logger.warning("[staff_oauth] token 获取成功")
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{WECOM_BASE_URL}/cgi-bin/user/getuserinfo",
                    params={"access_token": token, "code": code},
                )
                data = r.json()

            logger.warning("[staff_oauth] getuserinfo 返回: %s", data)
            if data.get("errcode", -1) != 0:
                logger.warning("[staff_oauth] getuserinfo 失败: %s", data)
                return RedirectResponse(url=f"{h5_url}?error=oauth_failed&errcode={data.get('errcode')}&errmsg={quote(str(data.get('errmsg','')))}")

            # 企微 API 返回 UserId (大写), 兼容两种写法
            wecom_userid = (data.get("UserId") or data.get("userid") or "").strip()
            if not wecom_userid:
                logger.warning("[staff_oauth] 返回没有 userid, 完整响应: %s", data)
                return RedirectResponse(url=f"{h5_url}?error=no_userid")

            # 按企微 userid 找员工
            staff = None
            for s in data_layer.load_table("staff"):
                if not s or s.get("deleted"):
                    continue
                if s.get("wecom_userid") == wecom_userid:
                    staff = s
                    break

            if not staff:
                logger.warning("[staff_oauth] 未找到 wecom_userid=%s 的员工", wecom_userid)
                return RedirectResponse(url=f"{h5_url}?error=staff_not_found&wecom_userid={quote(wecom_userid)}")

            # 种 cookie 并重定向
            response = RedirectResponse(url=h5_url)
            auth.set_session(response, staff["id"], staff.get("role", "employee"))
            logger.warning("[staff_oauth] %s (%s) 企微免登成功", staff.get("name"), wecom_userid)
            return response

        except Exception as exc:
            logger.warning("[staff_oauth] OAuth 免登异常: %s", exc, exc_info=True)
            return RedirectResponse(url=f"{h5_url}?error=exception&msg={quote(str(exc))}")

    @router.get("/h5/staff/oauth-init")
    async def staff_oauth_init(request: Request):
        """触发企微 OAuth 授权页

        把本端点设为企微应用主页/菜单 URL，员工点击后自动跳转到企微授权页，
        授权后再带 code 回到 /h5/staff/oauth 完成免登。
        """
        corp_id = _get_corp_id()
        # 优先使用配置的公网回调域名，避免反代下 request.url 拿到内网地址
        base_url = _get_callback_base_url()
        if not base_url:
            base_url = f"{request.url.scheme}://{request.url.netloc}"
        redirect_uri = f"{base_url}/api/domhotel-suite/h5/staff/oauth"

        oauth_url = (
            f"https://open.weixin.qq.com/connect/oauth2/authorize"
            f"?appid={corp_id}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code"
            f"&scope=snsapi_base"
            f"&state=domhotel#wechat_redirect"
        )
        logger.warning("[staff_oauth_init] corp_id=%s redirect_uri=%s oauth_url=%s", corp_id, redirect_uri, oauth_url)
        return RedirectResponse(url=oauth_url)

    # ─────────────────────────────────────────
    # GET /my/info — 我的基本信息
    # ─────────────────────────────────────────

    @router.get("/my/info")
    async def my_info(request: Request) -> Dict[str, Any]:
        sess = _get_staff_session(request)
        staff = sess.get("staff")
        if not staff:
            return {"ok": True, "user_id": sess["user_id"], "name": sess["name"], "role": sess["role"]}
        # 查部门名
        dept_name = ""
        if staff.get("department_id"):
            try:
                depts = data_layer.load_table("departments")
                for d in depts:
                    if d.get("dept_id") == staff["department_id"]:
                        dept_name = d.get("name", "")
                        break
            except Exception:
                pass
        return {
            "ok": True,
            "user_id": staff.get("id", ""),
            "name": staff.get("name", ""),
            "phone": staff.get("phone", ""),
            "role": staff.get("role", "employee"),
            "department": dept_name,
            "department_id": staff.get("department_id", ""),
            "on_duty": staff.get("on_duty", True),
        }

    # ─────────────────────────────────────────
    # GET /my/tasks — 我的待办任务
    # ─────────────────────────────────────────

    @router.get("/my/tasks")
    async def my_tasks(
        request: Request,
        status: str = "",
        work_type: str = "",
    ) -> Dict[str, Any]:
        """返回分配给当前员工的工单列表

        Query params:
          status: 过滤状态（assigned/in_progress/done）
          work_type: 过滤类型（清洁/维修/补充消耗品）
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        wos = data_layer.load_table("work_orders")
        my_wos = []
        for w in wos:
            # 匹配 assignee_id 或 assignee 名字
            assignee_id = w.get("assignee_id", "")
            assignee = w.get("assignee", "")
            if assignee_id != user_id and assignee != user_name:
                continue
            # 过滤状态
            if status and w.get("status") != status:
                continue
            # 过滤类型
            if work_type and w.get("work_type") != work_type:
                continue
            my_wos.append(w)

        # 排序: 进行中 > 已派单 > 其他，同状态按优先级
        status_order = {"in_progress": 0, "assigned": 1, "pending": 2, "done": 3, "rejected": 4}
        priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        my_wos.sort(key=lambda w: (
            status_order.get(w.get("status", ""), 9),
            priority_order.get(w.get("priority", "normal"), 2),
        ))

        return {
            "ok": True,
            "count": len(my_wos),
            "tasks": my_wos,
        }

    # ─────────────────────────────────────────
    # GET /my/tasks/{wo_id} — 单个任务详情
    # ─────────────────────────────────────────

    @router.get("/my/tasks/{wo_id}")
    async def my_task_detail(
        request: Request,
        wo_id: str = "",
    ) -> Dict[str, Any]:
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                # 安全检查: 只能看自己的工单（manager 例外）
                assignee_id = w.get("assignee_id", "")
                assignee = w.get("assignee", "")
                is_mine = assignee_id == user_id or assignee == user_name
                if not is_mine and not sess.get("is_manager"):
                    raise HTTPException(status_code=403, detail="无权查看他人的工单")
                # 补充房间信息
                room_info = {}
                if w.get("room_no"):
                    try:
                        rooms = data_layer.load_table("rooms")
                        for r in rooms:
                            if r.get("room_no") == w["room_no"]:
                                room_info = {
                                    "room_no": r["room_no"],
                                    "floor": r.get("floor", 0),
                                    "room_type": r.get("room_type", ""),
                                    "status": r.get("status", ""),
                                    "guest_name": r.get("guest_name", ""),
                                }
                                break
                    except Exception:
                        pass
                return {"ok": True, "task": w, "room": room_info}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /my/tasks/{wo_id}/ai-reply — AI 副驾建议回复
    # ─────────────────────────────────────────

    @router.post("/my/tasks/{wo_id}/ai-reply")
    async def ai_reply_for_task(
        request: Request,
        wo_id: str = "",
    ) -> Dict[str, Any]:
        """AI 副驾：根据工单上下文生成建议回复客人的话术

        员工可一键复制或编辑后发送给客人（通过微信客服渠道）。
        """
        sess = _get_staff_session(request)
        user_name = sess.get("name", "")

        # 找工单
        wos = data_layer.load_table("work_orders")
        wo = None
        for w in wos:
            if w.get("wo_id") == wo_id:
                wo = w
                break
        if not wo:
            raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

        # 构造 prompt
        room_no = wo.get("room_no", "")
        work_type = wo.get("work_type", "")
        description = wo.get("description", "")
        status = wo.get("status", "")
        assignee = wo.get("assignee", "")

        # 查客人档案
        profile_context = ""
        try:
            from .. import guest_profile
            rooms = data_layer.load_table("rooms")
            room = next((r for r in rooms if r.get("room_no") == room_no), None)
            guest_name = room.get("guest_name", "") if room else ""
            ctx = guest_profile.get_guest_profile_context(room_no=room_no, guest_name=guest_name)
            if ctx:
                profile_context = f"\n{ctx}"
        except Exception:
            pass

        prompt = (
            f"你是酒店前台 AI 副驾，帮员工生成给客人的回复话术。\n\n"
            f"工单信息：\n"
            f"- 房间：{room_no}\n"
            f"- 类型：{work_type}\n"
            f"- 描述：{description}\n"
            f"- 状态：{status}\n"
            f"- 处理人：{assignee}\n"
            f"{profile_context}\n"
            f"请生成一段简洁、专业、有温度的回复话术，用于告知客人工单进展。"
            f"如果是已完成状态，表达歉意并询问是否满意；"
            f"如果是处理中，告知预计时间；"
            f"如果是待处理，确认已收到并告知会尽快安排。"
            f"回复控制在100字以内，可以直接发给客人。"
        )

        # 调 AI 智能体
        try:
            agent_id = wecom_kf.get_kf_ai_agent()
            from ..routes.ai_assistants import _do_chat_with_ai_assistant, _resolve_base_url
            base_url = _resolve_base_url(None)
            body = {
                "message": prompt,
                "user_id": f"copilot-{user_name}-{wo_id}",
                "timeout": 30,
            }
            result = await _do_chat_with_ai_assistant(agent_id, body, base_url)
            if result.get("success"):
                reply = (result.get("response") or "").strip()
                if reply and reply != "(无回复)":
                    return {"ok": True, "wo_id": wo_id, "suggested_reply": reply}
        except Exception as exc:
            logger.warning("[staff/ai-reply] AI 生成失败: %s", exc)

        # 兜底模板
        status_templates = {
            "pending": f"您好，您的{work_type}需求已收到（房间{room_no}），我们会尽快安排人员处理，请稍候。",
            "assigned": f"您好，您的{work_type}需求已派单给{assignee}（房间{room_no}），预计很快上门。",
            "in_progress": f"您好，{assignee}正在处理您的{work_type}需求（房间{room_no}），预计还需一些时间，感谢耐心等待。",
            "done": f"您好，您的{work_type}需求已处理完成（房间{room_no}）。如有任何问题请随时联系我们，祝您入住愉快！",
        }
        fallback = status_templates.get(status, f"您好，您的需求（房间{room_no}）我们正在处理中，请稍候。")
        return {"ok": True, "wo_id": wo_id, "suggested_reply": fallback, "from_template": True}

    # ─────────────────────────────────────────
    # POST /my/tasks/{wo_id}/accept — 接受任务
    # ─────────────────────────────────────────

    @router.post("/my/tasks/{wo_id}/accept")
    async def accept_task(
        request: Request,
        wo_id: str = "",
    ) -> Dict[str, Any]:
        """接受任务: assigned → in_progress"""
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                # 校验: 必须是自己的工单
                assignee_id = w.get("assignee_id", "")
                assignee = w.get("assignee", "")
                if assignee_id != user_id and assignee != user_name:
                    raise HTTPException(status_code=403, detail="这不是分配给你的任务")
                # 校验状态
                if w.get("status") != "assigned":
                    raise HTTPException(
                        status_code=400,
                        detail=f"任务当前状态是 {w.get('status')}，只能接受 assigned 状态的任务"
                    )
                # v1.6.1 统一收口: 委托 service (history/timeline/通知/客人回执/safe_sync 单一实现)
                res = await svc_accept_work_order(wo_id, operator=user_name or user_id)
                if not res.get("ok"):
                    raise HTTPException(status_code=400, detail=res.get("error", "接单失败"))
                return {"ok": True, "task": res.get("work_order", w), "message": "已接受任务，开始执行"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /my/tasks/{wo_id}/done — 完成任务
    # ─────────────────────────────────────────

    @router.post("/my/tasks/{wo_id}/done")
    async def done_task(
        request: Request,
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """完成任务: in_progress → done

        Body:
          result_note: 完成备注（可选）
          photo_urls: 照片 URL 列表（可选，企微上传后填）
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")
        result_note = payload.get("result_note", "")
        photo_urls = payload.get("photo_urls", [])

        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                # 校验: 必须是自己的工单
                assignee_id = w.get("assignee_id", "")
                assignee = w.get("assignee", "")
                if assignee_id != user_id and assignee != user_name:
                    raise HTTPException(status_code=403, detail="这不是分配给你的任务")
                # 校验状态: 必须先接单(in_progress)才能完成, 不允许从 assigned 直接跳到 done
                if w.get("status") != "in_progress":
                    raise HTTPException(
                        status_code=400,
                        detail=f"任务当前状态是 {w.get('status')}，请先接单再完成"
                    )
                # v1.6.1 统一收口: 委托 service (在住保护/耗时/pending_actions/通知/safe_sync 单一实现)
                # 修复: 此前无条件把房间设空房, 会覆盖在住客人
                res = await svc_complete_work_order(
                    wo_id, operator=user_name or user_id,
                    result_note=result_note, photo_urls=photo_urls,
                )
                if not res.get("ok"):
                    raise HTTPException(status_code=400, detail=res.get("error", "完成失败"))
                return {"ok": True, "task": res.get("work_order", w), "message": "任务已完成"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /my/tasks/{wo_id}/pause — 暂停/退回任务
    # ─────────────────────────────────────────

    @router.post("/my/tasks/{wo_id}/pause")
    async def pause_task(
        request: Request,
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """暂停任务: in_progress → assigned（退回队列，等待重新派单）

        Body:
          reason: 暂停原因（必填）
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")
        reason = payload.get("reason", "")
        if not reason:
            raise HTTPException(status_code=400, detail="暂停原因必填")

        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                assignee_id = w.get("assignee_id", "")
                assignee = w.get("assignee", "")
                if assignee_id != user_id and assignee != user_name:
                    raise HTTPException(status_code=403, detail="这不是分配给你的任务")
                if w.get("status") != "in_progress":
                    raise HTTPException(
                        status_code=400,
                        detail=f"任务当前状态是 {w.get('status')}，只能暂停 in_progress 状态的任务"
                    )
                # v1.6.1 统一收口: 委托 service
                res = await svc_pause_work_order(wo_id, operator=user_name or user_id, reason=reason)
                if not res.get("ok"):
                    raise HTTPException(status_code=400, detail=res.get("error", "暂停失败"))
                return {"ok": True, "task": res.get("work_order", w), "message": "任务已暂停，等待重新分配"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /my/tasks/{wo_id}/transfer — 员工转单（退回工单池）
    # ─────────────────────────────────────────

    @router.post("/my/tasks/{wo_id}/transfer")
    async def transfer_task(
        request: Request,
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """员工主动转单：assigned / in_progress → pending（退回派单池，等待重新分配）

        Body:
          reason: 转单原因（可选）
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")
        reason = payload.get("reason", "员工转单")

        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                assignee_id = w.get("assignee_id", "")
                assignee = w.get("assignee", "")
                if assignee_id != user_id and assignee != user_name and not sess.get("is_manager"):
                    raise HTTPException(status_code=403, detail="这不是分配给你的任务")
                old_status = w.get("status", "")
                if old_status not in ("assigned", "in_progress"):
                    raise HTTPException(status_code=400, detail=f"当前状态 {old_status} 不能转单")

                w["status"] = "pending"
                w["assignee_id"] = ""
                w["assignee"] = ""
                w["updated_at"] = now()
                w.setdefault("history", []).append({
                    "time": now(),
                    "from": old_status,
                    "to": "pending",
                    "operator": user_name or user_id,
                    "note": f"员工转单: {reason}",
                })
                data_layer.save_table("work_orders", wos)
                await safe_sync("work_orders", w)
                return {"ok": True, "task": w, "message": "已转单，等待重新分配"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    # ─────────────────────────────────────────
    # POST /my/supply-request — 申领耗材
    # ─────────────────────────────────────────

    @router.post("/my/supply-request")
    async def supply_request(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """申领耗材，自动创建补货工单

        Body:
          room_no: 房间号（必填）
          items: 物品列表，如 ["毛巾x2", "牙刷x1"]（必填）
          note: 备注（可选）
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        room_no = payload.get("room_no", "").strip()
        items = payload.get("items", [])
        note = payload.get("note", "")

        if not room_no:
            raise HTTPException(status_code=400, detail="房间号必填")
        if not items:
            raise HTTPException(status_code=400, detail="物品列表必填")

        # 校验房间存在
        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        if not room:
            raise HTTPException(status_code=404, detail=f"房间 {room_no} 不存在")

        # 构建描述
        items_str = ", ".join(items) if isinstance(items, list) else str(items)
        description = f"耗材申领: {items_str}"
        if note:
            description += f" ({note})"

        # 创建补货工单
        # v1.6.1 统一收口: 委托 service (pending 前台确认派单; 补上此前漏的同步+通知)
        _res = await svc_create_work_order(
            room_no=room_no,
            work_type="补充消耗品",
            description=description,
            priority="normal",
            reporter=user_name or user_id or "staff",
            target_dept="housekeeping",
            data_source="manual",
            operator=user_id,
            auto_dispatch=False,
        )
        wo = _res.get("work_order", {})

        return {
            "ok": True,
            "work_order": wo,
            "message": f"已提交耗材申领，等待前台确认派单",
        }

    # ─────────────────────────────────────────
    # GET /my/stats — 我的工作统计
    # ─────────────────────────────────────────

    @router.get("/my/stats")
    async def my_stats(request: Request) -> Dict[str, Any]:
        """返回当前员工的工作统计"""
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        wos = data_layer.load_table("work_orders")
        today_str = now()[:10]  # YYYY-MM-DD

        # 统计
        total = 0
        today_done = 0
        today_assigned = 0
        in_progress = 0
        pending = 0

        for w in wos:
            assignee_id = w.get("assignee_id", "")
            assignee = w.get("assignee", "")
            if assignee_id != user_id and assignee != user_name:
                continue

            total += 1
            status = w.get("status", "")
            created = w.get("created_at", "")

            if status == "in_progress":
                in_progress += 1
            elif status in ("assigned", "pending"):
                pending += 1

            # 今日完成
            if status == "done" and w.get("completed_at", "").startswith(today_str):
                today_done += 1

            # 今日分配
            if created.startswith(today_str):
                today_assigned += 1

        return {
            "ok": True,
            "user_id": user_id,
            "name": user_name,
            "total": total,
            "today_assigned": today_assigned,
            "today_done": today_done,
            "in_progress": in_progress,
            "pending": pending,
        }

    # ─────────────────────────────────────────
    # GET /my/rooms — 我负责的房间
    # ─────────────────────────────────────────

    @router.get("/my/rooms")
    async def my_rooms(request: Request) -> Dict[str, Any]:
        """返回当前员工负责的房间（清洁阿姨看脏房/待打扫）

        主要给客房部员工用：查看哪些房间需要打扫
        """
        sess = _get_staff_session(request)
        user_id = sess.get("user_id", "")
        user_name = sess.get("name", "")

        # 查当前员工的未完成清洁工单
        wos = data_layer.load_table("work_orders")
        my_room_nos = set()
        for w in wos:
            assignee_id = w.get("assignee_id", "")
            assignee = w.get("assignee", "")
            if assignee_id != user_id and assignee != user_name:
                continue
            if w.get("work_type") == "清洁" and w.get("status") in ("assigned", "in_progress"):
                my_room_nos.add(w.get("room_no", ""))

        # 查房间详情
        rooms = data_layer.load_table("rooms")
        my_rooms = [r for r in rooms if r.get("room_no") in my_room_nos]

        return {
            "ok": True,
            "count": len(my_rooms),
            "rooms": my_rooms,
        }

    app.include_router(router)
    logger.info("[routes/staff_portal] 已注册 9 个员工端路由")
