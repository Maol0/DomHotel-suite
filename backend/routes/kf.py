# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 企微微信客服 API (kf) v1.4.0

微信客服正确架构 (修复旧版收不到消息的问题):
  ① 企微 POST 加密事件通知到 /kf/callback (不含消息体)
  ② 验签+AES解密 → 拿事件 Token
  ③ BackgroundTasks 里调 sync_msg 拉取消息并处理 (回调 5 秒内必须响应,
     AI 应答 30~60s 放后台)
  ④ 命令模式 (绑定/状态/报修) / AI 应答 / 欢迎语 / 内部通知

路由清单:
  GET  /kf/callback              — 企微验证回调 URL (验签+解密 echostr)
  POST /kf/callback              — 企微客服事件通知 (加密)
  POST /kf/bind                  — 手动绑定客人（管理端调用）
  POST /kf/bind/scan             — 扫码绑定（客人扫码后调用）
  GET  /kf/bindings              — 查看所有绑定（管理端）
  DELETE /kf/bindings/{customer_id} — 解绑客人
  POST /kf/test/send             — 测试发送消息
  GET  /kf/config                — 客服配置状态 + 回调 URL
  POST /kf/unbind/{customer_id}  — 解绑客人（企微侧）
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, Query
from fastapi.responses import PlainTextResponse

from .. import wecom_kf
from .. import wecom_sync
from .. import data_layer
from ..auth import require_manager, get_session
from ..routes._helpers import now

logger = logging.getLogger(__name__)
router = APIRouter()


def register_routes(app) -> None:
    """注册客服路由"""

    # ─────────────────────────────────────────
    # GET /kf/callback — 企微验证回调 URL
    # ─────────────────────────────────────────

    @router.get("/kf/callback")
    async def kf_callback_verify(
        request: Request,
        msg_signature: str = Query(default=""),
        timestamp: str = Query(default=""),
        nonce: str = Query(default=""),
        echostr: str = Query(default=""),
    ) -> Any:
        """企微后台保存回调 URL 时的验证请求

        企微 GET 本端点带加密 echostr, 验签解密后必须回明文。
        """
        plain = wecom_kf.verify_kf_callback(msg_signature, timestamp, nonce, echostr)
        if plain:
            logger.info("[kf/callback] 回调 URL 验证成功")
            return PlainTextResponse(plain)
        logger.warning("[kf/callback] 回调 URL 验证失败 (WECOM_KF_TOKEN/ENCODING_AES_KEY 配置或签名不对)")
        return PlainTextResponse("verification failed", status_code=403)

    # ─────────────────────────────────────────
    # POST /kf/callback — 企微客服事件通知 (加密)
    # ─────────────────────────────────────────

    @router.post("/kf/callback")
    async def kf_callback(
        request: Request,
        background_tasks: BackgroundTasks,
        msg_signature: str = Query(default=""),
        timestamp: str = Query(default=""),
        nonce: str = Query(default=""),
    ) -> Any:
        """接收企微客服事件通知 (v1.4.0 正确架构)

        企微推的是 AES 加密的事件通知 (Event=kf_msg_or_event, 带 Token/OpenKfId),
        不含消息体。验签解密后用 Token 调 sync_msg 拉取消息。
        拉取+处理放 BackgroundTasks — 企微要求 5 秒内响应。
        """
        logger.warning("[kf/callback] 收到 POST 回调: msg_signature=%s timestamp=%s nonce=%s", bool(msg_signature), timestamp, nonce)
        try:
            body = (await request.body()).decode("utf-8", errors="replace")
            plain_xml = wecom_kf.decrypt_callback_body(
                body, msg_signature, timestamp, nonce
            )
            if not plain_xml:
                logger.warning("[kf/callback] 验签/解密失败, 忽略本次通知")
                return PlainTextResponse("ok")

            logger.warning("[kf/callback] 解密后 XML: %s", plain_xml[:500])
            notice = wecom_kf.parse_kf_event_notice(plain_xml)
            logger.warning("[kf/callback] 解析通知: %s", notice)
            if notice.get("event") != "kf_msg_or_event":
                logger.info("[kf/callback] 忽略非消息事件: %s", notice.get("event"))
                return PlainTextResponse("ok")

            event_token = notice.get("token", "")
            open_kfid = notice.get("open_kfid", "")
            if not event_token:
                logger.warning("[kf/callback] 事件通知缺 Token, 无法拉取")
                return PlainTextResponse("ok")

            logger.warning("[kf/callback] 触发 sync_and_process: token=%s open_kfid=%s", bool(event_token), open_kfid)
            # 立即响应企微 (5s 超时), 拉取+AI 处理放后台
            background_tasks.add_task(wecom_kf.sync_and_process, event_token, open_kfid)
        except Exception as e:
            logger.error("[kf/callback] 处理事件通知失败: %s", e, exc_info=True)

        # 企微约定: 无论内部处理如何, 回包必须是明文 "ok" (否则企微会重推)
        return PlainTextResponse("ok")

    # ─────────────────────────────────────────
    # POST /kf/bind — 手动绑定客人
    # ─────────────────────────────────────────

    @router.post("/kf/bind")
    async def kf_bind(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """手动绑定客人（管理端调用）

        Body:
          external_userid: 企微外部联系人 ID（必填）
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
          guest_phone: 手机号（可选）
        """
        session = get_session(request)
        if not session:
            raise HTTPException(401, "未登录")
        if not session.get("is_manager"):
            raise HTTPException(403, "需要 manager 权限")

        ext_uid = payload.get("external_userid", "").strip()
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()

        if not ext_uid or not room_no or not guest_name:
            raise HTTPException(400, "external_userid, room_no, guest_name 必填")

        # 校验房间存在
        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        if not room:
            raise HTTPException(404, f"房间 {room_no} 不存在")

        customer = wecom_kf.bind_customer(
            external_userid=ext_uid,
            room_no=room_no,
            guest_name=guest_name,
            guest_phone=payload.get("guest_phone", ""),
        )

        return {"ok": True, "customer": customer, "message": f"已绑定房间 {room_no}"}

    # ─────────────────────────────────────────
    # POST /kf/bind/scan — 扫码绑定
    # ─────────────────────────────────────────

    @router.post("/kf/bind/scan")
    async def kf_bind_scan(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """扫码绑定（客人扫码后调用）

        前端页面引导客人输入房间号和姓名，然后调用此接口绑定。
        需要客人先在企微客服号发过消息（获取 external_userid）。

        Body:
          external_userid: 企微外部联系人 ID（从企微客服消息回调获取）
          room_no: 房间号（必填）
          guest_name: 客人姓名（必填）
          guest_phone: 手机号（可选）
        """
        ext_uid = payload.get("external_userid", "").strip()
        room_no = payload.get("room_no", "").strip()
        guest_name = payload.get("guest_name", "").strip()

        if not ext_uid or not room_no or not guest_name:
            raise HTTPException(400, "external_userid, room_no, guest_name 必填")

        # 校验房间存在且在住
        rooms = data_layer.load_table("rooms")
        room = next((r for r in rooms if r.get("room_no") == room_no), None)
        if not room:
            raise HTTPException(404, f"房间 {room_no} 不存在")

        # 验证客人姓名（宽松匹配）
        stored_name = room.get("guest_name", "")
        if stored_name and guest_name not in stored_name and stored_name not in guest_name:
            raise HTTPException(403, "姓名与房间登记信息不符")

        customer = wecom_kf.bind_customer(
            external_userid=ext_uid,
            room_no=room_no,
            guest_name=guest_name,
            guest_phone=payload.get("guest_phone", ""),
        )

        # 发送绑定成功消息
        await wecom_kf.send_kf_message(
            ext_uid,
            "text",
            f"✅ 绑定成功！\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"房间：{room_no}\n"
            f"姓名：{guest_name}\n\n"
            f"后续报修和需求通知将通过此客服号推送给您。\n"
            f"发送「状态」查询房间状态，发送「报修 <描述>」快速报修。",
        )

        return {"ok": True, "customer": customer, "message": "绑定成功"}

    # ─────────────────────────────────────────
    # GET /kf/bindings — 查看所有绑定
    # ─────────────────────────────────────────

    @router.get("/kf/bindings")
    async def kf_bindings(
        request: Request,
        room_no: str = "",
    ) -> Dict[str, Any]:
        """查看客人绑定列表

        Query params:
          room_no: 按房间号过滤（可选）
        """
        session = get_session(request)
        if not session:
            raise HTTPException(401, "未登录")

        customers = wecom_kf.load_customers()
        if room_no:
            customers = [c for c in customers if c.get("room_no") == room_no]
        customers = [c for c in customers if not c.get("deleted")]

        return {"ok": True, "count": len(customers), "bindings": customers}

    # ─────────────────────────────────────────
    # DELETE /kf/bindings/{customer_id} — 解绑
    # ─────────────────────────────────────────

    @router.delete("/kf/bindings/{customer_id}")
    async def kf_unbind(
        request: Request,
        customer_id: str = "",
    ) -> Dict[str, Any]:
        """解绑客人"""
        session = get_session(request)
        if not session:
            raise HTTPException(401, "未登录")
        if not session.get("is_manager"):
            raise HTTPException(403, "需要 manager 权限")

        customers = wecom_kf.load_customers()
        for c in customers:
            if c.get("customer_id") == customer_id:
                c["deleted"] = True
                c["unbound_at"] = now()
                c["updated_at"] = now()
                wecom_kf.save_customers(customers)

                # 通知客人
                ext_uid = c.get("external_userid", "")
                if ext_uid:
                    await wecom_kf.send_kf_message(
                        ext_uid,
                        "text",
                        "ℹ️ 您的房间绑定已解除，后续通知将不再推送。",
                    )

                return {"ok": True, "message": "已解绑"}
        raise HTTPException(404, f"绑定记录 {customer_id} 不存在")

    # ─────────────────────────────────────────
    # POST /kf/test/send — 测试发送
    # ─────────────────────────────────────────

    @router.post("/kf/test/send")
    async def kf_test_send(
        request: Request,
        payload: dict = Body(default_factory=dict),
    ) -> Dict[str, Any]:
        """测试发送客服消息

        Body:
          external_userid: 企微外部联系人 ID（必填）
          message: 消息内容（必填）
        """
        session = get_session(request)
        if not session:
            raise HTTPException(401, "未登录")
        if not session.get("is_manager"):
            raise HTTPException(403, "需要 manager 权限")

        ext_uid = payload.get("external_userid", "").strip()
        message = payload.get("message", "").strip()

        if not ext_uid or not message:
            raise HTTPException(400, "external_userid 和 message 必填")

        result = await wecom_kf.send_kf_message(ext_uid, "text", message)
        return {"ok": result.get("ok", False), "result": result}

    # ─────────────────────────────────────────
    # GET /kf/config — 客服配置状态
    # ─────────────────────────────────────────

    @router.get("/kf/config")
    async def kf_config(request: Request) -> Dict[str, Any]:
        """查看客服配置状态 (v1.4.0: 含回调 URL / AI 智能体 / 内部通知配置)"""
        # 回调 URL 优先用已记住的部署地址 (wecom_kf_config 传入一次即存),
        # 否则从 Request 推断 (用户从哪个地址访问就显示哪个, 反代场景也正确);
        # 换服务器部署后传新 callback_base_url 覆盖即可
        base = wecom_sync.get_kf_setting("KF_CALLBACK_BASE_URL").rstrip("/")
        if not base:
            try:
                base = str(request.base_url).rstrip("/")
            except Exception:
                pass
        notify_uids = wecom_kf.get_kf_notify_userids()
        return {
            "ok": True,
            "kf_id": wecom_kf.get_kf_id(),
            "kf_id_configured": bool(wecom_kf.get_kf_id()),
            "kf_token_configured": bool(wecom_kf.get_kf_token()),
            "kf_encoding_aes_key_configured": bool(wecom_kf.get_kf_aes_key()),
            "fully_configured": wecom_kf.is_configured(),
            "corp_id_configured": bool(wecom_sync.get_corp_id()),
            # v1.4.0
            "callback_url": f"{base}/api/domhotel-suite/kf/callback" if base else "",
            "callback_base_url": base,
            "ai_agent_id": wecom_kf.get_kf_ai_agent(),
            "notify_userids": notify_uids,
            "notify_userids_count": len(notify_uids),
            "agent_id_configured": bool(wecom_sync.get_kf_setting("WECOM_AGENT_ID")),
            "customers_count": len([c for c in wecom_kf.load_customers() if not c.get("deleted")]),
        }

    # ─────────────────────────────────────────
    # POST /kf/unbind/{customer_id} — 解绑（企微侧）
    # ─────────────────────────────────────────

    @router.post("/kf/unbind/{customer_id}")
    async def kf_unbind_wecom(
        request: Request,
        customer_id: str = "",
    ) -> Dict[str, Any]:
        """解绑客人（同时在企微侧解绑）"""
        session = get_session(request)
        if not session:
            raise HTTPException(401, "未登录")
        if not session.get("is_manager"):
            raise HTTPException(403, "需要 manager 权限")

        customers = wecom_kf.load_customers()
        for c in customers:
            if c.get("customer_id") == customer_id:
                ext_uid = c.get("external_userid", "")

                # 本地解绑
                c["deleted"] = True
                c["unbound_at"] = now()
                c["updated_at"] = now()
                wecom_kf.save_customers(customers)

                # 通知客人
                if ext_uid:
                    await wecom_kf.send_kf_message(
                        ext_uid,
                        "text",
                        "ℹ️ 您的房间绑定已解除。",
                    )

                return {"ok": True, "message": "已解绑"}
        raise HTTPException(404, f"绑定记录 {customer_id} 不存在")

    app.include_router(router)
    logger.info("[routes/kf] 已注册 9 个客服路由 (v1.4.0: GET 验证 + POST 事件通知 + sync_msg)")
