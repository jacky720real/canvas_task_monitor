"""CLI 适配器：真跑子进程，验证退出码与关键输出。

注意：断言只基于"输出里包含关键内容"（字符串），
不依赖 rich 的具体渲染字符（边框/图标会随版本变化）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from canvas_task_monitor.core.models import TaskItem
from canvas_task_monitor.storage.db import Database
from canvas_task_monitor.storage.task_repo import TaskRepo

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "cli_main.py"
TEMPLATE = PROJECT / "config" / "templates" / "task_extract_template.yaml"


def _settings_yaml(tmp_path: Path, *, sources: list[str] | None = None, db_name: str = "tasks.db",
                   canvas: dict | None = None) -> Path:
    import yaml

    data = {
        "app": {"db_path": str(tmp_path / db_name), "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": sources or [], "jitter_seconds": 30},
        "canvas": canvas or {"base_url": "", "token": ""},
        "mail": {"provider": "graph", "graph": {}, "imap": {}},
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _run(*argv: str, settings: Path | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(CLI)]
    if settings is not None:
        args += ["--settings", str(settings)]
    args += list(argv)
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "COLUMNS": "200",
    }
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(PROJECT),
        timeout=60,
        check=False,
    )


def test_version_flag() -> None:
    result = _run("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == "canvas-task-monitor 1.0.0"


def test_unknown_subcommand_exits_with_2() -> None:
    assert _run("frobnicate").returncode == 2


def test_missing_config_exits_with_2(tmp_path: Path) -> None:
    """缺凭证 → 退出码 2 + 友好提示（不是 traceback）。"""
    settings = _settings_yaml(tmp_path, sources=["canvas"])

    result = _run("list", settings=settings)

    assert result.returncode == 2
    assert "配置错误" in result.stdout
    assert "canvas.base_url" in result.stdout


def test_empty_list_shows_placeholder_panel(tmp_path: Path) -> None:
    """无数据打印 (空) 面板，而不是空表格。"""
    result = _run("list", settings=_settings_yaml(tmp_path))

    assert result.returncode == 0
    assert "(空)" in result.stdout
    assert "任务清单" in result.stdout


def test_list_renders_task_content(tmp_path: Path) -> None:
    settings = _settings_yaml(tmp_path, db_name="data.db")
    db = Database(str(tmp_path / "data.db"))
    repo = TaskRepo(db)
    repo.upsert(
        TaskItem(
            source="canvas_assignment",
            external_id="course:1:assignment:1",
            category="assignment",
            title="论文初稿",
            course="机器学习",
            urgency=5,
            importance=3,
            score=74,
            tags=["paper"],
            urgency_reason="今天截止",
            importance_reason="占比 20%",
        )
    )
    db.close()

    result = _run("list", settings=settings)

    assert result.returncode == 0
    assert "论文初稿" in result.stdout
    assert "作业" in result.stdout  # 类别已中文化
    assert "74" in result.stdout  # 分数列


def test_done_and_undone_round_trip(tmp_path: Path) -> None:
    settings = _settings_yaml(tmp_path, db_name="done.db")
    db = Database(str(tmp_path / "done.db"))
    task_id = TaskRepo(db).upsert(
        TaskItem(
            source="canvas_assignment",
            external_id="course:1:assignment:1",
            category="activity",
            title="小组讨论",
            urgency=2,
            importance=1,
        )
    )
    db.close()

    marked = _run("done", str(task_id), settings=settings)
    assert marked.returncode == 0
    assert "done" in marked.stdout

    unmarked = _run("undone", str(task_id), settings=settings)
    assert unmarked.returncode == 0
    assert "pending" in unmarked.stdout


def test_missing_task_exits_with_1(tmp_path: Path) -> None:
    result = _run("done", "9999", settings=_settings_yaml(tmp_path, db_name="missing.db"))

    assert result.returncode == 1
    assert "未找到任务 9999" in result.stdout
