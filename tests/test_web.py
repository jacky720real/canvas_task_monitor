"""Web 适配器测试。

【为什么单独成文件】
web_main.py 是一个独立入口，它有自己的路由契约（4 个端点 + 1 个禁用端点）。
这些契约不能只靠临时脚本验证——按项目惯例，临时脚本最终会清理。
本文件把这些契约固化为长期测试。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
import urllib.error
import urllib.parse
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

import setup_config  # 同上：根级配置工具
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
    settings_path = tmp_path / "config" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
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
    # 注入"已配置 + 正常模式"的进程状态；路径全部指向临时目录，
    # 免得测试读到（或写到）开发机上真实的 .env / data/state.json
    monkeypatch.setattr(
        web_main,
        "_state",
        web_main._AppState(
            container=container,
            setup_mode=False,
            settings_path=settings_path,
            env_path=tmp_path / ".env",
            # 与 load_current_config 内部推导的一致（<settings 上两级>/data/state.json）
            state_path=setup_config.state_path_for(settings_path),
        ),
    )

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


def test_poll_endpoint_in_setup_mode_is_blocked(setup_mode_server: str) -> None:
    """配置模式下不能触发拉取：与其它业务 API 一样返回 503（统一语义）。"""
    status, payload = _request_http_error(setup_mode_server + "/api/poll", method="POST")

    assert status == 503
    assert payload["ok"] is False


def test_poll_endpoint_success(web_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/poll 必须走 facade.invoke("poll_now")，并把统计原样回给页面。"""
    stats = {"sources": 1, "changes": 3, "tasks": 2, "llm_calls": 1}
    calls: list[tuple[str, dict]] = []

    async def fake_invoke(action: str, params: dict) -> dict:
        calls.append((action, params))
        return {"ok": True, "data": stats}

    monkeypatch.setattr(web_main._state.container.facade, "invoke", fake_invoke)

    status, payload = _request(web_server + "/api/poll", method="POST")

    assert status == 200
    assert payload == {"ok": True, "data": stats}
    assert calls == [("poll_now", {})]


@pytest.fixture
def setup_mode_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """起真实 HTTP 服务，但把进程状态置为"未配置"（配置模式）。"""
    monkeypatch.setattr(
        web_main,
        "_state",
        web_main._AppState(
            container=None,
            setup_mode=True,
            settings_path=tmp_path / "config" / "settings.yaml",
            env_path=tmp_path / ".env",
            state_path=tmp_path / "data" / "state.json",
        ),
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), web_main.WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_setup_mode_redirects_root_to_setup(setup_mode_server: str) -> None:
    """没配好时 / 必须 302 到 /setup：用户不该先看到一个空任务列表。"""
    parsed = urllib.parse.urlsplit(setup_mode_server)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/setup"
    finally:
        connection.close()


def test_setup_mode_blocks_api(setup_mode_server: str) -> None:
    """配置模式下业务 API 一律 503（前端此时只该看到配置向导）。"""
    status, payload = _request_http_error(setup_mode_server + "/api/tasks")
    assert status == 503
    assert payload["ok"] is False and "未配置" in payload["error"]

    posted, marked = _request_http_error(f"{setup_mode_server}/api/tasks/1/done", method="POST")
    assert posted == 503 and marked["ok"] is False


def test_setup_page_accessible(setup_mode_server: str) -> None:
    """配置向导页本身必须能打开（它就是未配置时的默认落地页）。"""
    with urllib.request.urlopen(setup_mode_server + "/setup", timeout=5) as response:
        html = response.read().decode("utf-8")

    assert response.status == 200
    assert "欢迎使用" in html
    assert 'id="finish"' in html  # "完成配置，开始使用"
    assert html.count('type="password"') >= 3  # Canvas Token / API Key / IMAP 密码都不外显
    assert "test-canvas" in html and "test-ai" in html  # 两个"测试连接"按钮


def test_get_setup_current_endpoint(web_server: str) -> None:
    """设置页预填 + 首页 banner 都读这个端点；必须可用且不回敏感明文。"""
    # 制造一条"上次 Canvas 测试失败"的记录（CLI 写的就是同一个 state.json）
    setup_config.record_test("canvas", False, "401: 令牌无效", web_main._state.state_path)

    status, payload = _request(web_server + "/api/setup/current")

    assert status == 200
    assert payload["ok"] is True

    data = payload["data"]
    assert set(data["canvas"]) == {"base_url", "token_saved"}  # 不回明文
    assert data["canvas"]["token_saved"] is False  # 临时目录里没有 .env
    assert data["last_test"]["canvas_ok"] is False
    assert data["last_test"]["canvas_error"] == "401: 令牌无效"
    assert data["last_test"]["tested_at"]  # 带了时间戳（banner 要显示）


