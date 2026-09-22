"""Poller 行为测试。

【为什么单独成文件】
Poller 承载本项目的第一号不变式："LLM 成功之后才写快照"。
这条不变式一旦破坏，后果是"变更事件永久丢失"——比"多花一次 LLM 调用"
严重得多。因此它必须有独立的、长期存在的测试守护，不能依赖临时冒烟脚本。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from canvas_task_monitor.ai.extractor import TaskExtractor
from canvas_task_monitor.ai.template import PromptTemplate
from canvas_task_monitor.connectors.base import BaseConnector
from canvas_task_monitor.core.models import RawItem
from canvas_task_monitor.services.poller import Poller
from canvas_task_monitor.storage.change_repo import ChangeRepo
from canvas_task_monitor.storage.db import Database
from canvas_task_monitor.storage.snapshot_repo import SnapshotRepo
from canvas_task_monitor.storage.task_repo import TaskRepo


def _build_poller(
    db: Database, connector: BaseConnector, llm: Any, template: PromptTemplate
) -> Poller:
    return Poller(
        connectors=[connector],
        snapshot_repo=SnapshotRepo(db),
        task_repo=TaskRepo(db),
        change_repo=ChangeRepo(db),
        extractor=TaskExtractor(template, llm),
        interval=1,
    )


def _count(db: Database, table: str) -> int:
    with db.lock:
        return int(db.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _processed(db: Database) -> list[int]:
    with db.lock:
        rows = db.conn.execute("SELECT processed FROM change_log ORDER BY id").fetchall()
    return [int(row["processed"]) for row in rows]


async def test_no_change_second_round_skips_llm(
    memory_db: Database,
    fake_connector: Any,
    fake_llm: Any,
    template: PromptTemplate,
    make_item: Callable[..., RawItem],
) -> None:
    """第二轮数据未变 → changes=0 / llm_calls=0，且 LLM 调用次数不增加。"""
    fake_connector.items = [make_item(1)]
    fake_llm.payloads = [{"tasks": []}]
    poller = _build_poller(memory_db, fake_connector, fake_llm, template)

    first = await poller.poll_once()
    second = await poller.poll_once()

    assert first == {"sources": 1, "changes": 1, "tasks": 0, "llm_calls": 1}
    assert second == {"sources": 1, "changes": 0, "tasks": 0, "llm_calls": 0}
    assert fake_llm.calls == 1


async def test_llm_failure_leaves_no_snapshot_and_retries_next_round(
    memory_db: Database,
    fake_connector: Any,
    fake_llm: Any,
    template: PromptTemplate,
    make_item: Callable[..., RawItem],
    llm_entry: Callable[..., dict],
) -> None:
    """★ LLM 失败 → 不写快照、不写任务、processed=0；下轮可重试并成功。"""
    fake_connector.items = [make_item(1)]
    fake_llm.error = RuntimeError("LLM 挂了")
    poller = _build_poller(memory_db, fake_connector, fake_llm, template)

    failed = await poller.poll_once()

    assert failed["changes"] == 1
    assert failed["tasks"] == 0
    assert failed["llm_calls"] == 0
    assert _count(memory_db, "snapshots") == 0
    assert _count(memory_db, "tasks") == 0
    assert _processed(memory_db) == [0]  # 变更事实仍留审计痕迹

    fake_llm.error = None
    fake_llm.payloads = [{"tasks": [llm_entry()]}]
    retried = await poller.poll_once()

    assert retried["changes"] == 1
    assert retried["tasks"] == 1
    assert _count(memory_db, "snapshots") == 1
    assert _processed(memory_db) == [0, 1]


async def test_llm_success_writes_snapshot_tasks_and_processed_flag(
    memory_db: Database,
    fake_connector: Any,
    fake_llm: Any,
    template: PromptTemplate,
    make_item: Callable[..., RawItem],
    llm_entry: Callable[..., dict],
) -> None:
    """LLM 成功 → 快照 + 任务落库，change_log.processed=1。"""
    fake_connector.items = [make_item(1)]
    fake_llm.payloads = [{"tasks": [llm_entry()]}]
    poller = _build_poller(memory_db, fake_connector, fake_llm, template)

    stats = await poller.poll_once()

    assert stats == {"sources": 1, "changes": 1, "tasks": 1, "llm_calls": 1}
    assert _count(memory_db, "snapshots") == 1
    assert _count(memory_db, "tasks") == 1
    assert _processed(memory_db) == [1]


async def test_connector_failure_does_not_break_the_round(
    memory_db: Database,
    fake_connector: Any,
    fake_llm: Any,
    template: PromptTemplate,
) -> None:
    """单个数据源抛异常时整轮不崩（返回全 0 统计）。"""
    fake_connector.error = RuntimeError("拉取炸了")
    poller = _build_poller(memory_db, fake_connector, fake_llm, template)

    stats = await poller.poll_once()

    assert stats == {"sources": 1, "changes": 0, "tasks": 0, "llm_calls": 0}
    assert fake_llm.calls == 0
