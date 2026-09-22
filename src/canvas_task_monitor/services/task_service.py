"""任务服务：对 TaskRepo 的业务语义包装。

职责边界：只提供"任务"这一块的动作方法；不做 DTO 转换（DTO 在 interfaces/dto.py，
由 Facade 层统一调用），也不做数据库之外的任何事。
"""

from __future__ import annotations

from typing import Any

from ..core.models import TaskStatus
from ..storage.task_repo import TaskRepo


class TaskService:
    """任务清单的读写入口。"""

    def __init__(self, repo: TaskRepo) -> None:
        self.repo = repo

    def list_tasks(
        self,
        category: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出任务，返回**原始行 dict**（未做 DTO 转换）。

        原始行含 tags_json / raw_json 列，DTO 转换由 Facade 层负责，
        这样"对外结构"只有一处定义。
        """
        return self.repo.list(category=category, status=status, limit=limit)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """按 ID 取单条任务（原始行 dict）；不存在返回 None。"""
        return self.repo.get(task_id)

    def set_status(self, task_id: int, status: TaskStatus) -> bool:
        """设置任务状态，返回是否命中行。"""
        return self.repo.set_status(task_id, status)

    def mark_done(self, task_id: int) -> bool:
        """标记完成（用户勾选）。"""
        return self.repo.set_status(task_id, "done")

    def mark_pending(self, task_id: int) -> bool:
        """取消完成（用户取消勾选）。"""
        return self.repo.set_status(task_id, "pending")
