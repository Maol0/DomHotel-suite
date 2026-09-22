"""数据同步元信息 — v2.1.18 双流程 + 企微优先架构

为所有数据项加统一的元信息追踪, 解决:
  - 手动 vs AI 操作区分 (审计)
  - 双向同步冲突解决 (last-write-wins by version)
  - 未来企微 webhook 反向推数据时, 识别来源
  - 离线模式: 企微挂了不影响本地

字段:
  - data_source: "manual" | "ai" | "wecom_callback" | "import" | "seed"
  - source_detail: "user:domai" / "agent:hotel-ai-frontdesk" / "wecom:userid_xxx"
  - version: 自增版本号, 每次 update 自增
  - updated_at: ISO 字符串
  - updated_by: 人类可读是谁改的 (deprecated, 用 source_detail)

v2.1.18+ 流程:
  1. 任何业务路由在 save_table 之前, 调用 apply_meta_on_create / apply_meta_on_update
  2. 自动 safe_sync 推企微 (本地 → 企微)
  3. 企微 webhook 回调时, 用 apply_meta_on_update + 写本地 (企微 → 本地)
  4. 双向冲突时, 按 version 比较, 大的赢 (last-write-wins)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict

from . import data_layer


# 来源枚举 (前端展示用)
DATA_SOURCE_LABELS = {
    "manual": "👤 手动",
    "ai": "🤖 AI",
    "wecom_callback": "📨 企微回调",
    "import": "📥 导入",
    "seed": "🌱 种子数据",
    "migration": "🔄 数据迁移",
}


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def new_version() -> int:
    """每次数据变更都 +1 (用于 last-write-wins 冲突解决)"""
    return int(time.time() * 1000)  # 毫秒时间戳


def meta_for_new(source: str = "manual", operator: str = "") -> Dict[str, Any]:
    """新建数据项时, 初始化元信息

    Args:
        source: 来自哪个流程 (manual/ai/wecom_callback/import/seed)
        operator: 谁 (用户ID/agent_id/企微userid), 可空
    """
    return {
        "data_source": source,
        "source_detail": operator,
        "version": new_version(),
        "created_at": now(),
        "updated_at": now(),
    }


def meta_for_update(source: str = "manual", operator: str = "") -> Dict[str, Any]:
    """更新现有数据项时, 更新元信息 (保留 created_at / data_source 的初始值)
    注意: data_source 不应该被覆盖 (初次创建时已确定), 但记录 last_modified_source
    """
    return {
        "last_modified_source": source,
        "last_modified_by": operator,
        "version": new_version(),
        "updated_at": now(),
    }


def apply_meta_on_create(item: Dict[str, Any], source: str = "manual", operator: str = "") -> Dict[str, Any]:
    """新建时: 在 item 上叠加元信息 (如果还没有)"""
    if "data_source" not in item:
        item.update(meta_for_new(source, operator))
    return item


def apply_meta_on_update(item: Dict[str, Any], source: str = "manual", operator: str = "") -> Dict[str, Any]:
    """更新时: 在 item 上叠加 updated 信息 (不覆盖 created_at)"""
    if "created_at" in item and "data_source" not in item:
        # 老数据没 meta, 给一个兜底
        item["data_source"] = "migration"
        item["source_detail"] = ""
        item["version"] = new_version()
    # 更新 last_modified 字段
    item["last_modified_source"] = source
    item["last_modified_by"] = operator
    item["version"] = new_version()
    item["updated_at"] = now()
    return item


def load_table_with_meta(table: str, default_source: str = "manual") -> list:
    """读表, 给老数据(没meta字段的)补 meta (向后兼容)

    这样不破坏现有数据, 读到内存里时自动补字段。
    """
    rows = data_layer.load_table(table)
    for r in rows:
        if "data_source" not in r:
            r["data_source"] = default_source
        if "version" not in r:
            r["version"] = new_version()
        if "updated_at" not in r:
            r["updated_at"] = r.get("created_at", now())
    return rows


# ════════════════════════════════════════════════════════════
# 双向同步冲突解决 (last-write-wins)
# ════════════════════════════════════════════════════════════


def should_overwrite(local_item: Dict, remote_item: Dict) -> bool:
    """对比本地和企微两端版本, 决定是否覆盖本地

    规则: version 大的赢 (代表更晚写入); version 相等时保留本地 (避免 ping-pong)
    """
    local_v = local_item.get("version", 0) or 0
    remote_v = remote_item.get("version", 0) or 0
    return remote_v > local_v


def log_sync_event(
    table: str,
    op: str,  # "push" / "pull" / "webhook" / "retry"
    direction: str,  # "local→wecom" / "wecom→local"
    record_id: str = "",
    source: str = "manual",
    success: bool = True,
    error: str = "",
):
    """写一条同步历史到 wecom_sync_log 表 (运维/排错用)"""
    entry = {
        "time": now(),
        "table": table,
        "op": op,
        "direction": direction,
        "record_id": record_id,
        "source": source,
        "success": success,
        "error": error[:500],
    }
    try:
        data_layer.append_log("wecom_sync_log", entry)
    except Exception as exc:
        # append_log 会因为不存在 append 而失败 (wecom_sync_log 不在 append_log 列表中? 实际是)
        # 兜底: 直接读+写
        try:
            rows = data_layer.load_table("wecom_sync_log")
            rows.append(entry)
            # 限制最大 1000 条
            if len(rows) > 1000:
                rows = rows[-1000:]
            data_layer.save_table("wecom_sync_log", rows)
        except Exception:
            pass  # 静默失败, 不影响主流程