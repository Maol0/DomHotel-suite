# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — AI 助手管理路由 (7 endpoints)


Endpoints:
  - GET    /ai/assistants                        列出所有 AI 助手(含自检状态)
  - GET    /ai/assistants/{id}/status            单个 AI 助手自检详情
  - POST   /ai/assistants                        新建 AI 助手(基于模板)
  - DELETE /ai/assistants/{id}                   删除 AI 助手
  - POST   /ai/assistants/{id}/chat              对 AI 助手发起协同对话(聚合版,SSE 流)
  - POST   /ai/assistants/{id}/chat/stream       流式 chat(透传 SSE delta,前端实时打字)
  - GET    /ai/assistants/{id}/chats             列出对话历史
  - POST   /ai/assistants/{id}/selfcheck         自检(发一条自我介绍测试消息)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Body, Request, Response
from fastapi.responses import StreamingResponse

from ..wecom_sync import safe_sync
from .. import chat_session_manager
from ._helpers import now

logger = logging.getLogger(__name__)

# 模块顶层 router(让 routes/__init__.py 的合并机制能找到)
router = APIRouter()

WORKSPACE_BASE = Path(os.environ.get("QWENPAW_WORKING_DIR", "/app/working")) / "workspaces"

# v2.1.5 修复: 不再硬编码 localhost:8889 — plugin 在主进程内, 直接用 QwenPaw 内部 SDK
# 旧: QWENPAW_BASE_URL = os.environ.get("QWENPAW_BASE_URL", "http://localhost:8889")
# 用 load_config() 读主进程内存里的 agent 注册表
# 对于需要 HTTP 转发的 endpoint (chat/stream), 从 Request.url 推断 base_url


def _resolve_base_url(request) -> str:
    """
    从 FastAPI Request 推断当前 QwenPaw 主进程的 base URL。

    之前硬编码 http://localhost:8889 → 在 8899 / 其他端口 / 反代后 全部失效。
    v2.1.5 修复: 从 request.url 拿 scheme + host + port (即用户访问的真实地址)。

    同时支持显式覆盖: 环境变量 QWENPAW_BASE_URL 仍然有效 (用于反代场景)。

    v2.1.8 修复: 反代场景下 request.url.netloc 推断出的是公网入口 (例如 117.141.37.204:9999),
    但 plugin 内部调主进程 /api/agents/.../console/chat 时, 公网 nginx 不认识该路径
    → 返回 nginx 自己 404 HTML, plugin SSE 解析失败 → "All connection attempts failed"。

    修复: 优先级改成
      1) QWENPAW_BASE_URL env 强制覆盖 (跨主机反代时用户自己设)
      2) 容器内 QWENPAW_RUNNING_IN_CONTAINER=1 → 用 127.0.0.1:8889 (容器内 loopback 直连主进程,
         不走公网反代, 避开所有 nginx 路径问题)
      3) request.url 推断 (单进程/同机直连场景)
      4) 兜底 http://127.0.0.1:8889
    """
    # 1) 显式 env 强制覆盖
    explicit = os.environ.get("QWENPAW_BASE_URL")
    if explicit:
        return explicit.rstrip("/")

    # 2) 容器内运行: 直接用 loopback 127.0.0.1:PORT 调主进程, 不走任何公网反代
    if os.environ.get("QWENPAW_RUNNING_IN_CONTAINER") == "1":
        port = os.environ.get("QWENPAW_PORT", "8889")
        return f"http://127.0.0.1:{port}"

    # 3) 从 request 推断 (单进程/同机直连)
    try:
        # request.url 形如 http://192.168.1.10:8899/api/hotel-frontdesk-pawapp/...
        # 截取到 path 之前的部分就是 base
        base = f"{request.url.scheme}://{request.url.netloc}"
        return base
    except Exception as e:
        logger.warning(f"_resolve_base_url 推断失败, 用 127.0.0.1 兜底: {e}")
        return "http://127.0.0.1:8889"

HOTEL_AI_TEMPLATES = {
    "frontdesk": {
        "agent_id": "hotel-ai-frontdesk",
        "name": "酒店 AI - 前台小帮",
        "description": "酒店前台 AI 助手:接电话、查房态、改派工单、登记客人需求。",
        "icon": "🛎️",
        "color": "#1677ff",
        "tags": ["前台", "调度", "电话"],
        "scope": "接听电话、查询房态、改派工单、协调客房/工程/客人 AI",
    },
    "housekeeping": {
        "agent_id": "hotel-ai-housekeeping",
        "name": "酒店 AI - 客房管家",
        "description": "酒店客房 AI 助手:清洁派工、查脏房、跟踪房间清洁进度、申领耗材。",
        "icon": "🧹",
        "color": "#52c41a",
        "tags": ["客房", "清洁", "派工"],
        "scope": "脏房查询、清洁派工、完工汇报、耗材申领",
    },
    "engineering": {
        "agent_id": "hotel-ai-engineering",
        "name": "酒店 AI - 工程师傅",
        "description": "酒店工程 AI 助手:维修派单、设备状态跟踪、紧急维修响应。",
        "icon": "🔧",
        "color": "#fa8c16",
        "tags": ["工程", "维修", "设备"],
        "scope": "维修接单、紧急评估、派工给师傅、完工验证",
    },
    "guest-service": {
        "agent_id": "hotel-ai-guest-service",
        "name": "酒店 AI - 客人服务",
        "description": "酒店客人 AI 助手:知识问答、需求接收、投诉处理。",
        "icon": "💬",
        "color": "#722ed1",
        "tags": ["客人", "问答", "投诉"],
        "scope": "知识问答、需求接收、投诉处理、跨部门协同",
    },
}


async def _list_hotel_ai_agents() -> List[Dict[str, Any]]:
    """从 QwenPaw 内部 SDK 拉所有 hotel-ai-* agent 状态

    v2.1.5 修复: 不再用 httpx 调 localhost:8889 (硬编码端口, 8899/其他机器会全挂)
    改用 load_config().agents.profiles 读主进程内存里的 agent 注册表
    + 读 workspace/agent.json 拿 active_model / startup_status 等完整字段
    """
    try:
        from qwenpaw.config import load_config
        cfg = load_config()
        profiles = getattr(cfg.agents, "profiles", None) or {}
        if not isinstance(profiles, dict):
            return []
        out = []
        for aid, prof in profiles.items():
            if not aid.startswith("hotel-ai-"):
                continue
            if hasattr(prof, "model_dump"):
                d = prof.model_dump()
            elif isinstance(prof, dict):
                d = dict(prof)
            else:
                d = {"id": aid, "workspace_dir": str(getattr(prof, "workspace_dir", ""))}
            d.setdefault("id", aid)

            # v2.1.5: ProfileRef 只存 id/workspace_dir, 完整字段在 workspace/agent.json
            ws_dir = d.get("workspace_dir") or str(WORKSPACE_BASE / aid)
            agent_json_path = Path(ws_dir) / "agent.json"
            if agent_json_path.is_file():
                try:
                    with open(agent_json_path, "r", encoding="utf-8") as f:
                        full = json.load(f)
                    # 把 agent.json 里的关键字段合并进来
                    for k in ("name", "description", "active_model",
                              "startup_status", "enabled", "language"):
                        if k in full and full[k] is not None:
                            d[k] = full[k]
                except Exception as e:
                    logger.debug(f"_list_hotel_ai_agents 读 {agent_json_path} 失败: {e}")

            out.append(d)
        return out
    except Exception as e:
        logger.warning(f"_list_hotel_ai_agents (SDK) failed: {e}")
        return []


def _strip_scroll_headline(text: str) -> str:
    """去掉 QwenPaw scroll 策略强制输出的 ⟦ ... ⟧ headline 注释

    QwenPaw 的 SCROLL_SYSTEM_PROMPT 教 LLM 在每轮末尾加一行:
        <!-- ⟦ actual result or status of this response ⟧ -->
    这是给未来自己看的索引,不应该泄漏给前端用户。
    走 SSE 时 channel 会自动 strip,但聚合一次性返回时不会,
    这里手动过滤一下。
    """
    import re
    return re.sub(r"\s*<!--\s*⟦.*?⟧\s*-->\s*", "\n", text, flags=re.DOTALL).strip()


async def _do_chat_with_ai_assistant(agent_id: str, body: Dict[str, Any], base_url: str = "") -> Dict[str, Any]:
    """跟指定 AI 助手对话(走 qwenpaw SSE 流 /console/chat,聚合 delta 返回最后文本)"""
    message = body.get("message", "").strip()
    user_id = body.get("user_id", "hotel-pawapp-frontend")
    timeout = int(body.get("timeout", 120))  # 默认提到 120s(实测 hotel-ai-* LLM 回复 30~50s)

    # v2.1.9 session 路由表: 按 (user_id, agent_id) 维护稳定 session, 30min TTL
    force_new = bool(body.get("force_new_session", False))
    sm = chat_session_manager.get_session_manager()
    session_info = None
    if sm is not None:
        session_info = sm.get_or_create(user_id, agent_id, force_new=force_new)
        session_id = session_info["session_id"]
    else:
        # 兜底: 用前端传的 (保持向后兼容)
        session_id = body.get("session_id", "default")
    if not message:
        return {"success": False, "error": "message is required", "agent_id": agent_id}
    started = time.time()
    try:
        req_body = {
            "input": [{"content": [{"type": "text", "text": f"[Hotel PAWAPP] {message}"}]}],
            "user_id": user_id,
            "session_id": session_id,
        }
        msg_texts: Dict[str, List[str]] = {}
        msg_kind: Dict[str, str] = {}
        async with httpx.AsyncClient(timeout=timeout + 5) as c:
            async with c.stream(
                "POST",
                f"{base_url}/api/agents/{agent_id}/console/chat",
                json=req_body,
            ) as resp:
                if resp.status_code != 200:
                    return {
                        "success": False,
                        "agent_id": agent_id,
                        "status_code": resp.status_code,
                        "error": await resp.aread(),
                        "elapsed": round(time.time() - started, 2),
                    }
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        obj = json.loads(line[6:])
                    except Exception:
                        continue
                    if obj.get("object") == "message" and obj.get("type"):
                        mid = obj.get("id")
                        if mid:
                            msg_kind[mid] = obj.get("type")
                    if obj.get("type") == "text" and "text" in obj:
                        mid = obj.get("msg_id") or obj.get("id")
                        if mid:
                            msg_texts.setdefault(mid, []).append(obj["text"])
        texts: List[str] = []
        for mid, deltas in msg_texts.items():
            kind = msg_kind.get(mid)
            if kind == "reasoning":
                continue
            # QwenPaw SSE 流:每个 msg_id 的 delta 是 cumulative 的,
            # 最后一条 delta 就是完整文本(不是 .join 拼接,那样会重复).
            # 只取最后一条 delta.
            t = deltas[-1].strip() if deltas else ""
            if t:
                texts.append(t)
        # 不同 msg_id 之间也可能重复(如"思考中"的初稿 + 最终版)
        seen_sigs = set()
        deduped = []
        for t in texts:
            sig = t[:80]
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                deduped.append(t)
        reply = max(deduped, key=len) if deduped else "(无回复)"
        # 过滤 QwenPaw scroll 策略的 headline 注释 (Phase 6: 不让 ⟦ ⟧ 泄漏给前端)
        reply = _strip_scroll_headline(reply)
        elapsed = round(time.time() - started, 2)
        result = {
            "success": True,
            "agent_id": agent_id,
            "status_code": 200,
            "response": reply,
            "elapsed": elapsed,
            "timeout_used": timeout,
        }
        # v2.1.9 session 信息 (前端可显示 "X 分钟前活跃" / "新对话" 提示)
        if session_info is not None:
            result["session"] = session_info
        return result
    except httpx.TimeoutException:
        elapsed = round(time.time() - started, 2)
        result = {
            "success": False,
            "agent_id": agent_id,
            "error": f"AI 助手响应超时({timeout}s),请稍后重试",
            "elapsed": elapsed,
            "timeout": True,
        }
        if session_info is not None:
            result["session"] = session_info
        return result
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        result = {
            "success": False,
            "agent_id": agent_id,
            "error": str(e),
            "elapsed": elapsed,
        }
        if session_info is not None:
            result["session"] = session_info
        return result


async def _stream_chat_with_ai_assistant(agent_id: str, body: Dict[str, Any], base_url: str = ""):
    """流式 chat — 把 qwenpaw SSE 原始 delta 透传给前端,前端可实时显示 token
    输出格式: `data: {json}\\n\\n` SSE 帧
      - {event:meta,...}   起始
      - {object:message,type:text,text:...}  原始 delta(透传)
      - {event:done,response,elapsed,success}  结束聚合
      - {event:error,...}  异常/超时
    """
    message = body.get("message", "").strip()
    user_id = body.get("user_id", "hotel-pawapp-frontend")
    timeout = int(body.get("timeout", 120))
    started = time.time()
    if not message:
        yield f"data: {json.dumps({'event':'error','error':'message is required'})}\n\n"
        return

    # v2.1.9 session 路由表: 按 (user_id, agent_id) 维护稳定 session, 30min TTL
    force_new = bool(body.get("force_new_session", False))
    sm = chat_session_manager.get_session_manager()
    session_info = None
    if sm is not None:
        session_info = sm.get_or_create(user_id, agent_id, force_new=force_new)
        session_id = session_info["session_id"]
    else:
        session_id = body.get("session_id", "default")

    # meta (v2.1.9 增加 session 信息: created_new/idle 提示)
    meta_obj: Dict[str, Any] = {
        "event": "meta",
        "agent_id": agent_id,
        "started_at": started,
    }
    if session_info is not None:
        meta_obj["session"] = {
            "session_id": session_info["session_id"],
            "created_new": session_info["created_new"],
            "idle_seconds": 0,  # 本次是新建/复用, idle=0
            "ttl_seconds": session_info["ttl_seconds"],
        }
    yield f"data: {json.dumps(meta_obj)}\n\n"

    req_body = {
        "input": [{"content": [{"type": "text", "text": f"[Hotel PAWAPP] {message}"}]}],
        "user_id": user_id,
        "session_id": session_id,
    }
    msg_texts: Dict[str, List[str]] = {}
    msg_kind: Dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=timeout + 5) as c:
            async with c.stream(
                "POST",
                f"{base_url}/api/agents/{agent_id}/console/chat",
                json=req_body,
            ) as resp:
                if resp.status_code != 200:
                    err_body = await resp.aread()
                    yield f"data: {json.dumps({'event':'error','status_code':resp.status_code,'error':err_body.decode('utf-8','ignore')[:500]})}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    # 透传原始 delta(前端可直接渲染)
                    yield f"data: {raw}\n\n"
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("object") == "message" and obj.get("type"):
                        mid = obj.get("id")
                        if mid:
                            msg_kind[mid] = obj.get("type")
                    if obj.get("type") == "text" and "text" in obj:
                        mid = obj.get("msg_id") or obj.get("id")
                        if mid:
                            msg_texts.setdefault(mid, []).append(obj["text"])
        texts: List[str] = []
        for mid, deltas in msg_texts.items():
            kind = msg_kind.get(mid)
            if kind == "reasoning":
                continue
            # QwenPaw SSE 流:每个 msg_id 的 delta 是 cumulative 的,
            # 最后一条 delta 就是完整文本(不是 .join 拼接,那样会重复).
            # 只取最后一条 delta.
            t = deltas[-1].strip() if deltas else ""
            if t:
                texts.append(t)
        # 不同 msg_id 之间也可能重复(如"思考中"的初稿 + 最终版)
        seen_sigs = set()
        deduped = []
        for t in texts:
            sig = t[:80]
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                deduped.append(t)
        reply = max(deduped, key=len) if deduped else "(无回复)"
        # 过滤 QwenPaw scroll 策略的 headline 注释 (Phase 6: 不让 ⟦ ⟧ 泄漏给前端)
        reply = _strip_scroll_headline(reply)
        elapsed = round(time.time() - started, 2)
        yield f"data: {json.dumps({'event':'done','response':reply,'elapsed':elapsed,'success':True})}\n\n"
    except httpx.TimeoutException:
        elapsed = round(time.time() - started, 2)
        yield f"data: {json.dumps({'event':'error','error':f'AI 助手响应超时({timeout}s),请稍后重试或换个问题','elapsed':elapsed,'timeout':timeout})}\n\n"
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        yield f"data: {json.dumps({'event':'error','error':str(e),'elapsed':elapsed})}\n\n"


def register_routes(app) -> None:
    """注册 AI 助手路由到 PawApp SDK (router mode)"""
    from qwenpaw.pawapp import get_ctx

    @router.get("/ai/assistants")
    async def list_ai_assistants(request: Request, ctx=Depends(get_ctx)):
        """列出所有酒店 AI 助手(含自检状态)"""
        agents = await _list_hotel_ai_agents()
        base_url = _resolve_base_url(request)
        results: List[Dict[str, Any]] = []
        for a in agents:
            aid = a.get("id", "")
            meta = None
            for t in HOTEL_AI_TEMPLATES.values():
                if t["agent_id"] == aid:
                    meta = t
                    break
            ws_path = WORKSPACE_BASE / aid
            endpoint_ok = False
            endpoint_err = ""
            try:
                async with httpx.AsyncClient(timeout=5) as _c:
                    _r = await _c.get(f"{base_url}/api/agents/{aid}/agent-status")
                    endpoint_ok = _r.status_code == 200
                    if not endpoint_ok:
                        endpoint_err = f"HTTP {_r.status_code}"
            except Exception as _e:
                endpoint_err = f"{type(_e).__name__}: {_e}"

            checks = {
                "agent_registered": bool(a),
                "workspace_exists": ws_path.exists(),
                "has_profile": (ws_path / "PROFILE.md").exists() if ws_path.exists() else False,
                "has_soul": (ws_path / "SOUL.md").exists() if ws_path.exists() else False,
                "has_active_model": bool(a.get("active_model")),
                "endpoint_reachable": endpoint_ok,
            }
            checks_pass = sum(1 for v in checks.values() if v)
            results.append({
                "agent_id": aid,
                "name": a.get("name", aid),
                "description": a.get("description", ""),
                "icon": (meta or {}).get("icon", "🤖"),
                "color": (meta or {}).get("color", "#8c8c8c"),
                "tags": (meta or {}).get("tags", []),
                "scope": (meta or {}).get("scope", ""),
                "workspace_dir": str(ws_path),
                "active_model": a.get("active_model"),
                "startup_status": a.get("startup_status"),
                "enabled": a.get("enabled", False),
                "checks": checks,
                "checks_pass": checks_pass,
                "checks_total": len(checks),
                "endpoint_err": endpoint_err,
                "healthy": checks_pass >= 4,
                "template_key": next((k for k, v in HOTEL_AI_TEMPLATES.items() if v["agent_id"] == aid), None),
            })
        return {
            "assistants": results,
            "templates": list(HOTEL_AI_TEMPLATES.keys()),
            "total": len(results),
        }

    @router.get("/ai/assistants/{agent_id}/status")
    async def ai_assistant_status(ctx=Depends(get_ctx), agent_id: str = ""):
        """单个 AI 助手自检详情"""
        ws_path = WORKSPACE_BASE / agent_id
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{base_url}/api/agents/{agent_id}")
                agent_data = r.json() if r.status_code == 200 else None
        except Exception as e:
            agent_data = None
            logger.warning(f"get agent {agent_id} failed: {e}")

        ws_ok = ws_path.exists()
        files: Dict[str, bool] = {}
        for fname in ["PROFILE.md", "SOUL.md", "MEMORY.md", "AGENTS.md", "HEARTBEAT.md", "agent.json", "skill.json"]:
            files[fname] = (ws_path / fname).exists() if ws_ok else False

        return {
            "agent_id": agent_id,
            "workspace_exists": ws_ok,
            "workspace_dir": str(ws_path),
            "registered": agent_data is not None,
            "startup_status": (agent_data or {}).get("startup_status"),
            "enabled": (agent_data or {}).get("enabled"),
            "active_model": (agent_data or {}).get("active_model"),
            "files": files,
            "files_pass": sum(1 for v in files.values() if v),
            "files_total": len(files),
            "healthy": files["PROFILE.md"] and files["SOUL.md"] and (agent_data or {}).get("active_model") is not None,
            "checked_at": now(),
        }

    @router.post("/ai/assistants")
    async def create_ai_assistant(
        ctx=Depends(get_ctx),
        body: Dict[str, Any] = Body(default_factory=dict),
    ):
        """新建一个酒店 AI 助手(基于模板或自定义)"""
        template_key = body.get("template_key", "custom")
        template = HOTEL_AI_TEMPLATES.get(template_key) if template_key != "custom" else None
        agent_id = body.get("agent_id") or (template["agent_id"] if template else f"hotel-ai-{uuid.uuid4().hex[:6]}")
        name = body.get("name") or (template["name"] if template else f"酒店 AI - {agent_id}")
        description = body.get("description") or (template["description"] if template else "")

        try:
            proc = subprocess.run(
                ["/app/venv/bin/qwenpaw", "agents", "create",
                 "--name", name, "--agent-id", agent_id,
                 "--description", description,
                 "--template", "default"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                return {"success": False, "error": f"qwenpaw create failed: {proc.stderr}", "agent_id": agent_id}
        except Exception as e:
            return {"success": False, "error": str(e), "agent_id": agent_id}

        ws_path = WORKSPACE_BASE / agent_id
        if template:
            profile_md = f"""# {name} ({agent_id})

## 身份
- **名字:** {name}
- **定位:** {description}
- **Agent ID:** `{agent_id}`

## 核心职责
{template['scope']}

## 协同伙伴(酒店 AI 矩阵)
- ↔ `hotel-ai-frontdesk` 前台小帮
- ↔ `hotel-ai-housekeeping` 客房管家
- ↔ `hotel-ai-engineering` 工程师傅
- ↔ `hotel-ai-guest-service` 客人服务

## 协同规则
1. 协同用 `chat_with_agent`,前缀 `[Agent {agent_id} requesting]`
2. 能直接调 API 完成的事直接调,跨部门才协同
3. 不抢活,自己职责范围内自己处理
4. 协同时必须等回复,给用户闭环

## 备注
本助手基于模板 `{template_key}` 创建,可手动编辑 PROFILE.md / SOUL.md 调优。
"""
            soul_md = f"""---
summary: "{name} 工作区"
read_when:
  - 启动协同任务
---

# {name} — 核心原则

## 我是谁
{description}

## 协同规则
1. **协同用 chat_with_agent**:发起协同时前缀 `[Agent {agent_id} requesting]`
2. **不抢活**:职责范围内自己处理,跨部门才协同
3. **要闭环**:协同必须等回复,确认完成后才能回用户

## API 直调 vs 协同
- 业务数据查询/修改 → 直接调 `/api/hotel/*`
- 跨部门事务 → chat_with_agent 其他 hotel-ai-* agent

## 不做的事
- 不擅自处理超出职责的事务
- 不忽略用户/客人的紧急诉求
- 不让请求超过 30 秒无回复
"""
            (ws_path / "PROFILE.md").write_text(profile_md, encoding="utf-8")
            (ws_path / "SOUL.md").write_text(soul_md, encoding="utf-8")

        agent_json_path = ws_path / "agent.json"
        if agent_json_path.exists():
            try:
                with open(agent_json_path) as f:
                    aj = json.load(f)
                if "active_model" not in aj:
                    aj["active_model"] = {"provider_id": "aliyun-codingplan", "model": "qwen3.7-plus"}
                with open(agent_json_path, "w") as f:
                    json.dump(aj, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"add active_model failed: {e}")

        return {
            "success": True,
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "workspace_dir": str(ws_path),
            "template_key": template_key,
            "created_at": now(),
            "next_step": "等待 qwenpaw reload config(约 5 秒),然后调 /ai/assistants/{agent_id}/status 自检",
        }

    @router.delete("/ai/assistants/{agent_id}")
    async def delete_ai_assistant(ctx=Depends(get_ctx), agent_id: str = ""):
        """删除 AI 助手(调 qwenpaw CLI)"""
        try:
            proc = subprocess.run(
                ["/app/venv/bin/qwenpaw", "agents", "delete", agent_id],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                return {"success": False, "error": proc.stderr}
            return {"success": True, "agent_id": agent_id, "deleted_at": now()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @router.post("/ai/assistants/{agent_id}/chat")
    async def chat_with_assistant(
        request: Request,  # v2.1.5: 用于推断 base_url (避开硬编码 8889)
        response: Response,  # v2.1.10: 用于回写 Set-Cookie (浏览器级稳定 user_id)
        ctx=Depends(get_ctx),
        agent_id: str = "",
        payload: dict = Body(default_factory=dict),
    ):
        """对某个 hotel-ai-* agent 发起协同对话(走 SSE /console/chat,聚合返回)

        v2.1.10 用户隔离:
          - 优先看 X-User-Id / X-Wecom-* header (外部集成方传)
          - 否则读 cookie hotel_uid (浏览器级稳定身份)
          - 否则读 body user_id (向后兼容)
          - 兜底: 后端种 cookie (Set-Cookie: hotel_uid=...) 给浏览器一个稳定 UUID
        """
        from ..user_identity import resolve_user_id_with_cookie, build_set_cookie_header

        identity = resolve_user_id_with_cookie(request, payload)
        if identity.get("set_cookie"):
            response.headers["Set-Cookie"] = build_set_cookie_header(identity["set_cookie"])
            payload = dict(payload)
            payload["user_id"] = identity["user_id"]
        elif identity["source"] not in ("anon",):
            payload = dict(payload)
            payload["user_id"] = identity["user_id"]
        # anon 不写 user_id 让后端兜底, 但 set_cookie 已标记, 上面会处理

        result = await _do_chat_with_ai_assistant(agent_id, payload, _resolve_base_url(request))
        # 在响应里加 identity 信息 (调试 + 前端可展示)
        if isinstance(result, dict):
            result["identity"] = {
                "user_id": identity["user_id"],
                "source": identity["source"],
                "cookie_set": bool(identity.get("set_cookie")),
            }
        return result

    @router.post("/ai/assistants/{agent_id}/chat/stream")
    async def stream_chat_with_assistant(
        request: Request,  # v2.1.5
        ctx=Depends(get_ctx),
        agent_id: str = "",
        payload: dict = Body(default_factory=dict),
    ):
        """流式 chat — 把 qwenpaw SSE delta 透传给前端,前端可实时渲染 token

        v2.1.10: 流式也走 user_identity 解析 user_id
        """
        from ..user_identity import resolve_user_id_with_cookie, build_set_cookie_header

        identity = resolve_user_id_with_cookie(request, payload)
        if identity["source"] != "anon":
            payload = dict(payload)
            payload["user_id"] = identity["user_id"]

        # 流式响应要回写 Set-Cookie, 但 SSE 不能直接 set headers 在 generator 里
        # 用 StreamingResponse + 收集 headers 的方式
        extra_headers = {}
        if identity.get("set_cookie"):
            extra_headers["Set-Cookie"] = build_set_cookie_header(identity["set_cookie"])

        async def _gen():
            async for chunk in _stream_chat_with_ai_assistant(
                agent_id, payload, _resolve_base_url(request)
            ):
                yield chunk
            # 流末尾追加 identity 信息 (前端可解析)
            yield f"data: {json.dumps({'event':'identity','user_id':identity['user_id'],'source':identity['source'],'cookie_set':bool(identity.get('set_cookie'))})}\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                **extra_headers,
            },
        )

    @router.get("/ai/assistants/{agent_id}/chats")
    async def list_ai_chats(request: Request, ctx=Depends(get_ctx), agent_id: str = ""):
        """列出指定 AI 助手的对话历史"""
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{_resolve_base_url(request)}/api/agents/{agent_id}/chats")
                if r.status_code == 200:
                    return r.json()
                return {"chats": [], "error": f"status {r.status_code}"}
        except Exception as e:
            return {"chats": [], "error": str(e)}

    @router.post("/ai/assistants/{agent_id}/selfcheck")
    async def selfcheck_ai_assistant(request: Request, ctx=Depends(get_ctx), agent_id: str = ""):
        """自检:发一条"你好,请自我介绍"测试消息"""
        return await _do_chat_with_ai_assistant(agent_id, {
            "message": "你好,请用一句话自我介绍(包括你的名字、职责)",
            "session_id": f"hotel-ai:selfcheck:{agent_id}",
        }, _resolve_base_url(request))

    # ------------------------------------------------------------------
    # v2.1.9 Chat Session Manager 管理 endpoint
    # ------------------------------------------------------------------

    @router.get("/ai/sessions")
    async def list_user_sessions(
        request: Request,
        response: Response,
        ctx=Depends(get_ctx),
    ):
        """列出指定 user_id 的所有 agent session 状态

        v2.1.10: 不传 user_id 也行, 自动按当前浏览器/header 解析
        Query: ?user_id=xxx (可选; 不传则用当前请求身份)
        """
        from ..user_identity import resolve_user_id_with_cookie, build_set_cookie_header

        explicit_uid = request.query_params.get("user_id", "").strip()
        if explicit_uid:
            user_id = explicit_uid
            cookie_set = False
        else:
            identity = resolve_user_id_with_cookie(request, {})
            user_id = identity["user_id"]
            cookie_set = bool(identity.get("set_cookie"))
            if cookie_set:
                response.headers["Set-Cookie"] = build_set_cookie_header(identity["set_cookie"])

        if not user_id:
            raise HTTPException(400, "user_id is required (explicit or from cookie)")
        sm = chat_session_manager.get_session_manager()
        if sm is None:
            return {
                "user_id": user_id,
                "sessions": [],
                "ttl_seconds": 0,
                "error": "session manager not initialized",
            }
        return {
            "user_id": user_id,
            "sessions": sm.list_for_user(user_id),
            "ttl_seconds": sm._ttl,
            "stats": sm.stats(),
        }

    @router.post("/ai/sessions/reset")
    async def reset_user_session(request: Request, ctx=Depends(get_ctx)):
        """重置指定用户/agent 的 session, 下次发消息将开新 session

        Body: {"user_id": "xxx", "agent_id": "yyy" | null}  // agent_id=null 表示全部
        Response: {"reset_count": int, "user_id": "...", "agent_id": "..."}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id = (body.get("user_id") or "").strip()
        if not user_id:
            raise HTTPException(400, "user_id is required")
        agent_id = body.get("agent_id")  # None = 全部
        if agent_id is not None:
            agent_id = agent_id.strip() or None
        sm = chat_session_manager.get_session_manager()
        if sm is None:
            raise HTTPException(503, "session manager not initialized")
        n = sm.reset(user_id, agent_id)
        return {"reset_count": n, "user_id": user_id, "agent_id": agent_id}

    @router.get("/ai/sessions/stats")
    async def session_stats(request: Request, ctx=Depends(get_ctx)):
        """全局 session 统计 (调试/管理用)"""
        sm = chat_session_manager.get_session_manager()
        if sm is None:
            raise HTTPException(503, "session manager not initialized")
        return sm.stats()

    app.include_router(router)
    logger.info("[routes/ai_assistants] 已注册 7 个 AI 助手路由 + 3 个 session 管理路由")
