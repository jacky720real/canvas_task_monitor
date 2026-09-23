"""Web 适配器测试。

【为什么单独成文件】
web_main.py 是一个独立入口，它有自己的路由契约（4 个端点 + 1 个禁用端点）。
这些契约不能只靠临时脚本验证——按项目惯例，临时脚本最终会清理。
本文件把这些契约固化为长期测试。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from canvas_task_monitor.core.models import TaskItem
from canvas_task_monitor.services.bootstrap import Container

PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT / "config" / "templates" / "task_extract_template.yaml"

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import web_main  # 入口文件在项目根，需先把根加进 sys.path（本项目未启用 E402，无需 noqa）


def _request(url: str, method: str = "GET") -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _request_http_error(url: str, method: str = "GET") -> tuple[int, dict[str, Any]]:
    """预期 4xx 的请求：把错误响应体也读出来。"""
    request = urllib.request.Request(url, method=method)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    error = excinfo.value
    return error.code, json.loads(error.read().decode("utf-8"))


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """起真实 HTTP 服务（随机端口 port=0）+ 真实 Container（临时 DB、预填 2 条任务）。"""
    settings = {
        "app": {"db_path": str(tmp_path / "web.db"), "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": [], "jitter_seconds": 30},
        "canvas": {"base_url": "", "token": ""},
        "mail": {"provider": "graph", "graph": {}, "imap": {}},
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")

    container = Container(settings_path)
    container.tasks_repo.upsert(
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
    container.tasks_repo.upsert(
        TaskItem(
            source="canvas_announcement",
            external_id="course:1:announcement:2",
            category="reminder",
            title="考试规则",
            urgency=1,
            importance=1,
            score=18,
        )
    )
    monkeypatch.setattr(web_main, "_container", container)

    server = ThreadingHTTPServer(("127.0.0.1", 0), web_main.WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        asyncio.run(container.aclose())


def test_web_server_starts(web_server: str) -> None:
    with urllib.request.urlopen(web_server + "/", timeout=5) as response:
        html = response.read().decode("utf-8")

    assert response.status == 200
    assert "任务清单" in html
    assert "cardHTML" in html and "esc(" in html  # 前端模板与转义函数都在


def test_list_tasks_endpoint(web_server: str) -> None:
    status, payload = _request(web_server + "/api/tasks")

    assert status == 200
    assert payload["ok"] is True
    assert len(payload["data"]) == 2

    row = payload["data"][0]
    assert isinstance(row["tags"], list)
    assert "tags_json" not in row and "raw_json" not in row


def test_filter_by_category(web_server: str) -> None:
    _, payload = _request(web_server + "/api/tasks?category=reminder")

    assert [task["title"] for task in payload["data"]] == ["考试规则"]


def test_mark_task_endpoint(web_server: str) -> None:
    task_id = _request(web_server + "/api/tasks")[1]["data"][0]["id"]

    _, marked = _request(f"{web_server}/api/tasks/{task_id}/done", method="POST")
    assert marked["ok"] is True and marked["data"]["status"] == "done"
    assert _request(f"{web_server}/api/tasks/{task_id}")[1]["data"]["status"] == "done"

    _, unmarked = _request(f"{web_server}/api/tasks/{task_id}/undone", method="POST")
    assert unmarked["data"]["status"] == "pending"


def test_unknown_path_returns_404(web_server: str) -> None:
    status, payload = _request_http_error(web_server + "/api/nonexistent")

    assert status == 404
    assert payload["ok"] is False
    assert "error" in payload


def test_poll_endpoint_not_exposed(web_server: str) -> None:
    """轮询会真调外部 API 并烧 token，绝不能在浏览器里一键触发。"""
    status, payload = _request_http_error(web_server + "/api/poll", method="POST")

    assert status == 404
    assert payload["ok"] is False
