"""Canvas 连接器：Link header 分页 / 429 重试 / 单课程失败隔离 / payload 白名单对齐。

用 httpx.MockTransport 注入假响应：不触网、不依赖真实 Canvas。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from canvas_task_monitor.connectors.canvas import CanvasConnector
from canvas_task_monitor.core.hashing import canonical_hash, hash_fields

BASE = "https://canvas.test"


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _assignment(assignment_id: int, **overrides: object) -> dict:
    data = {
        "id": assignment_id,
        "name": f"作业{assignment_id}",
        "description": "d",
        "due_at": _iso(3),
        "points_possible": 100,
        "submission_types": ["online_upload"],
    }
    data.update(overrides)
    return data


def _build(
    handler: Callable[[httpx.Request], httpx.Response], **cfg: object
) -> CanvasConnector:
    """构造连接器并注入 MockTransport（速率调高、退避置 0，测试才跑得快）。"""
    config: dict = {
        "base_url": BASE,
        "token": "tok",
        "rate_limit_rps": 1000,
        "lookback_days": 30,
        "retry": {"max_attempts": 3, "backoff_base": 0},
    }
    config.update(cfg)
    connector = CanvasConnector(config)
    connector._client = httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    return connector


async def test_follows_link_header_pagination() -> None:
    """必须解析 Link header 的 rel="next"；只拉第一页会漏数据。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/courses":
            if "page=2" in str(request.url):
                return httpx.Response(200, json=[{"id": 2, "name": "有机化学"}])
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "机器学习"}],
                headers={"Link": f'<{BASE}/api/v1/courses?page=2>; rel="next"'},
            )
        return httpx.Response(200, json=[])

    connector = _build(handler)
    await connector.fetch()

    assert seen.count("/api/v1/courses") == 2  # 第一页 + 跟随 next
    assert "/api/v1/courses/1/assignments" in seen
    assert "/api/v1/courses/2/assignments" in seen  # 第二页的课程也被处理
    await connector.aclose()


async def test_retries_on_429_and_honours_zero_retry_after() -> None:
    """429 要重试，且 Retry-After: 0 必须被照办（0 是合法值，不能被 or 短路吞掉）。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "机器学习"}])
        if request.url.path.endswith("/assignments"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})
            return httpx.Response(200, json=[_assignment(11)])
        return httpx.Response(200, json=[])

    connector = _build(handler)
    items = await connector.fetch()

    assert attempts["n"] == 2
    assert [item.external_id for item in items] == ["course:1:assignment:11"]
    await connector.aclose()


async def test_single_course_failure_is_isolated() -> None:
    """单个课程 404 只跳过它，不能让整轮 fetch 挂掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "机器学习"}, {"id": 9, "name": "坏课程"}])
        if path == "/api/v1/courses/9/assignments":
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        if path == "/api/v1/courses/1/assignments":
            return httpx.Response(200, json=[_assignment(11)])
        return httpx.Response(200, json=[])

    connector = _build(handler)
    items = await connector.fetch()

    assert [item.external_id for item in items] == ["course:1:assignment:11"]
    await connector.aclose()


async def test_payload_keys_align_with_hash_whitelist() -> None:
    """★ 跨层契约：连接器产出的 payload 必须能被 hashing 白名单直接消化。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "机器学习"}])
        if request.url.path == "/api/v1/courses/1/assignments":
            return httpx.Response(200, json=[_assignment(11)])
        if request.url.path == "/api/v1/announcements":
            return httpx.Response(200, json=[{"id": 21, "title": "考试范围", "message": "第1-5章", "posted_at": _iso(-1)}])
        return httpx.Response(200, json=[])

    connector = _build(handler)
    items = await connector.fetch()

    assert {item.source for item in items} == {"canvas_assignment", "canvas_announcement"}
    for item in items:
        assert canonical_hash(item.source, item.payload)  # 未登记 source 会抛 ValueError
        assert set(item.payload) - set(hash_fields(item.source)) <= {"course_name"}
    await connector.aclose()


async def test_keeps_items_without_or_with_broken_due_at() -> None:
    """无 due_at / 解析失败都必须保留（老师可能先发作业后补截止时间）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "机器学习"}])
        if request.url.path == "/api/v1/courses/1/assignments":
            return httpx.Response(
                200,
                json=[
                    _assignment(11, due_at=None),
                    _assignment(12, due_at="not-a-date"),
                    _assignment(13, due_at=_iso(-90)),  # 这条确实过期 → 应被过滤
                ],
            )
        return httpx.Response(200, json=[])

    connector = _build(handler)
    items = await connector.fetch()

    assert sorted(item.external_id for item in items) == [
        "course:1:assignment:11",
        "course:1:assignment:12",
    ]
    await connector.aclose()
