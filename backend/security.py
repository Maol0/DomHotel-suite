"""DomHotel 安全工具：内部 API Key、客人 HMAC 确认令牌、常量时间比较

安全约定：
- secret/token 不进仓库，由 deploy.sh 在 POD 内生成到 secret 文件
- 技能脚本经 X-Internal-Key 获得内部通道，错误/缺失 key 按匿名处理
- 客人确认链接使用 HMAC(ticketId) 签名 ct 参数，作为渠道无关凭证
"""

import hmac
import hashlib
import secrets
import os
from pathlib import Path
from typing import Optional


# 内部 API Key 文件路径（POD 内生成，不进仓库）
# v1.4.0-persistent: 优先读取持久化目录
_INTERNAL_KEY_PATHS = [
    Path("/run/secrets/domhotel_internal_key"),
    Path("/app/working/dompaw-data-backup/.domhotel_internal_key"),
    Path("/app/data/v20/.domhotel_internal_key"),
    Path("/app/working/plugins/domhotel-suite/.secret/internal_api_key"),
]


def _load_internal_api_key() -> str:
    """读取内部 API Key；没有则自动生成一个（仅适用于单 POD/开发环境）"""
    for p in _INTERNAL_KEY_PATHS:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    # 自动生成并保存到数据目录（优先持久化目录）
    auto_path = _INTERNAL_KEY_PATHS[1] if Path("/app/working/dompaw-data-backup").exists() else _INTERNAL_KEY_PATHS[2]
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    auto_path.write_text(key, encoding="utf-8")
    os.chmod(auto_path, 0o600)
    return key


INTERNAL_API_KEY = _load_internal_api_key()


def constant_time_equals(a: str, b: str) -> bool:
    """常量时间字符串比较，避免时序攻击"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_internal_key(header_value: Optional[str]) -> bool:
    """验证 X-Internal-Key"""
    if not header_value:
        return False
    return constant_time_equals(header_value, INTERNAL_API_KEY)


def sign_guest_confirmation(ticket_id: str) -> str:
    """为客人确认链接生成 HMAC 签名 ct 参数"""
    return hmac.new(
        INTERNAL_API_KEY.encode("utf-8"),
        ticket_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def verify_guest_confirmation(ticket_id: str, ct: Optional[str]) -> bool:
    """验证客人确认链接的 ct 签名"""
    if not ct:
        return False
    expected = sign_guest_confirmation(ticket_id)
    return constant_time_equals(ct, expected)
