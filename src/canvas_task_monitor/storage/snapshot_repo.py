"""快照仓储：保存每轮采集到的原始条目内容指纹，作为变更检测的比对基准。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from ..core.hashing import canonical_hash
from ..core.models import RawItem
from .db import Database, utc_now_iso

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO snapshots (
    source, external_id, course_id, content_hash, payload_json, first_seen_at, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, external_id) DO UPDATE SET
    -- first_seen_at 刻意不出现在 SET 里：它记录"首次见到该条目"的时间，必须保持不动。
    course_id = excluded.course_id,
    content_hash = excluded.content_hash,
    payload_json = excluded.payload_json,
    last_seen_at = excluded.last_seen_at
"""

_SELECT_HASH_SQL = "SELECT content_hash FROM snapshots WHERE source = ? AND external_id = ?"
_SELECT_PAYLOAD_SQL = "SELECT payload_json FROM snapshots WHERE source = ? AND external_id = ?"
_SELECT_IDS_SQL = "SELECT external_id FROM snapshots WHERE source = ?"
_DELETE_SQL = "DELETE FROM snapshots WHERE source = ? AND external_id = ?"


class SnapshotRepo:
    """snapshots 表的读写入口。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get_hash(self, source: str, external_id: str) -> str | None:
        """读取已存快照的内容指纹；该条目从未入库时返回 None。"""
        with self.db.lock:
            row = self.db.conn.execute(_SELECT_HASH_SQL, (source, external_id)).fetchone()
        return None if row is None else str(row["content_hash"])

    def get_payload(self, source: str, external_id: str) -> dict[str, Any] | None:
        """读取已存快照的原始 payload；不存在或 JSON 损坏时返回 None。

        供变更检测计算 changed_fields 使用（只需要旧 payload 的白名单字段）。
        """
        with self.db.lock:
            row = self.db.conn.execute(_SELECT_PAYLOAD_SQL, (source, external_id)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("快照 payload_json 解析失败，按缺失处理：%s/%s", source, external_id)
            return None
        return payload if isinstance(payload, dict) else None

    def list_ids(self, source: str) -> set[str]:
        """列出某个 source 下已入库的全部 external_id。"""
        with self.db.lock:
            rows = self.db.conn.execute(_SELECT_IDS_SQL, (source,)).fetchall()
        return {str(row["external_id"]) for row in rows}

    def upsert_many(self, items: Iterable[RawItem]) -> None:
        """批量写入快照：新条目插入，已有条目刷新指纹、payload 与 last_seen_at。"""
        now = utc_now_iso()
        rows = [
            (
                item.source,
                item.external_id,
                item.course_id,
                canonical_hash(item.source, item.data),
                json.dumps(item.data, ensure_ascii=False, sort_keys=True, default=str),
                now,
                now,
            )
            for item in items
        ]
        if not rows:
            return
        with self.db.lock, self.db.conn:
            self.db.conn.executemany(_UPSERT_SQL, rows)

    def delete(self, source: str, external_id: str) -> None:
        """删除一条快照（供人工清理或来源下线时使用）。"""
        with self.db.lock, self.db.conn:
            self.db.conn.execute(_DELETE_SQL, (source, external_id))
