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

    def record_many(
        self,
        changes: list[ChangeRecord],
        processed: bool = True,
    ) -> None:
        """批量写入变更日志。

        processed 语义（**由 Poller 传入 llm_ok**）：
        - True  → LLM 成功走完流程（产出 0 条任务也算成功，那是"全是噪声"）
        - False → LLM 调用失败 / schema 校验失败 / 部分批次失败

        之前 Phase 2 里 processed 恒写 1 是临时方案，现由调用者控制。
        """
        detected_at = utc_now_iso()
        rows = [
            (
                change.source,
                change.external_id,
                change.change_type,
                json.dumps(change.diff, ensure_ascii=False, sort_keys=True, default=str),
                detected_at,
                int(processed),
            )
            for change in changes
        ]
        if not rows:
            return
        with self.db.lock, self.db.conn:
            self.db.conn.executemany(_INSERT_SQL, rows)
