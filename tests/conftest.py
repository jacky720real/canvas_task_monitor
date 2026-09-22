"""pytest 共享 fixture：内存 DB、可编程假 LLM、可控假连接器、模板、对象工厂。

设计原则（源自 Phase 9 裁决）：
- 只做最小替身（替换网络/存储边界），其余走真实代码。
- 不在断言里依赖第三方库的渲染细节（unicode 字符等）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from canvas_task_monitor.ai.template import PromptTemplate
from canvas_task_monitor.connectors.base import BaseConnector
from canvas_task_monitor.core.hashing import canonical_hash
from canvas_task_monitor.core.models import ChangeRecord, RawItem, TaskItem
from canvas_task_monitor.storage.db import Database
from canvas_task_monitor.storage.snapshot_repo import SnapshotRepo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "config" / "templates" / "task_extract_template.yaml"


@pytest.fixture
def memory_db() -> Iterator[Database]:
    """内存 SQLite（不落盘，自动关闭）。"""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def snapshot_repo(memory_db: Database) -> SnapshotRepo:
    return SnapshotRepo(memory_db)


@pytest.fixture
def template() -> PromptTemplate:
    """真实模板（config/templates/task_extract_template.yaml）。"""
    return PromptTemplate.load(TEMPLATE_PATH)


class FakeLLM:
    """可编程假 LLM 客户端：注入返回值序列（可混入异常），并记录调用次数。"""

    def __init__(self, payloads: list[Any] | None = None, error: Exception | None = None) -> None:
        self.payloads = list(payloads or [])
        self.error = error
        self.calls = 0
        self.last_user = ""

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls += 1
        self.last_user = user
        if self.error is not None:
            raise self.error
        if not self.payloads:
            return {"tasks": []}
        item = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeConnector(BaseConnector):
    """可控假连接器：返回注入的条目，或抛注入的异常。"""

    name = "canvas"

    def __init__(self, items: list[RawItem] | None = None, error: Exception | None = None) -> None:
        super().__init__(rate_limit_rps=1000)
        self.items = list(items or [])
        self.error = error

    async def fetch(self) -> list[RawItem]:
        if self.error is not None:
            raise self.error
        return list(self.items)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_connector() -> FakeConnector:
    return FakeConnector()


def _build_item(index: int = 1, **payload_overrides: Any) -> RawItem:
    payload: dict[str, Any] = {
        "name": f"作业{index}",
        "description": "3000 字",
        "due_at": "2026-10-08",
        "points_possible": 100,
        "submission_types": ["online_upload"],
        # 以下两个是白名单外字段，用于验证"噪声字段不触发变更"
        "updated_at": "2026-09-22T10:00:00Z",
        "course_name": "机器学习",
    }
    payload.update(payload_overrides)
    return RawItem(
        source="canvas_assignment",
        external_id=f"course:1:assignment:{index}",
        course_id="1",
        payload=payload,
    )


def _build_change(index: int = 1, **payload_overrides: Any) -> ChangeRecord:
    item = _build_item(index, **payload_overrides)
    return ChangeRecord.from_item(item, "new", canonical_hash(item.source, item.payload))


def _build_task(index: int = 1, **overrides: Any) -> TaskItem:
    data: dict[str, Any] = {
        "source": "canvas_assignment",
        "external_id": f"course:1:assignment:{index}",
        "category": "assignment",
        "title": f"任务{index}",
        "course": "机器学习",
        "urgency": 3,
        "importance": 2,
        "score": 46,
        "tags": ["paper"],
        "urgency_reason": "三天内",
        "importance_reason": "一般作业",
    }
    data.update(overrides)
    return TaskItem(**data)


@pytest.fixture
def make_item() -> Callable[..., RawItem]:
    return _build_item


@pytest.fixture
def make_change() -> Callable[..., ChangeRecord]:
    return _build_change


@pytest.fixture
def make_task() -> Callable[..., TaskItem]:
    return _build_task


def _build_llm_entry(**overrides: Any) -> dict[str, Any]:
    """构造一条"LLM 应该输出的任务对象"（不含 score，score 由代码算）。"""
    data: dict[str, Any] = {
        "source": "canvas_assignment",
        "external_id": "course:1:assignment:1",
        "change_type": "new",
        "category": "assignment",
        "title": "论文初稿",
        "summary": "3000 字",
        "course": "机器学习",
        "due_at": "2026-10-08T23:59:00+08:00",
        "urgency": 5,
        "importance": 3,
        "tags": ["paper"],
        "is_rule": False,
        "urgency_reason": "今天截止",
        "importance_reason": "占比 20%",
    }
    data.update(overrides)
    return data


@pytest.fixture
def llm_entry() -> Callable[..., dict[str, Any]]:
    return _build_llm_entry


@pytest.fixture
def fake_facade() -> Callable[[dict], Any]:
    """构造一个"只回固定结果"的假 Container（用于测适配层的失败路径）。"""

    def _make(result: dict) -> Any:
        class _Facade:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            async def invoke(self, action: str, params: dict) -> dict:
                self.calls.append((action, params))
                return result

        class _Container:
            def __init__(self) -> None:
                self.facade = _Facade()

            async def aclose(self) -> None:
                return None

        return _Container()

    return _make
