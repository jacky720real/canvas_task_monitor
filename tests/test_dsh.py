"""DSH 适配器骨架：Protocol 结构匹配 / 幂等 / 自检块退出码与零副作用。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from canvas_task_monitor.contracts.plugin import PLUGIN_API_VERSION, PluginFacade

PROJECT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = PROJECT / "dsh_plugin.py"
TEMPLATE = PROJECT / "config" / "templates" / "task_extract_template.yaml"

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import dsh_plugin


def _write_settings(
    directory: Path,
    *,
    sources: list[str] | None = None,
    canvas: dict[str, Any] | None = None,
) -> None:
    """在 directory/config/settings.yaml 写一份配置（自检块默认读相对路径）。"""
    config_dir = directory / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "app": {"db_path": str(directory / "tasks.db"), "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": sources or [], "jitter_seconds": 30},
        "canvas": canvas or {"base_url": "", "token": ""},
        "mail": {"provider": "graph", "graph": {}, "imap": {}},
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def _run_self_check(cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(PLUGIN_FILE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd),
        timeout=60,
        check=False,
    )


def test_plugin_satisfies_plugin_facade_protocol() -> None:
    """★ 结构化匹配：无需显式继承，isinstance 即可验证契约。"""
    plugin = dsh_plugin.CanvasTaskMonitorDSHPlugin()

    assert isinstance(plugin, PluginFacade)
    assert plugin.api_version == PLUGIN_API_VERSION
    assert plugin.name == "canvas_task_monitor"


def test_container_is_lazy() -> None:
    """DSH 可能长时间不调用插件 → 构造时不该装配 Container。"""
    assert dsh_plugin.CanvasTaskMonitorDSHPlugin()._container is None


def test_shutdown_is_idempotent() -> None:
    plugin = dsh_plugin.CanvasTaskMonitorDSHPlugin()

    assert asyncio.run(plugin.shutdown()) is None
    assert asyncio.run(plugin.shutdown()) is None
    assert plugin._container is None


def test_dsh_sdk_probe_is_boolean() -> None:
    """用 find_spec 探测 SDK；缺 SDK 也不能阻塞导入（骨架阶段预期 False）。"""
    assert isinstance(dsh_plugin._HAS_DSH_SDK, bool)


def test_self_check_reports_missing_config_without_side_effect(tmp_path: Path) -> None:
    """缺配置 → 退出码 2 + 友好提示，且**不创建数据库**（预检前移的效果）。"""
    _write_settings(tmp_path, sources=["canvas"], canvas={"base_url": "", "token": ""})

    result = _run_self_check(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode == 2
    assert "capabilities() 失败" in output
    assert "请检查 .env / config/settings.yaml" in output
    assert not (tmp_path / "tasks.db").exists()


def test_self_check_runs_without_dsh_sdk(tmp_path: Path) -> None:
    """DSH 缺席时自检块仍能验证适配器：打印 5 项能力 + summarize_pending 结果。"""
    _write_settings(tmp_path, sources=[])

    result = _run_self_check(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "capabilities = 5 项" in output
    for name in ("poll_now", "list_tasks", "get_task", "mark_task", "summarize_pending"):
        assert name in output
    assert "summarize_pending" in output
    assert "Traceback" not in output
