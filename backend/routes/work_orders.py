# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 工单路由 (6 endpoints)

"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx

from fastapi import APIRouter, Depends, HTTPException, Body, Query

from .. import data_layer
from .. import auth
from ..wecom_sync import safe_sync
from .. import wecom_kf
from ._helpers import now, create_work_order
from .pending import create_pending_action
from .work_order_svc import svc_assign_work_order, svc_complete_work_order, svc_create_work_order


def _notify_trace(msg: str) -> None:
    """通知链路可观测性: 插件 logger 不进 supervisor 日志, 唯 stderr 可见, 故双写。"""
    try:
        print(f"[WO\u901a\u77e5] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        logging.getLogger("domhotel-suite.work_orders").warning("[WO\u901a\u77e5] %s", msg)
    except Exception:
        pass


def _wecom_runtime_cfg() -> Dict[str, Any]:
    """读取企微运行时配置（绕过相对导入问题，直接读文件）"""
    # v1.4.0-persistent: 优先读持久化目录
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


async def _wecom_access_token() -> str:
    cfg = _wecom_runtime_cfg()
    corp_id = cfg.get("WECOM_CORP_ID") or os.environ.get("WECOM_CORP_ID", "")
    secret = cfg.get("WECOM_AGENT_SECRET") or os.environ.get("WECOM_AGENT_SECRET", "")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}",
            timeout=15,
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    return data["access_token"]


async def _send_app_message(userids: List[str], title: str, detail: str) -> Dict[str, Any]:
    """直接调企微应用消息接口"""
    if not userids:
        return {"ok": False, "skipped": True, "reason": "userids empty"}
    cfg = _wecom_runtime_cfg()
    agent_id = cfg.get("WECOM_AGENT_ID") or os.environ.get("WECOM_AGENT_ID", "")
    token = await _wecom_access_token()
    body = {
        "touser": "|".join(userids),
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": f"{title}\n\n{detail}"},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json=body,
            timeout=15,
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"message/send failed: {data}")
    return {"ok": True, "msgid": data.get("msgid")}


async def _send_group_notification(chatid: str, title: str, detail: str, target_dept: str = "") -> Dict[str, Any]:
    """通过 QwenPaw messages API 往部门群发工单通知（替代 appchat/send）

    v1.5.0: 改用 QwenPaw WeCom channel 的 messages API，
    绕过 appchat/send 只能发到本应用自建群的限制，
    可以发到企微部门群（需 CLI 机器人已在群内）。

    v1.5.1: 根据目标部门选择对应的 agent 发送，而不是统一用 CLI。
    """
    if not chatid:
        return {"ok": False, "skipped": True, "reason": "chatid empty"}

    # v1.6.1: 群发统一走持有企业微信长连接、且在各部门群内的网关 agent
    # (默认 hotel-wecom-assistant = 桂山大酒店bot)。各 hotel-ai-* 后端智能体通常没有
    # 活跃的 wecom 出站连接, 用它们的 bot 直发部门群会静默失败 → 群里收不到。
    # 需要换发送方时, 在运行时配置/env 设 NOTIFY_GROUP_AGENT_ID 覆盖。
    cfg = _wecom_runtime_cfg()
    # v1.6.3: 各部门群由"该部门自己的 bot"发送(实测: 系统自检消息正是各部门 bot 送达,
    # 而统一的 hotel-wecom-assistant 不在这些群内 -> 静默失败)。显式配置可覆盖。
    _DEPT_GROUP_AGENT = {
        "engineering": "hotel-ai-engineering",
        "housekeeping": "hotel-ai-housekeeping",
        "frontdesk": "hotel-ai-frontdesk",
        "guest-service": "hotel-ai-guest-service",
    }
    _override = cfg.get("NOTIFY_GROUP_AGENT_ID") or os.environ.get("NOTIFY_GROUP_AGENT_ID")
    agent_id = _override or _DEPT_GROUP_AGENT.get(target_dept, "hotel-wecom-assistant")
    dept_label = {"engineering": "工程", "housekeeping": "客房", "frontdesk": "前台",
                  "guest-service": "客服"}.get(target_dept, target_dept or "全员")

    text = f"{title}\n\n【{dept_label}】\n{detail}"
    body = {
        "channel": "wecom",
        "target_user": chatid,
        "target_session": f"wecom:group:{chatid}",
        "text": text,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:8088/api/messages/send",
                json=body,
                headers={"Content-Type": "application/json", "X-Agent-Id": agent_id},
                timeout=15,
            )
            data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"messages/send 返回失败: agent={agent_id} chatid={chatid} resp={data}")
        _notify_trace(f"部门群发送成功 agent={agent_id} chatid={chatid}")
        logger.info("[work_orders] 部门群通知已发送: agent=%s chatid=%s", agent_id, chatid)
        return {"ok": True, "chatid": chatid, "agent_id": agent_id}
    except Exception as exc:
        _notify_trace(f"部门群发送异常 agent={agent_id} chatid={chatid} err={exc}")
        logger.warning("[work_orders] 群通知发送异常: agent=%s chatid=%s err=%s",
                       agent_id, chatid, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


def _get_dept_chatid(target_dept: str) -> str:
    cfg = _wecom_runtime_cfg()
    raw = cfg.get("DEPT_APPCHAT_CHATIDS", "{}")
    if isinstance(raw, str):
        try:
            mapping = json.loads(raw)
        except Exception:
            mapping = {}
    else:
        mapping = raw or {}
    return mapping.get(target_dept, "")


async def _send_work_order_card(userids: List[str], wo: dict, action: str = "新工单") -> Dict[str, Any]:
    """给员工发工单模板卡片（点击打开员工 H5 工单页）

    v1.4.1: 替代纯文本通知，员工可在企微内点击查看详情并接单/完成。
    """
    if not userids:
        return {"ok": False, "skipped": True, "reason": "userids empty"}

    cfg = _wecom_runtime_cfg()
    agent_id = cfg.get("WECOM_AGENT_ID") or os.environ.get("WECOM_AGENT_ID", "")
    base_url = cfg.get("KF_CALLBACK_BASE_URL") or os.environ.get("KF_CALLBACK_BASE_URL", "")
    if not base_url:
        base_url = "https://guishan.paw.domai.fun"

    token = await _wecom_access_token()
    wo_id = wo.get("wo_id", "")
    room_no = wo.get("room_no", "—")
    work_type = wo.get("work_type", "—")
    priority = wo.get("priority", "normal")
    description = wo.get("description", "") or "暂无描述"
    assignee = wo.get("assignee") or wo.get("assignee_id") or "未分配"

    priority_label = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}.get(priority, priority)
    title = f"{action} — {room_no} · {work_type}"
    url = f"{base_url}/api/domhotel-suite/ui/staff-h5.html?wo_id={wo_id}"

    body = {
        "touser": "|".join(userids),
        "msgtype": "template_card",
        "agentid": int(agent_id) if str(agent_id).isdigit() else agent_id,
        "template_card": {
            "card_type": "text_notice",
            "source": {
                "desc": "桂山大酒店",
                "desc_color": 0,
            },
            "main_title": {
                "title": title,
                "desc": f"优先级：{priority_label}｜当前处理人：{assignee}",
            },
            "quote_area": {
                "type": 0,
                "text": description[:120],
            },
            "jump_list": [
                {"type": 1, "url": url, "title": "查看工单详情"},
            ],
            "card_action": {
                "type": 1,
                "url": url,
            },
        },
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json=body,
            timeout=15,
        )
        data = r.json()
    if data.get("errcode") != 0:
        logger.warning("[work_orders] 模板卡片请求体: %s", json.dumps(body, ensure_ascii=False))
        raise RuntimeError(f"template_card message/send failed: {data}")
    return {"ok": True, "msgid": data.get("msgid")}


logger = logging.getLogger(__name__)

# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()


def _resolve_notify_userids(wo: dict) -> List[str]:
    """根据工单执行人和目标部门，解析应通知的企微 userid 列表

    优先级：
      1. 已派单 → 通知执行人
      2. 未派单 → 通知目标部门所有带 wecom_userid 的员工
      3. 空列表由调用方回退到全局 KF_NOTIFY_USERIDS
    """
    userids: List[str] = []
    # 1. 优先通知执行人
    assignee_id = wo.get("assignee_id") or wo.get("assignee") or ""
    if assignee_id:
        staff = auth.find_staff_by_id(assignee_id) or auth.find_active_staff_by_name(assignee_id)
        if staff and staff.get("wecom_userid"):
            userids.append(staff["wecom_userid"])

    # 2. 未指定执行人时，按 target_dept 通知部门成员
    if not userids:
        target_dept = wo.get("target_dept", "")
        dept_name_map = {
            "housekeeping": ["酒店清洁", "客房服务", "客房部"],
            "engineering": ["酒店工程", "工程部"],
            "frontdesk": ["酒店前台", "前台部"],
            "restaurant": ["餐饮部", "餐厅"],
        }
        target_names = dept_name_map.get(target_dept, [target_dept])
        try:
            depts = data_layer.load_table("departments")
            staffs = data_layer.load_table("staff")
            dept_ids = set()
            for d in depts:
                if not d or d.get("deleted"):
                    continue
                dname = d.get("name", "")
                did = d.get("id", "")
                if did == target_dept or dname in target_names:
                    dept_ids.add(did)
                    continue
                for tn in target_names:
                    if tn and (tn in dname or dname in tn):
                        dept_ids.add(did)
                        break
            for s in staffs:
                if not s or s.get("deleted"):
                    continue
                if s.get("department_id") in dept_ids and s.get("wecom_userid"):
                    userids.append(s["wecom_userid"])
        except Exception:
            logger.debug("[work_orders] 按部门解析通知人失败", exc_info=True)

    return userids


async def _notify_staff_group(wo: dict, action: str) -> None:
    """往目标部门应用群聊发工单变更通知（不阻塞主流程）"""
    target_dept = wo.get("target_dept", "")
    try:
        chatid = _get_dept_chatid(target_dept)
        _notify_trace(f"部门群检查 dept={target_dept} chatid={chatid or '(无→跳过)'} wo={wo.get('wo_id','')} action={action}")
        logger.warning("[work_orders] 部门群通知检查: target_dept=%s chatid=%s", target_dept, bool(chatid))
        if not chatid:
            return
        title = f"🔔 工单{action} — {wo.get('room_no', '—')}"
        detail = (
            f"工单号：{wo.get('wo_id', '')}\n"
            f"类型：{wo.get('work_type', '')}\n"
            f"房间：{wo.get('room_no', '')}\n"
            f"当前状态：{wo.get('status', '')}\n"
            f"执行人：{wo.get('assignee', '未分配')}\n"
            f"时间：{now()}"
        )
        result = await _send_group_notification(chatid, title, detail, target_dept)
        _notify_trace(f"部门群通知结果 dept={target_dept} -> {result}")
        logger.warning("[work_orders] 部门群通知结果: %s", result)
    except Exception as exc:
        logger.warning("[work_orders] 部门群通知异常: %s", exc, exc_info=True)


_NOTIFY_RETRY_ATTEMPTS = int(os.environ.get("WO_NOTIFY_RETRY_ATTEMPTS", "3"))
_NOTIFY_RETRY_DELAY = float(os.environ.get("WO_NOTIFY_RETRY_DELAY", "1.5"))


async def _send_staff_notice_resilient(
    userids: List[str], title: str, detail: str,
    wo: dict, want_card: bool, card_action: str, action: str,
) -> bool:
    """发送员工个人通知 (文本 + 可选直达卡片)。

    v1.6.1: 瞬时失败退避重试 (吸收 token 限频/网络抖动/瞬时 60020);
    重试用尽仍失败 → 写入企微失败队列, 由后台 60s 循环补发, 杜绝偶发永久丢通知。
    """
    last_exc = None
    for attempt in range(_NOTIFY_RETRY_ATTEMPTS):
        try:
            await _send_app_message(userids, title, detail)
            if want_card:
                await _send_work_order_card(userids, wo, card_action)
            return True
        except Exception as exc:
            last_exc = exc
            _notify_trace(f"个人通知失败({attempt + 1}/{_NOTIFY_RETRY_ATTEMPTS}) action={action}: {exc}")
            logger.warning("[work_orders] 内部通知发送失败(第 %d/%d 次) action=%s: %s",
                           attempt + 1, _NOTIFY_RETRY_ATTEMPTS, action, exc)
            if attempt < _NOTIFY_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_NOTIFY_RETRY_DELAY * (attempt + 1))
    try:
        from ..wecom_sync import enqueue_notify
        enqueue_notify(
            userids, title, detail,
            wo_id=wo.get("wo_id", ""),
            card_action=card_action if want_card else "",
            error=f"inline retries exhausted: {last_exc}",
        )
        logger.warning("[work_orders] 内部通知已入失败队列待后台补发: wo=%s", wo.get("wo_id", ""))
    except Exception as exc:
        logger.warning("[work_orders] 通知入失败队列失败: %s", exc)
    return False


async def resend_staff_notice(payload: dict) -> bool:
    """后台补发入口 (wecom_sync.retry_pending 回调)。失败会抛出, 交由队列计数。"""
    userids = payload.get("userids") or []
    if not userids:
        return True
    await _send_app_message(userids, payload.get("title", ""), payload.get("detail", ""))
    card_action = payload.get("card_action", "")
    wo_id = payload.get("wo_id", "")
    if card_action and wo_id:
        wo = next((w for w in data_layer.load_table("work_orders")
                   if w.get("wo_id") == wo_id), None)
        if wo:
            await _send_work_order_card(userids, wo, card_action)
    return True


async def _notify_staff_wo_change(wo: dict, action: str, operator: str = "") -> None:
    """工单状态变更时给内部员工发企微应用消息通知（不阻塞主流程）

    v1.6.1: 统一走 _send_staff_notice_resilient (即时重试 + 失败入队补发);
    建单/派单类 (含 '新建(房态联动)') 均带直达员工 H5 的模板卡片。
    """
    userids: List[str] = []
    try:
        userids = _resolve_notify_userids(wo)
        title = f"🔔 工单{action} — {wo.get('room_no', '—')}"
        detail = (
            f"工单号：{wo.get('wo_id', '')}\n"
            f"类型：{wo.get('work_type', '')}\n"
            f"房间：{wo.get('room_no', '')}\n"
            f"当前状态：{wo.get('status', '')}\n"
            f"执行人：{wo.get('assignee', '未分配')}\n"
            f"操作人：{operator or 'system'}\n"
            f"时间：{now()}"
        )
        if not userids:
            cfg = _wecom_runtime_cfg()
            notify = cfg.get("KF_NOTIFY_USERIDS") or os.environ.get("KF_NOTIFY_USERIDS", "")
            userids = [u.strip() for u in notify.replace("，", ",").split(",") if u.strip()]

        # 建单/派单类带直达卡片 (startwith 覆盖 新建 / 新建(客人H5) / 新建(房态联动))
        want_card = action.startswith("新建") or action == "派单"
        card_action = ("新工单" if action.startswith("新建") else "工单已派单") if want_card else ""

        _notify_trace(f"个人通知入口 action={action} wo={wo.get('wo_id','')} dept={wo.get('target_dept','')} userids={userids}")
        if not userids:
            _notify_trace(f"无可用通知人→跳过个人通知 action={action} wo={wo.get('wo_id','')}")
            logger.warning("[work_orders] 无可用通知人, 跳过内部通知: action=%s wo=%s",
                           action, wo.get("wo_id", ""))
        else:
            _notify_trace(f"个人通知准备发送 action={action} userids={userids}")
            logger.warning("[work_orders] 内部通知准备发送: action=%s userids=%s", action, userids)
            sent = await _send_staff_notice_resilient(
                userids, title, detail, wo, want_card, card_action, action)
            _notify_trace(f"个人通知结果 action={action} sent={sent}")
            logger.warning("[work_orders] 内部通知结果: sent=%s", sent)
    except Exception as exc:
        logger.warning("[work_orders] 内部通知异常: %s", exc, exc_info=True)

    # 同步发部门群通知
    try:
        await _notify_staff_group(wo, action)
    except Exception as exc:
        logger.warning("[work_orders] 部门群通知异常: %s", exc, exc_info=True)


# v1.4.1: 超时未接单提醒
_timeout_reminder_started = False
_TIMEOUT_REMINDER_MINUTES = int(os.environ.get("WO_TIMEOUT_REMINDER_MIN", "15"))
_TIMEOUT_REMINDER_INTERVAL = int(os.environ.get("WO_TIMEOUT_REMINDER_INTERVAL", "600"))


def _parse_dt(s: str):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


async def _run_timeout_reminder():
    """扫描 pending 工单，超过 N 分钟未接单则补推提醒"""
    try:
        wos = data_layer.load_table("work_orders")
        now_dt = datetime.now()
        reminded = 0
        for wo in wos:
            if wo.get("status") != "pending":
                continue
            created = _parse_dt(wo.get("created_at", ""))
            if not created:
                continue
            elapsed = (now_dt - created).total_seconds() / 60
            if elapsed < _TIMEOUT_REMINDER_MINUTES:
                continue
            # 已经提醒过跳过
            if wo.get("timeout_reminded"):
                continue
            userids = _resolve_notify_userids(wo)
            if not userids:
                cfg = _wecom_runtime_cfg()
                notify = cfg.get("KF_NOTIFY_USERIDS") or os.environ.get("KF_NOTIFY_USERIDS", "")
                userids = [u.strip() for u in notify.replace("，", ",").split(",") if u.strip()]
            if not userids:
                continue
            try:
                await _send_app_message(
                    userids,
                    f"⏰ 工单超时未接单 — {wo.get('room_no', '—')}",
                    (
                        f"工单号：{wo.get('wo_id', '')}\n"
                        f"类型：{wo.get('work_type', '')}\n"
                        f"房间：{wo.get('room_no', '')}\n"
                        f"已等待：{int(elapsed)} 分钟\n"
                        f"请尽快安排人员处理。"
                    ),
                )
                await _send_work_order_card(userids, wo, "⏰ 超时未接单")
                wo["timeout_reminded"] = True
                wo["timeout_reminded_at"] = now()
                reminded += 1
            except Exception as exc:
                logger.warning("[work_orders] 超时提醒发送失败: %s", exc)
        if reminded:
            data_layer.save_table("work_orders", wos)
            logger.warning("[work_orders] 已发送 %d 条超时未接单提醒", reminded)
    except Exception as exc:
        logger.warning("[work_orders] 超时提醒扫描异常: %s", exc)


def start_timeout_reminder_loop(interval_s: int = _TIMEOUT_REMINDER_INTERVAL) -> None:
    """启动工单超时提醒后台循环"""
    global _timeout_reminder_started
    if _timeout_reminder_started:
        return
    _timeout_reminder_started = True

    async def _loop():
        await asyncio.sleep(30)
        while True:
            try:
                await _run_timeout_reminder()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[work_orders] 超时提醒循环异常: %s", exc)
            await asyncio.sleep(interval_s)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_loop())
            logger.info("[work_orders] 超时提醒循环已挂到 event loop")
            return
        raise RuntimeError("event loop 未运行")
    except RuntimeError:
        import threading

        def _runner():
            try:
                asyncio.run(_loop())
            except Exception as exc:
                logger.error("[work_orders] 超时提醒线程退出: %s", exc)

        threading.Thread(target=_runner, daemon=True, name="wo-timeout").start()
        logger.info("[work_orders] 超时提醒循环已在后台线程启动")


def _calc_duration(wo: dict) -> None:
    """计算工单耗时，写入 wo["duration"] 字段"""
    timeline = wo.get("timeline", {})
    created = timeline.get("created_at") or wo.get("created_at", "")
    started = timeline.get("started_at", "")
    completed = timeline.get("completed_at") or wo.get("completed_at", "")
    if not created or not completed:
        return
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        t_created = datetime.strptime(created[:19], fmt)
        t_completed = datetime.strptime(completed[:19], fmt)
        total = int((t_completed - t_created).total_seconds() / 60)
        wait = 0
        work = total
        if started:
            t_started = datetime.strptime(started[:19], fmt)
            wait = int((t_started - t_created).total_seconds() / 60)
            work = int((t_completed - t_started).total_seconds() / 60)
        wo["duration"] = {
            "total_minutes": max(total, 0),
            "wait_minutes": max(wait, 0),
            "work_minutes": max(work, 0),
        }
    except Exception:
        pass


def register_routes(app) -> None:
    """注册工单路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/work_orders")
    async def list_work_orders(
        ctx=Depends(get_ctx),
        status: str = "",
        target_dept: str = "",
        priority: str = "",
        room_no: str = "",
    ) -> List[Dict[str, Any]]:
        wos = data_layer.load_table("work_orders")
        result: List[Dict[str, Any]] = []
        for w in wos:
            if status and w.get("status") != status:
                continue
            if target_dept and w.get("target_dept") != target_dept:
                continue
            if priority and w.get("priority") != priority:
                continue
            if room_no and w.get("room_no") != room_no:
                continue
            result.append(w)
        order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        result.sort(
            key=lambda w: (order.get(w.get("priority", "normal"), 2), w.get("created_at", ""))
        )
        return result

    @router.post("/work_orders")
    async def create_work_order_endpoint(
        ctx=Depends(get_ctx),
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ) -> Dict[str, Any]:
        try:
            return await svc_create_work_order(
                room_no=payload["room_no"],
                work_type=payload["work_type"],
                description=payload["description"],
                priority=payload.get("priority", "normal"),
                reporter=payload.get("reporter", "guest"),
                target_dept=payload.get("target_dept", ""),
                data_source=payload.get("data_source", "manual"),
                operator=payload.get("operator", sess.get("user_id", "")),
            )
        except KeyError as ke:
            raise HTTPException(status_code=400, detail=f"缺少字段:{ke}")

    @router.post("/work_orders/{wo_id}/assign")
    async def assign_work_order(
        ctx=Depends(get_ctx),
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        try:
            result = await svc_assign_work_order(
                wo_id=wo_id,
                assignee=payload.get("assignee", ""),
                operator=payload.get("created_by", sess.get("user_id", "")),
                assignee_id=payload.get("assignee_id", ""),
            )
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("error", "派单失败"))
            wo = result["work_order"]
            return {
                "ok": True, "pending": False, "work_order": wo,
                "message": f"工单 {wo_id} 已派给 {wo.get('assignee', '')}",
                "new_status": wo.get("status"),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("[assign_work_order] 500 error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"派单失败: {str(e)[:200]}")

    @router.post("/work_orders/{wo_id}/confirm")
    async def confirm_work_order(
        ctx=Depends(get_ctx),
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ) -> Dict[str, Any]:
        """人工确认工单:
        - pending_confirm → pending (进入派单队列)
        - pending → assigned (确认派单，接受执行)
        - assigned → in_progress (确认开始执行)
        """
        confirmed_by = payload.get("confirmed_by", "staff")
        note = payload.get("note", "")
        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                cur_status = w.get("status")
                if cur_status == "pending_confirm":
                    # pending_confirm → pending (进入派单队列)
                    w["status"] = "pending"
                    w["confirmed_by"] = confirmed_by
                    w["confirmed_at"] = now()
                    w.setdefault("timeline", {})["confirmed_at"] = now()
                elif cur_status == "pending":
                    # pending → assigned (确认派单)
                    w["status"] = "assigned"
                    w["confirmed_by"] = confirmed_by
                    w["confirmed_at"] = now()
                    w.setdefault("timeline", {})["assigned_at"] = now()
                elif cur_status == "assigned":
                    # assigned → in_progress (确认开始执行)
                    w["status"] = "in_progress"
                    w["confirmed_by"] = confirmed_by
                    w["confirmed_at"] = now()
                    w.setdefault("timeline", {})["started_at"] = now()
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"工单 {wo_id} 当前状态 {cur_status} 不允许确认(允许 pending_confirm→pending, pending→assigned, assigned→in_progress)",
                    )
                w["updated_at"] = now()
                if note:
                    w["confirm_note"] = note
                w.setdefault("history", []).append({
                    "time": now(),
                    "operator": confirmed_by,
                    "note": f"确认 {cur_status} → {w['status']}" + (f" ({note})" if note else "")
                })
                data_layer.save_table("work_orders", wos)
                await safe_sync("work_orders", w)

                # 通知内部员工
                await _notify_staff_wo_change(w, "确认", confirmed_by)

                # v2.2.0: 企微客服推送 — 工单状态变更通知客人
                try:
                    notify_event = {
                        "pending_confirm": "received",
                        "pending": "received",
                        "assigned": "assigned",
                        "in_progress": "in_progress",
                    }.get(w["status"], "")
                    if notify_event and w.get("data_source") in ("guest", "guest_kf"):
                        await wecom_kf.notify_guest_work_order_change(w, notify_event)
                except Exception as kf_exc:
                    logger.debug("confirm_work_order 客服推送失败(不影响主流程): %s", kf_exc)

                return {"ok": True, "work_order": w, "transition": f"{cur_status} → {w['status']}"}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    @router.post("/work_orders/{wo_id}/reject")
    async def reject_work_order(
        ctx=Depends(get_ctx),
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ) -> Dict[str, Any]:
        """人工驳回工单:pending_confirm → rejected (不派单)"""
        rejected_by = payload.get("rejected_by", "staff")
        reason = payload.get("reason", "")
        wos = data_layer.load_table("work_orders")
        for w in wos:
            if w.get("wo_id") == wo_id:
                if w.get("status") not in ("pending_confirm",):
                    raise HTTPException(
                        status_code=400,
                        detail=f"工单 {wo_id} 当前状态 {w.get('status')} 不允许驳回",
                    )
                w["status"] = "rejected"
                w["updated_at"] = now()
                w["rejected_by"] = rejected_by
                w["rejected_at"] = now()
                if reason:
                    w["reject_reason"] = reason
                data_layer.save_table("work_orders", wos)

                # v2.1.20: 同步更新 pending_actions — 驳回后自动 reject 对应的待确认记录
                try:
                    pas = data_layer.load_table("pending_actions")
                    changed = False
                    for pa in pas:
                        if pa.get("status") != "pending":
                            continue
                        rec = pa.get("target_record") or {}
                        if rec.get("wo_id") == wo_id:
                            pa["status"] = "rejected"
                            pa["rejected_by"] = rejected_by
                            pa["rejected_at"] = now()
                            pa["reject_reason"] = reason or "工单已驳回"
                            changed = True
                    if changed:
                        data_layer.save_table("pending_actions", pas)
                except Exception as exc:
                    logger.warning("reject_work_order: 同步 pending_actions 失败: %s", exc)

                await safe_sync("work_orders", w)

                # 通知内部员工
                await _notify_staff_wo_change(w, "驳回", rejected_by)

                # v2.2.0: 企微客服推送 — 工单驳回通知客人
                try:
                    if w.get("data_source") in ("guest", "guest_kf"):
                        await wecom_kf.notify_guest_work_order_change(w, "done")
                except Exception as kf_exc:
                    logger.debug("reject_work_order 客服推送失败(不影响主流程): %s", kf_exc)

                return {"ok": True, "work_order": w}
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")

    @router.post("/work_orders/{wo_id}/complete")
    async def complete_work_order(
        ctx=Depends(get_ctx),
        wo_id: str = "",
        payload: dict = Body(default_factory=dict),
        sess: Dict[str, Any] = Depends(auth.require_employee()),
    ) -> Dict[str, Any]:
        result = await svc_complete_work_order(
            wo_id=wo_id,
            operator=sess.get("name", "") or sess.get("user_id", ""),
            result_note=payload.get("result_note", ""),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "工单不存在"))
        return result

    
    app.include_router(router)

    # ─────────────────────────────────────────────
    # 历史工单 API（独立注册，不走 auth 依赖）
    # ─────────────────────────────────────────────

    @router.get("/work_orders/history/calendar")
    async def history_calendar(
        year: int = Query(0),
        month: int = Query(0),
    ) -> Dict[str, Any]:
        """获取某月有已完成工单的日期列表

        Query params:
          year: 年份（默认当前年）
          month: 月份（默认当前月）
        """
        from datetime import datetime as _dt
        now_dt = _dt.now()
        y = year or now_dt.year
        m = month or now_dt.month

        wos = data_layer.load_table("work_orders")
        # 也查归档文件
        archive = []
        try:
            archive = data_layer.load_table("work_orders_archive")
        except Exception:
            pass

        all_wos = wos + archive
        days: Dict[str, Dict[str, Any]] = {}
        prefix = f"{y:04d}-{m:02d}-"

        for w in all_wos:
            if w.get("status") != "done":
                continue
            completed = w.get("completed_at", "")
            if not completed.startswith(prefix):
                continue
            day = completed[:10]
            if day not in days:
                days[day] = {"count": 0}
            days[day]["count"] += 1

        return {"ok": True, "year": y, "month": m, "days": days}

    @router.get("/work_orders/history/by-date")
    async def history_by_date(
        date: str = Query(""),
    ) -> Dict[str, Any]:
        """获取某天的已完成工单列表

        Query params:
          date: 日期 YYYY-MM-DD（默认今天）
        """
        if not date:
            date = now()[:10]

        wos = data_layer.load_table("work_orders")
        archive = []
        try:
            archive = data_layer.load_table("work_orders_archive")
        except Exception:
            pass

        all_wos = wos + archive
        result = []
        for w in all_wos:
            if w.get("status") != "done":
                continue
            if not w.get("completed_at", "").startswith(date):
                continue
            # 补算耗时（兼容旧数据）
            if not w.get("duration"):
                _calc_duration(w)
            result.append(w)

        # 按完成时间倒序
        result.sort(key=lambda x: x.get("completed_at", ""), reverse=True)

        return {"ok": True, "date": date, "count": len(result), "tasks": result}

    @router.get("/work_orders/history/stats")
    async def history_stats(
        period: str = Query("month"),
        date_from: str = Query(""),
        date_to: str = Query(""),
    ) -> Dict[str, Any]:
        """统计分析

        Query params:
          period: week/month/quarter/custom
          date_from: 自定义起始日期
          date_to: 自定义结束日期
        """
        from datetime import datetime as _dt, timedelta
        now_dt = _dt.now()

        if period == "week":
            start = (now_dt - timedelta(days=now_dt.weekday())).strftime("%Y-%m-%d")
            end = now_dt.strftime("%Y-%m-%d")
        elif period == "month":
            start = f"{now_dt.year}-{now_dt.month:02d}-01"
            end = now_dt.strftime("%Y-%m-%d")
        elif period == "quarter":
            q = (now_dt.month - 1) // 3
            start = f"{now_dt.year}-{q*3+1:02d}-01"
            end = now_dt.strftime("%Y-%m-%d")
        else:
            start = date_from or f"{now_dt.year}-{now_dt.month:02d}-01"
            end = date_to or now_dt.strftime("%Y-%m-%d")

        wos = data_layer.load_table("work_orders")
        archive = []
        try:
            archive = data_layer.load_table("work_orders_archive")
        except Exception:
            pass

        all_wos = wos + archive
        filtered = []
        for w in all_wos:
            if w.get("status") != "done":
                continue
            completed = w.get("completed_at", "")[:10]
            if start <= completed <= end:
                if not w.get("duration"):
                    _calc_duration(w)
                filtered.append(w)

        # 统计
        from collections import Counter
        total = len(filtered)
        by_type: Dict[str, Dict] = {}
        by_assignee: Dict[str, Dict] = {}

        for w in filtered:
            wt = w.get("work_type", "其他")
            assignee = w.get("assignee", "未分配")
            duration = w.get("duration", {}).get("total_minutes", 0)
            rating = w.get("guest_rating", 0)

            if wt not in by_type:
                by_type[wt] = {"count": 0, "total_minutes": 0, "ratings": []}
            by_type[wt]["count"] += 1
            by_type[wt]["total_minutes"] += duration
            if rating:
                by_type[wt]["ratings"].append(rating)

            if assignee not in by_assignee:
                by_assignee[assignee] = {"count": 0, "total_minutes": 0, "ratings": []}
            by_assignee[assignee]["count"] += 1
            by_assignee[assignee]["total_minutes"] += duration
            if rating:
                by_assignee[assignee]["ratings"].append(rating)

        # 计算平均值
        for v in by_type.values():
            v["avg_minutes"] = round(v["total_minutes"] / v["count"], 1) if v["count"] else 0
            v["avg_rating"] = round(sum(v["ratings"]) / len(v["ratings"]), 1) if v["ratings"] else 0
            del v["ratings"]
            del v["total_minutes"]

        for v in by_assignee.values():
            v["avg_minutes"] = round(v["total_minutes"] / v["count"], 1) if v["count"] else 0
            v["avg_rating"] = round(sum(v["ratings"]) / len(v["ratings"]), 1) if v["ratings"] else 0
            del v["ratings"]
            del v["total_minutes"]

        return {
            "ok": True,
            "period": period,
            "date_from": start,
            "date_to": end,
            "total_completed": total,
            "by_type": by_type,
            "by_assignee": by_assignee,
        }

    @router.post("/admin/archive/clean")
    async def archive_clean(
        sess: Dict[str, Any] = Depends(auth.require_manager()),
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """清理本地已完成工单

        设计逻辑：
        1. 先检查企微表格是否已配置
        2. 已配置：推送到企微表格 → 再从本地删除
        3. 未配置：不清理，保留在本地（防止数据丢失）

        payload.force=True 可跳过企微检查（测试用）
        将 status=done 且 completed_at 超过 1 天的工单处理
        """
        force = payload.get("force", False)
        # 检查企微表格是否已配置
        wecom_configured = False
        try:
            wecom_configured = wecom_kf.is_configured()
        except Exception:
            pass

        if not wecom_configured and not force:
            return {
                "ok": False,
                "archived": 0,
                "kept": 0,
                "message": "企微表格未配置，暂不清理本地数据（防止数据丢失）。请先配置企微表格后再执行清理。",
            }

        today = now()[:10]
        wos = data_layer.load_table("work_orders")
        to_archive = []
        to_keep = []

        for w in wos:
            if w.get("status") == "done":
                completed = w.get("completed_at", "")[:10]
                if completed and completed != today:
                    to_archive.append(w)
                else:
                    to_keep.append(w)
            else:
                to_keep.append(w)

        if to_archive:
            if force:
                # force 模式：跳过企微推送，直接归档到本地
                archive = []
                try:
                    archive = data_layer.load_table("work_orders_archive")
                except Exception:
                    pass
                archive.extend(to_archive)
                data_layer.save_table("work_orders_archive", archive)
                data_layer.save_table("work_orders", to_keep)

                return {
                    "ok": True,
                    "archived": len(to_archive),
                    "kept": len(to_keep),
                    "message": f"[force] 本地归档 {len(to_archive)} 条（跳过企微推送），保留 {len(to_keep)} 条",
                }

            # 推送到企微表格
            pushed_count = 0
            for wo in to_archive:
                try:
                    await wecom_kf.push_to_done_table(wo)
                    pushed_count += 1
                except Exception as push_exc:
                    logger.warning("推送工单 %s 到企微表格失败: %s", wo.get("wo_id"), push_exc)

            # 只有推送成功的才从本地删除
            if pushed_count == len(to_archive):
                # 全部推送成功，归档到本地备份
                archive = []
                try:
                    archive = data_layer.load_table("work_orders_archive")
                except Exception:
                    pass
                archive.extend(to_archive)
                data_layer.save_table("work_orders_archive", archive)
                data_layer.save_table("work_orders", to_keep)

                return {
                    "ok": True,
                    "archived": pushed_count,
                    "kept": len(to_keep),
                    "message": f"已推送 {pushed_count} 条到企微表格，本地归档 {len(to_archive)} 条，保留 {len(to_keep)} 条",
                }
            else:
                # 部分推送失败，不删除本地数据
                return {
                    "ok": False,
                    "archived": 0,
                    "kept": len(wos),
                    "message": f"企微表格推送失败（成功 {pushed_count}/{len(to_archive)}），暂不清理本地数据",
                }

        return {
            "ok": True,
            "archived": 0,
            "kept": len(to_keep),
            "message": "无需清理，本地只有活跃工单和今日已完成工单",
        }

    # 企微表格推送预留接口
    @router.post("/admin/archive/push")
    async def archive_push(
        sess: Dict[str, Any] = Depends(auth.require_manager()),
    ) -> Dict[str, Any]:
        """推送已完成工单到企微表格（预留接口）

        TODO: 接入企微智能表格 work_orders_done
        """
        return {
            "ok": False,
            "message": "企微表格推送功能待配置，请先创建 work_orders_done 表并提供 doc_id",
        }

    logger.info("[routes/work_orders] 已注册 6 + 4 个工单路由（含历史查询）")


# ── v1.6.3 通知握手: 把通知实现注册进 svc, 供所有 svc_* 路径统一调用 ──
try:
    from . import work_order_svc as _svc
    _svc.register_notify(_notify_staff_wo_change)
    try:
        import sys as _s
        print("[WO通知] work_orders 已注册 _notify_staff_wo_change -> svc",
              file=_s.stderr, flush=True)
    except Exception:
        pass
except Exception as _exc:  # noqa: BLE001
    try:
        import sys as _s
        print(f"[WO通知] work_orders 注册握手失败: {_exc}", file=_s.stderr, flush=True)
    except Exception:
        pass