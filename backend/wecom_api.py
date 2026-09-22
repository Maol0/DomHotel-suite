# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 企微智能表格 API 客户端

Phase 5 实装 (2026-08-04):
  - access_token 缓存（7000s TTL）
  - 智能表格 add_records / update_records / delete_records
  - 与 wecom_sync 协同：sync 负责调度（队列/重试），本模块负责 HTTP 调用

注意：本模块是低层 HTTP 客户端，业务层应通过 wecom_sync.sync_now() 调用，
     不要直接 import 此模块以避免绕过重试逻辑。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_SECRET = os.environ.get(
    "WECOM_AGENT_SECRET",
    "",
)
WECOM_BASE_URL = "https://qyapi.weixin.qq.com"

# access_token 缓存（与 wecom_smartsheet MCP 共用一套凭证）
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0}
_token_lock = asyncio.Lock()


async def get_access_token() -> str:
    """获取/刷新 access_token，缓存 7000s（官方 7200s 留余量）

    Raises:
        RuntimeError: 企微返回非 0 errcode 时
    """
    async with _token_lock:
        if _token_cache["token"] and _token_cache["expires_at"] > time.time() + 60:
            return _token_cache["token"]

        # v1.3.0: 凭证走 wecom_sync 运行时配置层 (runtime json > env > 内置默认),
        # 与智能体配置工具 / POST /wecom/config 写入的值保持一致
        from . import wecom_sync
        url = f"{WECOM_BASE_URL}/cgi-bin/gettoken"
        params = {"corpid": wecom_sync.get_corp_id(), "corpsecret": wecom_sync.get_agent_secret()}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            data = r.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: {data}")

        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + 7000
        logger.info("[wecom_api] access_token refreshed")
        return _token_cache["token"]


async def add_records(docid: str, sheet_id: str, records: list[dict]) -> dict:
    """智能表格 add_records

    Args:
        docid: 文档 ID
        sheet_id: 子表 ID (v1.2.0: 官方文档必填参数, 之前版本漏传会报参数错误)
        records: [{"values": {...}}, ...]  (新增时不传 record_id, 由企微生成)
    Returns:
        企微响应 dict
    """
    token = await get_access_token()
    url = f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/add_records"
    payload = {
        "docid": docid,
        "sheet_id": sheet_id,
        "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
        "records": records,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{url}?access_token={token}", json=payload)
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"add_records failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    return data


async def update_records(docid: str, sheet_id: str, records: list[dict]) -> dict:
    """智能表格 update_records（Phase 5 当前未直接使用, 预留）"""
    token = await get_access_token()
    url = f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/update_records"
    payload = {"docid": docid, "sheet_id": sheet_id, "records": records}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{url}?access_token={token}", json=payload)
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"update_records failed: {data.get('errmsg')}")
    return data


async def delete_records(docid: str, sheet_id: str, record_ids: list[str]) -> dict:
    """智能表格 delete_records（Phase 5 当前未直接使用, 预留）"""
    token = await get_access_token()
    url = f"{WECOM_BASE_URL}/cgi-bin/wedoc/smartsheet/delete_records"
    payload = {"docid": docid, "sheet_id": sheet_id, "record_ids": record_ids}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{url}?access_token={token}", json=payload)
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"delete_records failed: {data.get('errmsg')}")
    return data


def reset_token_cache() -> None:
    """测试用：清空 token 缓存"""
    _token_cache["token"] = ""
    _token_cache["expires_at"] = 0
