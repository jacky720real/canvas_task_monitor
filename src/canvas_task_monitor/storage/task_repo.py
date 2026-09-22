"""任务仓储：保存 LLM 抽取后的结构化任务，并维护用户勾选的状态。

【读写形态说明】
- upsert(task: TaskItem) -> int：入参是校验过的领域模型，返回 tasks 表主键。
- list() / get() 返回**原始行 dict**（保留 tags_json / raw_json 等 SQL 列名），
  对外结构的转换统一由 interfaces/dto.py 负责，
  避免"同一份结构在两个地方被各自改写"。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import TaskItem
from .db import Database, utc_now_iso

# ★ status 绝对不出现在下面的 SET 子句里，原因见 SQL 内注释。
_UPSERT_SQL = """
INSERT INTO tasks (
    source, external_id, category, title, summary, course, due_at,
    urgency, importance, score, tags_json, is_rule,
    urgency_reason, importance_reason, status, raw_json, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, external_id) DO UPDATE SET
    -- ★★ 硬性约束：SET 子句里绝不能出现 status。★★
    -- 原因：status 是用户在本地勾选的状态（pending / done），代表人的意志；
    --   而本条 upsert 的数据来自 LLM 抽取结果 + 自动轮询。若把 status 一并覆盖，
    --   用户刚勾选完成的任务会在下一轮轮询后被打回 pending。
    --   LLM 与任何自动流程都无权改写用户的状态。
    -- created_at 同理不更新：它记录任务首次入库时间。
    category          = excluded.category,
    title             = excluded.title,
    summary           = excluded.summary,
    course            = excluded.course,
    due_at            = excluded.due_at,
    urgency           = excluded.urgency,
    importance        = excluded.importance,
    score             = excluded.score,
    tags_json         = excluded.tags_json,
    is_rule           = excluded.is_rule,
    urgency_reason    = excluded.urgency_reason,
    importance_reason = excluded.importance_reason,
    raw_json          = excluded.raw_json,
    updated_at        = excluded.updated_at
"""

_SELECT_ID_SQL = "SELECT id FROM tasks WHERE source = ? AND external_id = ?"
_SELECT_BY_ID_SQL = "SELECT * FROM tasks WHERE id = ?"
_UPDATE_STATUS_SQL = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"

_ALLOWED_STATUS = ("pending", "done")


class TaskRepo:
    """tasks 表的读写入口。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, task: TaskItem) -> int:
        """写入或更新一条任务，返回其在 tasks 表中的主键 id。

        说明：这里不用 SQLite 的 RETURNING 语法回读主键，而是 upsert 后紧跟一次
        SELECT，避免依赖 SQLite >= 3.35 这一版本前提。
        """
        now = utc_now_iso()
        params = (
            task.source,
            task.external_id,
            task.category,
            task.title,
            task.summary,
            task.course,
            task.due_at,
            task.urgency,
            task.importance,
            task.score,
            json.dumps(task.tags, ensure_ascii=False),
            int(task.is_rule),
            task.urgency_reason,
            task.importance_reason,
            task.status,
            task.raw_json,
            task.created_at or now,
            now,
        )
        with self.db.lock, self.db.conn:
            self.db.conn.execute(_UPSERT_SQL, params)
            row = self.db.conn.execute(
                _SELECT_ID_SQL, (task.source, task.external_id)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"任务写入后未能读回主键：{task.source}/{task.external_id}")
        return int(row["id"])

    def list(
        self,
        category: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按分数倒序（同分按截止时间升序）列出任务，可按类别与状态过滤。

        返回原始行 dict（含 tags_json / raw_json），由 interfaces/dto.py 转成对外结构。
        """
        clauses: list[str] = []
        params: list[object] = []
        # 注意：这里只拼接固定的条件片段，所有取值一律走占位符，绝不把值拼进 SQL。
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = f"SELECT * FROM tasks{where} ORDER BY score DESC, due_at ASC LIMIT ?"
        params.append(limit)
        with self.db.lock:
            rows = self.db.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id: int) -> dict[str, Any] | None:
        """按主键读取单条任务（原始行 dict）；不存在时返回 None。"""
        with self.db.lock:
            row = self.db.conn.execute(_SELECT_BY_ID_SQL, (task_id,)).fetchone()
        return None if row is None else dict(row)

    def set_status(self, task_id: int, status: str) -> bool:
        """更新任务状态（仅 pending / done），返回是否命中行。

        这是唯一允许改写 status 的入口：只由用户显式操作触发。
        """
        if status not in _ALLOWED_STATUS:
            raise ValueError(f"非法任务状态：{status}（只允许 {' / '.join(_ALLOWED_STATUS)}）")
        with self.db.lock, self.db.conn:
            cursor = self.db.conn.execute(_UPDATE_STATUS_SQL, (status, utc_now_iso(), task_id))
        return cursor.rowcount > 0
