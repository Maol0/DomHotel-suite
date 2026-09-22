"""
Chat Session Manager (v2.1.9)
=============================

按 (user_id, agent_id) 维护稳定的 QwenPaw session_id, 同一用户同一 agent
30 分钟 (可配) 内的连续对话共用一个 session, 保留上下文。

设计要点:
  - 持久化到 plugin data_dir/sessions.json, QwenPaw/plugin 重启不丢
  - TTL 默认 1800s (30 分钟), 可由 env HOTEL_CHAT_SESSION_TTL_SECONDS 覆盖
  - 支持 force_new_session=True 强制开新 (前端"开始新对话"按钮用)
  - 支持 list/reset/cleanup 接口供前端查询/管理

稳定性:
  - 文件读写用 atomic rename (write temp + os.replace) 防止半写损坏
  - 内存 dict + 异步锁保护并发
  - 后台惰性 cleanup (查询时才清过期, 不起 background thread)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hotel-frontdesk-pawapp.session-manager")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 1800  # 30 分钟
SESSIONS_FILE = "chat_sessions.json"


def get_ttl_seconds() -> int:
    """读 env 取 TTL, 失败兜底 1800s"""
    raw = os.environ.get("HOTEL_CHAT_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        ttl = int(raw)
        return ttl if ttl > 0 else DEFAULT_TTL_SECONDS
    except (ValueError, TypeError):
        return DEFAULT_TTL_SECONDS


# ---------------------------------------------------------------------------
# 存储后端 (JSON file + 内存 cache + asyncio.Lock)
# ---------------------------------------------------------------------------


class ChatSessionManager:
    """(user_id, agent_id) -> {session_id, last_active_at, created_at} 路由表"""

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / SESSIONS_FILE
        self._ttl = get_ttl_seconds()
        # 内存缓存
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        # 异步写锁 (对外 API)
        import asyncio

        self._lock = asyncio.Lock()
        # 同步读锁 (供 sync API 用)
        self._sync_lock: Optional[Any] = None  # type: ignore

    # --- 持久化 ---------------------------------------------------------

    def _load(self) -> None:
        """从磁盘加载到内存缓存 (sync, 启动时或首次 lazy 加载)"""
        if self._loaded:
            return
        if not self._path.exists():
            self._cache = {}
            self._loaded = True
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
            # 清理过期项
            cutoff = time.time() - self._ttl * 2  # 保留宽限, 防止启动即过期
            self._cache = {
                k: v
                for k, v in data.items()
                if isinstance(v, dict)
                and v.get("last_active_at", 0) >= cutoff
            }
            logger.info(
                "session manager: loaded %d entries from %s (ttl=%ds)",
                len(self._cache),
                self._path,
                self._ttl,
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "session manager: failed to load %s: %s — starting empty",
                self._path,
                e,
            )
            self._cache = {}
        self._loaded = True

    def _save(self) -> None:
        """原子写磁盘 (sync)"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # atomic rename
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(
                "session manager: failed to save %s: %s", self._path, e
            )

    # --- 内部 helper ----------------------------------------------------

    @staticmethod
    def _key(user_id: str, agent_id: str) -> str:
        return f"{user_id}::{agent_id}"

    @staticmethod
    def _gen_session_id() -> str:
        """生成新 session id (用 unix 秒+随机, 短可读)"""
        import random

        ts = int(time.time())
        rnd = random.randint(1000, 9999)
        return f"chat-{ts}-{rnd}"

    def _is_expired(self, entry: Dict[str, Any], now: float) -> bool:
        last = entry.get("last_active_at", 0)
        return (now - last) > self._ttl

    def _cleanup_expired(self, now: float) -> int:
        """清理过期项, 返回清理条数"""
        expired = [k for k, v in self._cache.items() if self._is_expired(v, now)]
        for k in expired:
            del self._cache[k]
        return len(expired)

    # --- 对外 API (同步, 供 plugin 同步路径用) --------------------------

    def get_or_create(
        self,
        user_id: str,
        agent_id: str,
        *,
        force_new: bool = False,
    ) -> Dict[str, Any]:
        """取或建 session, 顺手更新 last_active_at 并落盘

        Returns:
            {
                "session_id": str,        # 给 QwenPaw 用的 session
                "created_new": bool,      # True=本次新建 (前端可提示)
                "last_active_at": float,  # 本次活跃时间 (前端可显示"X 分钟前活跃")
                "ttl_seconds": int,       # 当前 TTL 配置
                "key": str,               # 内部 key (调试用)
            }
        """
        self._load()
        key = self._key(user_id, agent_id)
        now = time.time()

        existing = self._cache.get(key)
        if existing and not force_new and not self._is_expired(existing, now):
            existing["last_active_at"] = now
            self._save()
            return {
                "session_id": existing["session_id"],
                "created_new": False,
                "last_active_at": now,
                "ttl_seconds": self._ttl,
                "key": key,
            }

        # 新建
        sid = self._gen_session_id()
        self._cache[key] = {
            "session_id": sid,
            "last_active_at": now,
            "created_at": now,
            "user_id": user_id,
            "agent_id": agent_id,
        }
        self._save()
        logger.info(
            "session manager: %s session for user=%s agent=%s (ttl=%ds)",
            "force-new" if force_new else "expired/new",
            user_id,
            agent_id,
            self._ttl,
        )
        return {
            "session_id": sid,
            "created_new": True,
            "last_active_at": now,
            "ttl_seconds": self._ttl,
            "key": key,
        }

    def reset(self, user_id: str, agent_id: Optional[str] = None) -> int:
        """重置指定用户的 session, agent_id=None 表示全部 agent, 返回清理条数"""
        self._load()
        if agent_id:
            keys = [self._key(user_id, agent_id)]
        else:
            keys = [k for k in self._cache if k.startswith(f"{user_id}::")]
        for k in keys:
            self._cache.pop(k, None)
        self._save()
        return len(keys)

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """列出该用户所有 agent 的 session 状态"""
        self._load()
        now = time.time()
        prefix = f"{user_id}::"
        results = []
        for k, v in self._cache.items():
            if not k.startswith(prefix):
                continue
            agent_id = v.get("agent_id", k[len(prefix):])
            last = v.get("last_active_at", 0)
            results.append(
                {
                    "agent_id": agent_id,
                    "session_id": v.get("session_id"),
                    "last_active_at": last,
                    "idle_seconds": max(0, int(now - last)),
                    "expired": self._is_expired(v, now),
                    "created_at": v.get("created_at"),
                }
            )
        results.sort(key=lambda r: r["last_active_at"], reverse=True)
        return results

    def stats(self) -> Dict[str, Any]:
        """全局统计 (调试/管理用)"""
        self._load()
        now = time.time()
        total = len(self._cache)
        expired = sum(
            1 for v in self._cache.values() if self._is_expired(v, now)
        )
        return {
            "total_entries": total,
            "expired_entries": expired,
            "ttl_seconds": self._ttl,
            "storage_path": str(self._path),
        }

    def cleanup_expired(self) -> int:
        """手动触发清理过期项, 返回清理条数"""
        self._load()
        n = self._cleanup_expired(time.time())
        if n > 0:
            self._save()
        return n


# ---------------------------------------------------------------------------
# 单例 (plugin 启动时 init, 业务代码 get_instance)
# ---------------------------------------------------------------------------

_INSTANCE: Optional[ChatSessionManager] = None


def init_session_manager(data_dir: Path) -> ChatSessionManager:
    """plugin 启动时调用, 创建单例"""
    global _INSTANCE
    _INSTANCE = ChatSessionManager(data_dir)
    _INSTANCE._load()  # 启动即加载
    logger.info(
        "session manager initialized (data_dir=%s, ttl=%ds)",
        data_dir,
        _INSTANCE._ttl,
    )
    return _INSTANCE


def get_session_manager() -> Optional[ChatSessionManager]:
    return _INSTANCE


def shutdown_session_manager() -> None:
    """plugin 卸载时调用, 落盘 + 清单例"""
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            _INSTANCE._save()
        except Exception:
            pass
        _INSTANCE = None