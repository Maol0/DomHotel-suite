# -*- coding: utf-8 -*-
"""Hotel Frontdesk PawApp — 数据访问层 (v1.4.0 SQLite 版)

v1.4.0 SQLite:
  - 业务表全部落 SQLite (hotel.db, WAL 模式): 整表写入包在单个事务里,
    原子提交、崩溃安全, 多进程/多线程由 DB 锁串行化 —— 根治 JSON 整文件
    重写在并发写入/进程崩溃时造成的丢写与文件损坏 (订单/房态不同步的根源)。
  - 对外 API 与 JSON 版完全一致 (load_table/save_table/append_log/...),
    全部路由模块零改动。
  - 首次访问自动迁移: {table}.json → SQLite, 迁移成功后 json 改名为
    {table}.json.migrated 留作备份 (防二次导入); DB 有数据时以 DB 为准。
  - 版本机制升级: 写计数器落 _meta 表, 多进程部署下 /data/version 也一致。
  - 仍保持 JSON 的文件 (非 KNOWN_TABLES, 不受影响):
    auth_setup.json / auth_log.json / wecom_runtime_config.json /
    wecom_pending_queue.json / chat_sessions.json / hotel_config 迁移后的
    .migrated 备份等。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# v2.0 自己的数据目录
# 默认: 进程 CWD 下的 data/v20/ (无需 root 权限,任何用户都能跑)
# 生产: 通过 env HOTEL_DATA_DIR_V20 指向持久化目录
# v1.4.0-persistent: 优先使用 /app/working/dompaw-data-backup（持久化目录）
_DEFAULT_DATA_DIR = "/app/working/dompaw-data-backup"
_DATA_ROOT = Path(os.environ.get(
    "HOTEL_DATA_DIR_V20",
    _DEFAULT_DATA_DIR if Path(_DEFAULT_DATA_DIR).exists() else str(Path.cwd() / "data" / "v20"),
))
DATA_DIR = _DATA_ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 已知表名（防止任意表名; SQL 表名做 f"t_{table}" 拼接, 必须白名单约束）
KNOWN_TABLES = {
    "rooms",
    "work_orders",
    "guests",
    "supplies",
    "schedules",
    "handover",
    "rooms_log",
    "guests_log",
    "supplies_log",
    "work_orders_log",
    # 后台配置 (admin/* endpoints)
    "staff",
    "departments",
    "customers",
    "vendors",
    "roles",
    "permissions",
    "knowledge",
    "knowledge_files",
    "wecom_sync_log",
    "system_config",
    "room_types",  # v2.1.18 修复: 漏注册导致 admin_create 500
    "floors",      # v2.1.18 修复: 同上
    # Phase 6: pending confirmation queue
    "pending_actions",
    # v2.2.0: 已完成工单归档
    "work_orders_archive",
    # 整合版: HUB 配置向导 + 前台接待登记
    "hotel_config",
    "checkins",
    "checkins_log",
    # v2.2.2: 派单闭环 — Request/Ticket 双表
    "requests",
}

# 各表主键字段 (存 pk 列, 便于调试/未来行级操作; 无主键的表存 "")
_TABLE_PK: Dict[str, str] = {
    "rooms": "room_no",
    "work_orders": "wo_id",
    "work_orders_archive": "wo_id",
    "checkins": "id",
    "checkins_log": "id",
    "staff": "id",
    "departments": "id",
    "room_types": "id",
    "floors": "id",
    "customers": "customer_id",
    "pending_actions": "id",
    "requests": "request_id",
}

# ─────────────────────────────────────────────
# SQLite 引擎 (v1.4.0)
# ─────────────────────────────────────────────

_DB_PATH = DATA_DIR / "hotel.db"
_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None
_ensured: set = set()          # 本进程已 ensure(建表+迁移) 的表
_WRITE_COUNTER_FALLBACK = 0    # DB 不可用时的兜底计数 (进程内)

# 进程启动时间戳: 重启即变化, 前端会多刷一次, 无害
_BOOT_TS = int(time.time())


def _db() -> sqlite3.Connection:
    """惰性初始化 SQLite 连接 (autocommit 模式, 事务手动管理)"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(
            str(_DB_PATH), check_same_thread=False, timeout=10,
            isolation_level=None,  # autocommit; 事务显式 BEGIN/COMMIT
        )
        _conn.execute("PRAGMA journal_mode=WAL")     # 读写不互斥, 崩溃安全
        _conn.execute("PRAGMA busy_timeout=8000")    # 跨进程写锁等待
        _conn.execute("PRAGMA synchronous=NORMAL")   # WAL 下安全且快
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS _meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
    return _conn


def _row_pk(table: str, row: Dict[str, Any]) -> str:
    pk = _TABLE_PK.get(table) or "id"
    v = row.get(pk) if isinstance(row, dict) else None
    return str(v) if v not in (None, "") else ""


def _ensure_table(table: str) -> None:
    """建 SQL 表 + 首次从 {table}.json 自动迁移 (双检锁, 进程内只跑一次)"""
    if table in _ensured:
        return
    with _LOCK:
        if table in _ensured:
            return
        db = _db()
        tname = f"t_{table}"
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {tname} ("
            f"seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            f"pk TEXT, row_json TEXT NOT NULL)"
        )
        n = db.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
        if n == 0:
            src = DATA_DIR / f"{table}.json"
            if src.is_file():
                try:
                    rows = json.loads(src.read_text(encoding="utf-8"))
                    db.execute("BEGIN IMMEDIATE")
                    db.executemany(
                        f"INSERT INTO {tname}(pk, row_json) VALUES(?, ?)",
                        [
                            (_row_pk(table, r), json.dumps(r, ensure_ascii=False))
                            for r in rows
                        ],
                    )
                    db.execute("COMMIT")
                    # 迁移成功 → json 改名留备份 (防二次导入; 用户可手动删除)
                    src.rename(DATA_DIR / f"{table}.json.migrated")
                    logger.info(
                        "[data_layer] 迁移表 %s: %d 行 (json → SQLite, 原文件备份为 .migrated)",
                        table, len(rows),
                    )
                except Exception as exc:
                    try:
                        db.execute("ROLLBACK")
                    except Exception:
                        pass
                    logger.error("[data_layer] 迁移表 %s 失败(保持 JSON 不动): %s", table, exc)
        _ensured.add(table)


def _bump_counter(db: sqlite3.Connection) -> int:
    """写计数器 +1 (调用方需已持有事务), 返回新值"""
    db.execute("INSERT OR IGNORE INTO _meta(k, v) VALUES('write_counter', '0')")
    db.execute(
        "UPDATE _meta SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT) WHERE k = 'write_counter'"
    )
    row = db.execute("SELECT v FROM _meta WHERE k = 'write_counter'").fetchone()
    return int(row[0])


def get_data_version() -> str:
    """全局数据版本: '{启动时间戳}:{累计写表次数}' (计数器落 DB, 跨进程一致)"""
    global _WRITE_COUNTER_FALLBACK
    with _LOCK:
        try:
            row = _db().execute(
                "SELECT v FROM _meta WHERE k = 'write_counter'"
            ).fetchone()
            return f"{_BOOT_TS}:{int(row[0]) if row else 0}"
        except Exception:
            _WRITE_COUNTER_FALLBACK += 0  # 读失败不 bump
            return f"{_BOOT_TS}:{_WRITE_COUNTER_FALLBACK}"


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return time.strftime("%Y-%m-%d")


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def load_table(table: str) -> List[Dict[str, Any]]:
    """读一张表(SQLite), 不存在/空表返空 list

    注意: 与 JSON 版不同, DB 异常时 raise 而不是返回 [] —— 防止调用方
    拿着空列表 save_table 把好数据覆盖掉 (JSON 版的隐性丢数据路径)。
    """
    if table not in KNOWN_TABLES:
        logger.warning("load_table: unknown table %s", table)
        return []
    with _LOCK:
        _ensure_table(table)
        cur = _db().execute(f"SELECT row_json FROM t_{table} ORDER BY seq")
        return [json.loads(r[0]) for r in cur.fetchall()]


def save_table(table: str, rows: List[Dict[str, Any]]) -> None:
    """写一张表(整表覆盖, 单事务原子提交; 语义与 JSON 版一致)

    每次写入都会 bump 全局数据版本号 (整合版全局同步机制)。
    事务里同时完成 DELETE + INSERT + 计数器 bump, 崩溃时整体回滚,
    不会出现半写状态。
    """
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table: {table}")
    rows = list(rows or [])
    with _LOCK:
        _ensure_table(table)
        db = _db()
        tname = f"t_{table}"
        db.execute("BEGIN IMMEDIATE")  # 先拿写锁 (跨进程也串行)
        try:
            db.execute(f"DELETE FROM {tname}")
            db.executemany(
                f"INSERT INTO {tname}(pk, row_json) VALUES(?, ?)",
                [
                    (_row_pk(table, r), json.dumps(r, ensure_ascii=False))
                    for r in rows
                ],
            )
            _bump_counter(db)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def append_log(table: str, entry: Dict[str, Any]) -> None:
    """追加一条到 *log 表（自动加 ts）"""
    if not table.endswith("_log"):
        raise ValueError(f"{table} is not a log table")
    entry = dict(entry)
    entry.setdefault("ts", now_str())
    rows = load_table(table)
    rows.append(entry)
    save_table(table, rows)


def version_info() -> Dict[str, Any]:
    """返回数据目录诊断 (v1.4.0: SQLite 引擎 + 各表行数)"""
    tables: Dict[str, int] = {}
    backups: List[str] = []
    db_ok = True
    try:
        with _LOCK:
            for t in sorted(KNOWN_TABLES):
                try:
                    n = _db().execute(f"SELECT COUNT(*) FROM t_{t}").fetchone()[0]
                except Exception:
                    n = 0
                if n:
                    tables[t] = n
            backups = sorted(p.name for p in DATA_DIR.glob("*.json.migrated"))
    except Exception as exc:
        db_ok = False
        logger.error("[data_layer] version_info 失败: %s", exc)
    return {
        "version": "2.0.x",
        "storage": "sqlite",
        "db_ok": db_ok,
        "db_file": str(_DB_PATH),
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "tables": tables,
        "total_rows": sum(tables.values()),
        # 兼容老字段: 前端展示用 (现在列迁移备份文件)
        "files": backups,
        "v20_files": backups,
    }
