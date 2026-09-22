"""tasks 仓储的核心不变式：用户勾选状态永远不被自动流程覆盖。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from canvas_task_monitor.storage.db import Database
from canvas_task_monitor.storage.task_repo import TaskRepo


def test_upsert_does_not_overwrite_user_status(
    memory_db: Database, make_task: Callable[..., Any]
) -> None:
    """upsert 不覆盖用户手动设置的 status。

    场景：任务入库 → 用户勾选 done → 下一轮轮询时 upsert 同 ID 任务
    期望：status 保持 done，不被重置为 pending；内容字段照常更新。
    """
    repo = TaskRepo(memory_db)
    task_id = repo.upsert(make_task(1, status="pending", title="论文初稿"))
    assert repo.set_status(task_id, "done") is True

    # 下一轮轮询：LLM 又抽出一条同 ID 的 pending 任务
    same_id = repo.upsert(make_task(1, status="pending", title="论文终稿"))

    assert same_id == task_id
    row = repo.get(task_id)
    assert row is not None
    assert row["status"] == "done"  # ★ 用户状态优先
    assert row["title"] == "论文终稿"  # ★ 内容仍然被更新


def test_set_status_round_trip(memory_db: Database, make_task: Callable[..., Any]) -> None:
    repo = TaskRepo(memory_db)
    task_id = repo.upsert(make_task(1))

    assert repo.set_status(task_id, "done") is True
    assert repo.get(task_id)["status"] == "done"
    assert repo.set_status(task_id, "pending") is True
    assert repo.get(task_id)["status"] == "pending"


def test_set_status_rejects_illegal_value(
    memory_db: Database, make_task: Callable[..., Any]
) -> None:
    repo = TaskRepo(memory_db)
    task_id = repo.upsert(make_task(1))

    with pytest.raises(ValueError, match="非法任务状态"):
        repo.set_status(task_id, "archived")


def test_set_status_returns_false_for_missing_task(memory_db: Database) -> None:
    assert TaskRepo(memory_db).set_status(9999, "done") is False


def test_list_returns_raw_rows_and_filters(
    memory_db: Database, make_task: Callable[..., Any]
) -> None:
    """list() 返回**原始行 dict**（含 tags_json / raw_json），DTO 转换由上层负责。"""
    repo = TaskRepo(memory_db)
    repo.upsert(make_task(1, category="assignment", status="pending", urgency=5))
    repo.upsert(make_task(2, category="activity", status="pending", urgency=1))
    repo.upsert(make_task(3, category="reminder", status="done", urgency=0))

    rows = repo.list()
    assert len(rows) == 3
    assert "tags_json" in rows[0] and "raw_json" in rows[0]
    assert rows[0]["title"] == "任务1"  # score/urgency 倒序 → urgency=5 排最前

    assert [row["title"] for row in repo.list(status="pending")] == ["任务1", "任务2"]
    assert [row["title"] for row in repo.list(category="reminder")] == ["任务3"]
    assert len(repo.list(limit=1)) == 1


def test_get_returns_none_for_missing_task(memory_db: Database) -> None:
    assert TaskRepo(memory_db).get(9999) is None
