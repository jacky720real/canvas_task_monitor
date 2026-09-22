"""变更日志仓储：记录每轮检测到的变更，作为审计与排障依据。"""

from __future__ import annotations

import json

from ..core.models import ChangeRecord
from .db import Database, utc_now_iso

_INSERT_SQL = """
INSERT INTO change_log (source, external_id, change_type, diff_json, detected_at, processed)
VALUES (?, ?, ?, ?, ?, ?)
"""


class ChangeRepo:
    """change_log 表的写入入口。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_many(self, changes: list[ChangeRecord]) -> None:
        """批量写入变更日志。

        processed 恒写 1：本方法只在 LLM 流程成功走完后由 Poller 调用。
        将来若要区分"LLM 失败 vs 全部噪声"，会在 Phase 5 改 extractor 返回值，
        届时加 processed 参数（boolean 语义 = LLM 是否成功走完流程）。
        """
        detected_at = utc_now_iso()
        rows = [
            (
                change.source,
                change.external_id,
                change.change_type,
                json.dumps(change.diff, ensure_ascii=False, sort_keys=True, default=str),
                detected_at,
                1,
            )
            for change in changes
        ]
        if not rows:
            return
        with self.db.lock, self.db.conn:
            self.db.conn.executemany(_INSERT_SQL, rows)
