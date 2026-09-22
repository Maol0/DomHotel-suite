# -*- coding: utf-8 -*-
"""企微群机器人回调 — 部门 AI 助手

v1.0: 接收员工在部门群里 @机器人的消息,路由给对应 AI 助手,再把回复发回群里。

企微自定义机器人(可接收 @消息)回调格式(明文模式):
{
  "chat_type": "group",
  "msg_type": "text",
  "text": {"content": "@机器人 消息内容"},
  "from": {"userid": "ZhangSan"},
  "roomid": "R1234567890"
}

配置:
  wecom_runtime_config.json 里写:
  {
    "WECOM_BOT_WEBHOOKS": "{\"frontdesk\":\"key1\",\"engineering\":\"key2\",\"housekeeping\":\"key3\"}"
  }
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Request, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()

# 部门 → AI 助手 ID 映射
DEPT_AGENT_MAP = {
    "frontdesk": "hotel-ai-frontdesk",
    "engineering": "hotel-ai-engineering",
    "housekeeping": "hotel-ai-housekeeping",
}

WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"


def _load_runtime_cfg() -> dict:
    """读运行时配置 (复用 wecom_sync 同一文件)"""
    try:
        from .. import wecom_sync
        p = wecom_sync._runtime_config_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[wecom_bot] 读运行时配置失败: %s", exc)
    return {}


def _get_webhooks() -> Dict[str, str]:
    """解析 wecom_runtime_config.json 里的 WECOM_BOT_WEBHOOKS"""
    cfg = _load_runtime_cfg()
    raw = cfg.get("WECOM_BOT_WEBHOOKS") or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str) and v}
    except Exception:
        pass
    return {}


def _strip_at_bot(content: str) -> str:
    """去掉消息里 @机器人的前缀"""
    content = content.strip()
    # 企微 @机器人 在文本里可能显示为 "@机器人名称 " 或 XML
    # 简单处理:去掉以 @ 开头的第一段
    if content.startswith("@"):
        parts = content.split(None, 1)
        if len(parts) > 1:
            return parts[1].strip()
        return ""
    return content


async def _call_ai_assistant(agent_id: str, user_id: str, content: str) -> str:
    """调用 QwenPaw AI 助手生成回复"""
    try:
        from .ai_assistants import _do_chat_with_ai_assistant, _resolve_base_url
        base_url = _resolve_base_url(None)
        body = {
            "message": content,
            "user_id": user_id,
            "timeout": 60,
        }
        result = await _do_chat_with_ai_assistant(agent_id, body, base_url)
        if result.get("success"):
            reply = (result.get("response") or "").strip()
            if reply and reply != "(无回复)":
                if len(reply.encode("utf-8")) > 2000:
                    reply = reply[:650] + "\n…（回复过长已截断）"
                return reply
        logger.warning("[wecom_bot] AI 助手 %s 调用失败: %s", agent_id, result.get("error"))
    except Exception as exc:
        logger.warning("[wecom_bot] AI 助手 %s 调用异常: %s", agent_id, exc)
    return ""


async def _send_webhook(dept: str, content: str) -> bool:
    """通过群机器人 webhook 把回复发到群里"""
    hooks = _get_webhooks()
    key = hooks.get(dept)
    if not key:
        logger.warning("[wecom_bot] 部门 %s 未配置 webhook key", dept)
        return False
    url = WECOM_WEBHOOK_URL.format(key=key)
    body = {
        "msgtype": "text",
        "text": {"content": content},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=body)
            data = r.json()
        if data.get("errcode", 0) != 0:
            logger.warning("[wecom_bot] webhook 发送失败: %s", data)
            return False
        logger.info("[wecom_bot] 已回复到 %s 群", dept)
        return True
    except Exception as exc:
        logger.warning("[wecom_bot] webhook 请求异常: %s", exc)
        return False


def register_routes(app) -> None:
    """注册企微群机器人回调路由"""

    @router.get("/wecom/bot/{dept}")
    @router.post("/wecom/bot/{dept}")
    async def wecom_bot_callback(request: Request, dept: str):
        """企微群机器人回调入口

        - GET: 企微验证 URL 时调用,返回 echostr
        - POST: 员工 @机器人时,企微推送消息
        """
        dept = dept.lower().strip()
        if dept not in DEPT_AGENT_MAP:
            raise HTTPException(status_code=404, detail=f"未知部门: {dept}")

        params = dict(request.query_params)

        # GET 请求 — URL 验证
        if request.method == "GET":
            echostr = params.get("echostr", "")
            logger.info("[wecom_bot] GET 验证 dept=%s echostr=%s", dept, echostr[:20])
            if echostr:
                return echostr
            return {"errcode": 0, "errmsg": "ok"}

        # POST 请求 — 接收 @消息
        try:
            body = await request.json()
        except Exception:
            body = {}

        logger.info("[wecom_bot] POST dept=%s body=%s", dept, json.dumps(body, ensure_ascii=False)[:500])

        msg_type = body.get("msg_type", "")
        if msg_type != "text":
            return {"errcode": 0, "errmsg": "ok"}

        text = (body.get("text") or {}).get("content", "")
        from_userid = (body.get("from") or {}).get("userid", "unknown")
        content = _strip_at_bot(text)

        if not content:
            await _send_webhook(dept, "👋 你好，请 @我 并输入你想问的问题。")
            return {"errcode": 0, "errmsg": "ok"}

        agent_id = DEPT_AGENT_MAP[dept]
        user_id = f"wecom-bot-{dept}-{from_userid}"
        reply = await _call_ai_assistant(agent_id, user_id, content)

        if not reply:
            reply = "抱歉，我没听懂。你可以换一种方式描述你的问题。"

        # 发回群里
        await _send_webhook(dept, f"💬 {reply}")

        return {"errcode": 0, "errmsg": "ok"}

    app.include_router(router)
    logger.info("[routes/wecom_bot] 已注册 /wecom/bot/{frontdesk,engineering,housekeeping}")
