"""Facade 契约：版本号、5 项能力、invoke 路由与异常兜底、DTO 转换。

用临时 settings（内存 DB、不启用数据源）装配真实 Container，
这样契约测试覆盖的就是生产代码路径。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from canvas_task_monitor.contracts.plugin import PLUGIN_API_VERSION
from canvas_task_monitor.services.bootstrap import Container

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "config" / "templates" / "task_extract_template.yaml"

EXPECTED_CAPABILITIES = (
    "poll_now",
    "list_tasks",
    "get_task",
    "mark_task",
    "summarize_pending",
)


@pytest.fixture
def container(tmp_path: Path) -> Iterator[Container]:
    settings = {
        "app": {"db_path": ":memory:", "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": [], "jitter_seconds": 30},
        "canvas": {"base_url": "", "token": ""},
        "mail": {"provider": "graph", "graph": {}, "imap": {}},
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")

    instance = Container(settings_path)
    yield instance
    asyncio.run(instance.aclose())


async def test_api_version_matches_contract(container: Container) -> None:
    assert container.facade.api_version == PLUGIN_API_VERSION
    assert container.facade.name == "canvas_task_monitor"


async def test_capabilities_declares_five_items(container: Container) -> None:
    capabilities = await container.facade.capabilities()

    assert tuple(item.name for item in capabilities) == EXPECTED_CAPABILITIES
    for item in capabilities:
        assert item.description
        assert isinstance(item.params_schema, dict)
        assert isinstance(item.returns_schema, dict)


async def test_every_capability_is_routable(container: Container) -> None:
    """capabilities() 里声明的能力，invoke() 必须都能命中（不然宿主会踩空）。"""
    probes: dict[str, dict[str, Any]] = {
        "poll_now": {},
        "list_tasks": {},
        "get_task": {"task_id": 1},
        "mark_task": {"task_id": 1, "done": True},
        "summarize_pending": {},
    }

    for name in EXPECTED_CAPABILITIES:
        result = await container.facade.invoke(name, probes[name])
        assert result["ok"] is True, f"{name} 未被正确路由：{result}"


async def test_unknown_action_returns_error_without_raising(container: Container) -> None:
    result = await container.facade.invoke("不存在的 action", {})

    assert result["ok"] is False
    assert result["error"].startswith("unknown action:")


async def test_business_exception_is_wrapped(container: Container) -> None:
    """参数缺失等业务异常必须被兜住成 {"ok": False}，而不是抛给宿主。"""
    result = await container.facade.invoke("get_task", {})

    assert result["ok"] is False
    assert "KeyError" in result["error"]


async def test_list_tasks_returns_dto_shape(
    container: Container, make_task: Callable[..., Any]
) -> None:
    """对外结构：tags 是数组、不含 tags_json / raw_json（DTO 层统一负责）。"""
    container.tasks_repo.upsert(make_task(1, status="pending"))

    result = await container.facade.invoke("list_tasks", {})
    row = result["data"][0]

    assert result["ok"] is True
    assert row["tags"] == ["paper"]
    assert "tags_json" not in row
    assert "raw_json" not in row


async def test_mark_task_updates_status(
    container: Container, make_task: Callable[..., Any]
) -> None:
    task_id = container.tasks_repo.upsert(make_task(1, status="pending"))

    result = await container.facade.invoke("mark_task", {"task_id": task_id, "done": True})

    assert result["data"]["status"] == "done"
    assert container.tasks_repo.get(task_id)["status"] == "done"


async def test_summarize_pending_shape(
    container: Container, make_task: Callable[..., Any]
) -> None:
    container.tasks_repo.upsert(make_task(1, category="assignment", urgency=5, status="pending"))
    container.tasks_repo.upsert(make_task(2, category="reminder", urgency=0, status="done"))

    result = await container.facade.invoke("summarize_pending", {})

    assert result["data"] == {
        "total": 1,
        "assignment": 1,
        "activity": 0,
        "reminder": 0,
        "max_urgency": 5,
    }


async def test_health_reports_ok(container: Container) -> None:
    health = await container.facade.health()

    assert health["status"] == "ok"
    assert health["detail"]["db"] == "readable"
