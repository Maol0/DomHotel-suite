"""企微应用群聊 appchat 工具：建群、发 textcard 卡片

安全：chatid 由运行时配置读取，不硬编码；缺失时静默跳过。
"""

from typing import Any, Dict, List, Optional
import httpx
import logging

from .wecom_sync import _get_access_token, WECOM_BASE_URL
from .wecom_sync import get_kf_setting

logger = logging.getLogger(__name__)


# 部门 → appchat chatid 映射（运行时配置，不进仓库）
DEPT_APPCHAT_CONFIG_KEY = "DEPT_APPCHAT_CHATIDS"


def _load_dept_chatids() -> Dict[str, str]:
    """读取运行时配置中的部门群 chatid 映射（JSON 字符串）"""
    raw = get_kf_setting(DEPT_APPCHAT_CONFIG_KEY, "{}")
    try:
        return {k.strip(): v.strip() for k, v in (raw or "").items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def get_chatid(dept_en: str) -> Optional[str]:
    """按部门英文名取 chatid"""
    return _load_dept_chatids().get(dept_en)


async def send_appchat_textcard(chatid: str, title: str, description: str, url: str = "", btntxt: str = "查看详情") -> Dict[str, Any]:
    """向指定 appchat 群发送 textcard 卡片"""
    if not chatid:
        return {"ok": False, "skipped": True, "reason": "chatid 为空"}
    try:
        token = await _get_access_token()
    except Exception as e:
        logger.warning("[appchat] 获取 access_token 失败: %s", e)
        return {"ok": False, "reason": str(e)}

    body = {
        "chatid": chatid,
        "msgtype": "textcard",
        "textcard": {
            "title": title,
            "description": description,
            "url": url or "https://work.weixin.qq.com",
            "btntxt": btntxt,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{WECOM_BASE_URL}/cgi-bin/appchat/send?access_token={token}", json=body)
            data = r.json()
    except Exception as e:
        logger.warning("[appchat] 发送群卡片请求失败: %s", e)
        return {"ok": False, "reason": str(e)}

    if data.get("errcode", 0) != 0:
        logger.warning("[appchat] 发送群卡片失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
    logger.info("[appchat] 已向 %s 发送卡片: %s", chatid[:16], title)
    return {"ok": True, "chatid": chatid}


async def create_appchat(name: str, owner: str, userlist: List[str], dept_en: str = "") -> Optional[str]:
    """创建应用群聊并返回 chatid；可选把结果写回运行时配置"""
    try:
        token = await _get_access_token()
    except Exception as e:
        logger.warning("[appchat] 获取 access_token 失败: %s", e)
        return None

    body = {"name": name, "owner": owner, "userlist": userlist, "chatid_type": "group"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{WECOM_BASE_URL}/cgi-bin/appchat/create?access_token={token}", json=body)
            data = r.json()
    except Exception as e:
        logger.warning("[appchat] 建群请求失败: %s", e)
        return None

    if data.get("errcode", 0) != 0:
        logger.warning("[appchat] 建群失败: %s", data)
        return None
    chatid = data.get("chatid")
    logger.info("[appchat] 已建群 %s: %s", name, chatid)
    return chatid
