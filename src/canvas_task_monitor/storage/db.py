"""SQLite 连接与 Schema 初始化。

【并发模型说明 — 重要】
- sqlite3 是同步库，本项目在 asyncio 环境中通过 asyncio.to_thread 间接调用。
- 本文件使用 check_same_thread=False，这是"有风险开关"。
- 作为补偿，Database 暴露一个全局 threading.RLock（self.lock）。
- **硬性约束：所有对 self._conn 的读写必须包在 with self.db.lock 里。**
  任何绕过锁直接访问 conn 的代码都是 bug。
- 禁止在多线程中并发直接使用同一个连接做长事务。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# 建表语句。字段与第七部分规格一一对应，不额外增减列。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    course_id     TEXT,
    content_hash  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    category          TEXT NOT NULL,
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    course            TEXT NOT NULL DEFAULT '',
    due_at            TEXT,
    urgency           INTEGER NOT NULL DEFAULT 0,
    importance        INTEGER NOT NULL DEFAULT 0,
    score             INTEGER NOT NULL DEFAULT 0,
    tags_json         TEXT NOT NULL DEFAULT '[]',
    is_rule           INTEGER NOT NULL DEFAULT 0,
    urgency_reason    TEXT NOT NULL DEFAULT '',
    importance_reason TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    raw_json          TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    diff_json   TEXT NOT NULL DEFAULT '{}',
    detected_at TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);

-- poll_state：预留未启用。
-- 本项目采用"全量拉取 + hash 对比"策略，不需要增量游标。
-- 若未来要做真正的增量拉取（如按 last_poll_at 拉取），在此启用。
CREATE TABLE IF NOT EXISTS poll_state (
    source       TEXT PRIMARY KEY,
    cursor       TEXT,
    last_poll_at TEXT
);
"""


def utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（秒精度），供各仓储统一打时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_path(path: str | Path) -> str:
    """规整数据库路径。":memory:" 原样返回；其余确保父目录存在并转为绝对路径。"""
    raw = str(path)
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    db_file = Path(raw).expanduser().resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return str(db_file)


class Database:
    """SQLite 连接持有者，负责建库、开启 WAL 与初始化 Schema。"""

    def __init__(self, path: str | Path = "./data/tasks.db") -> None:
        self.path = _normalize_path(path)
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self.lock:
            # WAL 模式下读写不互相阻塞，适合"采集写入 + CLI 查询"并存的场景。
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """底层连接。调用方必须在 with self.lock 保护下使用它。"""
        return self._conn

    def close(self) -> None:
        """关闭连接；重复调用是安全的。"""
        with self.lock:
            self._conn.close()
