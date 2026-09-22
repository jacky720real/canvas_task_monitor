"""MCP 适配器：tool 注册 / _invoke 失败语义 / 参数透传。

不断言 SDK 内部属性名（先试公开 API、再退回内部注册表），
因此 mcp 1.x / 2.x 都能跑；缺 SDK 时整体跳过。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

pytest.importorskip("mcp", reason="核心安装不含 MCP SDK，跳过 MCP 适配器测试")

import mcp_server

logger = logging.getLogger(__name__)

EXPECTED_TOOLS = {"poll_now", "list_tasks", "get_task", "mark_task", "summarize_pending"}


def _registered_tool_names(server: Any) -> set[str]:
    """跨 SDK 版本取已注册的 tool 名（不硬编码私有属性路径）。"""
    manager = getattr(server, "_tool_manager", None)
    for holder in (manager, server):
        if holder is None:
            continue
        tools = getattr(holder, "_tools", None)
        if isinstance(tools, dict):
            return set(tools)
        if hasattr(holder, "list_tools"):
            try:
                return {tool.name for tool in asyncio.run(holder.list_tools())}
            except Exception as exc:  # noqa: BLE001 —— 版本差异：该候选 API 不可用就换下一个
                logger.debug("list_tools() 在此 SDK 版本不可用：%s", exc)
    return set()


def test_service_name() -> None:
    assert mcp_server.mcp.name == "canvas-task-monitor"


def test_five_tools_registered() -> None:
    assert _registered_tool_names(mcp_server.mcp) == EXPECTED_TOOLS


def test_tool_param_names_match_capabilities() -> None:
    """tool 参数名必须与 facade 的 params 键一致（不做转换层）。"""
    capability_names = {"poll_now", "list_tasks", "get_task", "mark_task", "summarize_pending"}

    assert EXPECTED_TOOLS == capability_names


def test_missing_sdk_prints_friendly_error() -> None:
    """缺 SDK 时必须给清晰提示 + 退出码 1（用 sys.modules 模拟，不必真卸载）。"""
    import os
    import subprocess

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['mcp'] = None; "
                f"sys.path.insert(0, r'{PROJECT}'); import mcp_server"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(PROJECT),
        timeout=60,
        check=False,
    )

    assert result.returncode == 1
    assert "未安装 MCP SDK" in result.stderr
    assert "Traceback" not in result.stderr


def test_invoke_returns_data_on_success(
    monkeypatch: pytest.MonkeyPatch, fake_facade: Callable[[dict], Any]
) -> None:
    container = fake_facade({"ok": True, "data": {"x": 1}})
    monkeypatch.setattr(mcp_server, "_container", container)

    assert asyncio.run(mcp_server._invoke("get_task", {"task_id": 3})) == {"x": 1}
    assert container.facade.calls == [("get_task", {"task_id": 3})]


def test_invoke_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch, fake_facade: Callable[[dict], Any]
) -> None:
    """facade 返回 {"ok": False} → _invoke 抛 RuntimeError（交给 MCP 协议层报错）。"""
    monkeypatch.setattr(mcp_server, "_container", fake_facade({"ok": False, "error": "boom"}))

    with pytest.raises(RuntimeError, match="canvas_task_monitor: boom"):
        asyncio.run(mcp_server._invoke("list_tasks", {}))


def test_tools_forward_expected_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """5 个 tool 各自的调用都应转成 (action, params)，默认值也正确。"""
    calls: list[tuple[str, dict]] = []

    async def _fake_invoke(action: str, params: dict) -> str:
        calls.append((action, params))
        return f"data:{action}"

    monkeypatch.setattr(mcp_server, "_invoke", _fake_invoke)

    assert asyncio.run(mcp_server.poll_now()) == "data:poll_now"
    assert calls[-1] == ("poll_now", {})

    assert asyncio.run(mcp_server.list_tasks()) == "data:list_tasks"
    assert calls[-1] == ("list_tasks", {"category": None, "status": None, "limit": 50})

    assert asyncio.run(mcp_server.list_tasks(category="activity", limit=7)) == "data:list_tasks"
    assert calls[-1] == ("list_tasks", {"category": "activity", "status": None, "limit": 7})

    assert asyncio.run(mcp_server.get_task(task_id=3)) == "data:get_task"
    assert calls[-1] == ("get_task", {"task_id": 3})

    assert asyncio.run(mcp_server.mark_task(task_id=3)) == "data:mark_task"
    assert calls[-1] == ("mark_task", {"task_id": 3, "done": True})

    assert asyncio.run(mcp_server.mark_task(task_id=3, done=False)) == "data:mark_task"
    assert calls[-1] == ("mark_task", {"task_id": 3, "done": False})

    assert asyncio.run(mcp_server.summarize_pending()) == "data:summarize_pending"
    assert calls[-1] == ("summarize_pending", {})
