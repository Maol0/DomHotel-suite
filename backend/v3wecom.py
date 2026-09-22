# -*- coding: utf-8 -*-
"""v3 企微接口预留

占位模块，等用户提供 corp_id + secret + 各表 doc_id 后激活。
"""
from __future__ import annotations
import os
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 企微配置（从环境变量读取）
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_SECRET = os.environ.get("WECOM_SECRET", "")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID", "")

# 智能表格 doc_id（待配置）
WECOM_DOCS = {
    "rooms": os.environ.get("WECOM_DOC_ROOMS", ""),
    "work_orders": os.environ.get("WECOM_DOC_WORK_ORDERS", ""),
    "staff": os.environ.get("WECOM_DOC_STAFF", ""),
    "pending_actions": os.environ.get("WECOM_DOC_PENDING", ""),
}


def is_configured() -> bool:
    """检查企微是否已配置"""
    return bool(WECOM_CORP_ID and WECOM_SECRET)


def get_config_status() -> Dict[str, Any]:
    """获取企微配置状态"""
    return {
        "configured": is_configured(),
        "corp_id_set": bool(WECOM_CORP_ID),
        "secret_set": bool(WECOM_SECRET),
        "agent_id_set": bool(WECOM_AGENT_ID),
        "docs_configured": {k: bool(v) for k, v in WECOM_DOCS.items()},
        "message": "企微接口已预留，等待配置 corp_id + secret + 各表 doc_id" if not is_configured() else "企微已配置"
    }


def sync_to_wecom(table: str, record: Dict[str, Any], action: str = "create") -> Dict[str, Any]:
    """同步数据到企微智能表格（占位）"""
    if not is_configured():
        return {"ok": False, "error": "企微未配置", "config_status": get_config_status()}
    
    doc_id = WECOM_DOCS.get(table)
    if not doc_id:
        return {"ok": False, "error": f"表 {table} 的 doc_id 未配置"}
    
    # TODO: 实现企微 API 调用
    logger.info(f"[wecom] sync_to_wecom: table={table}, action={action}, record_id={record.get('id', '?')}")
    return {"ok": True, "status": "placeholder", "message": "企微接口占位，待实现"}


def pull_from_wecom(table: str, record_id: str = None) -> Dict[str, Any]:
    """从企微智能表格拉取数据（占位）"""
    if not is_configured():
        return {"ok": False, "error": "企微未配置", "config_status": get_config_status()}
    
    doc_id = WECOM_DOCS.get(table)
    if not doc_id:
        return {"ok": False, "error": f"表 {table} 的 doc_id 未配置"}
    
    # TODO: 实现企微 API 调用
    logger.info(f"[wecom] pull_from_wecom: table={table}, record_id={record_id}")
    return {"ok": True, "status": "placeholder", "message": "企微接口占位，待实现"}


def send_notification(user_id: str, message: str, msg_type: str = "text") -> Dict[str, Any]:
    """发送企微消息通知（占位）"""
    if not is_configured():
        return {"ok": False, "error": "企微未配置"}
    
    # TODO: 实现企微消息发送
    logger.info(f"[wecom] send_notification: user={user_id}, type={msg_type}")
    return {"ok": True, "status": "placeholder", "message": "企微通知占位，待实现"}
