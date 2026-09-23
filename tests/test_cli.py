"""CLI 适配器：真跑子进程，验证退出码与关键输出。

注意：断言只基于"输出里包含关键内容"（字符串），
不依赖 rich 的具体渲染字符（边框/图标会随版本变化）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from canvas_task_monitor.core.models import TaskItem
from canvas_task_monitor.storage.db import Database
from canvas_task_monitor.storage.task_repo import TaskRepo

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "cli_main.py"
TEMPLATE = PROJECT / "config" / "templates" / "task_extract_template.yaml"


def _settings_yaml(tmp_path: Path, *, sources: list[str] | None = None, db_name: str = "tasks.db",
                   canvas: dict | None = None, config_dir: bool = False) -> Path:
    import yaml

    data = {
        "app": {"db_path": str(tmp_path / db_name), "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": sources or [], "jitter_seconds": 30},
        "canvas": canvas or {"base_url": "", "token": ""},
        "mail": {"provider": "graph", "graph": {}, "imap": {}},
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }
    # 真实布局是 <root>/config/settings.yaml（.env 与 data/ 都在 <root> 下）；
    # 需要 CLI 读写 .env / state.json 的用例把它放进 config/ 子目录。
    base = tmp_path / "config" if config_dir else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    path = base / "settings.yaml"
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


class _UnauthorizedCanvasHandler(BaseHTTPRequestHandler):
    """永远返回 401 的假 Canvas（只在本地回环，不碰真网络）。"""

    def log_message(self, *args: object) -> None:
        """静默访问日志。"""

    def do_GET(self) -> None:
        body = b'{"errors":[{"message":"Invalid access token"}]}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_poll_401_prints_token_hint(tmp_path: Path) -> None:
    """token 失效时 poll 要告诉用户"去哪修"，并把失败写进 data/state.json。

    【为什么用假 Canvas 而不是 mock】
    poll 内部会吞掉单源异常（刻意的：一个源挂了不该让整轮失败），所以"401 到底能不能
    被识别出来"只有真发一次请求才测得准。这里用本地 401 假服务，既真实又不碰外网。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UnauthorizedCanvasHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        settings = _settings_yaml(
            tmp_path,
            sources=["canvas"],
            config_dir=True,
            canvas={"base_url": base_url, "token": "expired", "retry": {"max_attempts": 1}},
        )
        # CLI 的 token 体检优先读 .env（配置向导写的就是它），所以这里也放一份
        (tmp_path / ".env").write_text(
            f"CANVAS_BASE_URL={base_url}\nCANVAS_TOKEN=expired\n", encoding="utf-8"
        )

        result = _run("poll", settings=settings)
    finally:
        server.shutdown()
        server.server_close()

    assert "token 可能已过期" in result.stdout
    assert "8765/setup" in result.stdout  # 告诉用户去哪修

    state = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert state["last_canvas_test"]["ok"] is False
    assert "401" in state["last_canvas_test"]["error"]

