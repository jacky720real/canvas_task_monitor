"""Canvas LMS 连接器。

【要点】
- Bearer token 鉴权，httpx.AsyncClient 异步请求。
- 必须解析 Link header 的 rel="next" 完成分页（只拉第一页会漏数据）。
- 429 / 5xx / 网络异常按指数退避重试，最多 max_attempts 次；429 优先用 Retry-After。
- 单个课程失败（403 / 404 / 超时）只记录日志并 continue，绝不让整轮 fetch 挂掉。
- 每次出站请求前都要 await bucket.acquire()，桶由本连接器独享。

【字段名约束】
canvas_assignment 的 data 键必须与 core/hashing.py 的白名单一致
（name / description / due_at / points_possible / submission_types），
announcement 对应（title / message / posted_at）。
course_name 是额外附带的上下文，不参与哈希。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..core.models import RawItem
from .base import BaseConnector

logger = logging.getLogger(__name__)

SOURCE_ASSIGNMENT = "canvas_assignment"
SOURCE_ANNOUNCEMENT = "canvas_announcement"

_COURSES_PATH = "/api/v1/courses"
_ANNOUNCEMENTS_PATH = "/api/v1/announcements"
_PAGE_SIZE = 100
# 形如 <https://x/api/v1/courses?page=2>; rel="next"
_NEXT_LINK_PATTERN = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
class CanvasConnector(BaseConnector):
    """Canvas LMS 连接器。"""

    name = "canvas"

    def __init__(self, cfg: dict[str, Any]) -> None:
        """按 settings.yaml 的 canvas 段构造。

        传整段配置（而不是一长串位置参数）的理由：以后加字段只改 settings.yaml，
        连接器侧用 .get(..., 默认值) 兜底，不必再去改调用方与构造函数签名。
        与 graph_mail.py / imap_mail.py 保持同一风格。
        """
        super().__init__(rate_limit_rps=float(cfg.get("rate_limit_rps", 3)))
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.token = str(cfg.get("token", ""))
        self.timeout = float(cfg.get("timeout", 20))
        self.lookback_days = int(cfg.get("lookback_days", 30))
        retry = cfg.get("retry") or {}
        self.max_attempts = max(int(retry.get("max_attempts", 3)), 1)
        self.backoff_base = float(retry.get("backoff_base", 1.5))
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        """惰性创建并复用 AsyncClient（避免每次请求都重建连接池）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            )
        return self._client

    async def fetch(self) -> list[RawItem]:
        """拉取所有在读课程近 lookback_days 内的作业与公告。"""
        courses = await self._get_paginated(
            _COURSES_PATH, {"enrollment_state": "active", "per_page": _PAGE_SIZE}
        )
        items: list[RawItem] = []
        for course in courses:
            course_id = course.get("id")
            if course_id is None:
                continue
            course_name = str(course.get("name") or course.get("course_code") or course_id)
            try:
                items.extend(await self._fetch_assignments(course_id, course_name))
                items.extend(await self._fetch_announcements(course_id, course_name))
            except Exception as exc:  # noqa: BLE001 —— 故意广catch：单课程失败不能让整轮 fetch 挂掉
                # 单个课程失败（403 / 404 / 超时）只记录日志并继续
                logger.error("课程 %s（%s）拉取失败，跳过：%s", course_id, course_name, exc)
                continue
        logger.info("Canvas 拉取完成：%d 条（近 %d 天）", len(items), self.lookback_days)
        return items

    async def _fetch_assignments(self, course_id: Any, course_name: str) -> list[RawItem]:
        """拉取单个课程的作业，只保留截止时间在回溯窗口内的条目。"""
        raw = await self._get_paginated(
            f"/api/v1/courses/{course_id}/assignments", {"per_page": _PAGE_SIZE}
        )
        cutoff = _utc_now() - timedelta(days=self.lookback_days)
        items: list[RawItem] = []
        for assignment in raw:
            # 只有"确实早于窗口"的才跳过；无 due_at / 解析失败一律保留。
            # 理由：老师可能先发作业、后补截止时间，一开始就丢弃会让这条作业永远丢失，
            # 且后补时无从对比（本地没有基线快照）。
            if _is_before_lookback(assignment.get("due_at"), cutoff):
                continue
            assignment_id = assignment.get("id")
            items.append(
                RawItem(
                    source=SOURCE_ASSIGNMENT,
                    external_id=f"course:{course_id}:assignment:{assignment_id}",
                    course_id=str(course_id),
                    payload={
                        "name": assignment.get("name") or "",
                        "description": assignment.get("description") or "",
                        "due_at": assignment.get("due_at"),
                        "points_possible": assignment.get("points_possible"),
                        "submission_types": assignment.get("submission_types") or [],
                        "course_name": course_name,
                    },
                )
            )
        return items

    async def _fetch_announcements(self, course_id: Any, course_name: str) -> list[RawItem]:
        """拉取单个课程的公告，只保留发布时间在回溯窗口内的条目。"""
        raw = await self._get_paginated(
            _ANNOUNCEMENTS_PATH,
            {"context_codes[]": f"course_{course_id}", "per_page": _PAGE_SIZE},
        )
        cutoff = _utc_now() - timedelta(days=self.lookback_days)
        items: list[RawItem] = []
        for announcement in raw:
            # 与作业同理：posted_at 为空或解析失败时保留，不丢弃
            if _is_before_lookback(announcement.get("posted_at"), cutoff):
                continue
            announcement_id = announcement.get("id")
            items.append(
                RawItem(
                    source=SOURCE_ANNOUNCEMENT,
                    external_id=f"course:{course_id}:announcement:{announcement_id}",
                    course_id=str(course_id),
                    payload={
                        "title": announcement.get("title") or "",
                        "message": announcement.get("message") or "",
                        "posted_at": announcement.get("posted_at"),
                        "course_name": course_name,
                    },
                )
            )
        return items

    async def _get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """按 Link header 的 rel="next" 逐页取完，返回合并后的条目列表。"""
        results: list[dict[str, Any]] = []
        next_url: str | None = path
        current_params = params
        while next_url:
            response = await self._request(next_url, current_params)
            payload = response.json()
            if isinstance(payload, list):
                results.extend(payload)
            elif isinstance(payload, dict):
                results.append(payload)
            next_url = _next_link(response.headers.get("Link"))
            # next 链接自带完整查询串，后续页不能再叠加 params
            current_params = None
        return results

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """发一次 GET；429 / 5xx / 网络异常按指数退避重试，其它 4xx 直接抛出。"""
        client = await self._http()
        for attempt in range(1, self.max_attempts + 1):
            await self.bucket.acquire()
            retry_wait: float | None = None
            try:
                response = await client.get(path, params=params)
            except httpx.HTTPError as exc:
                retry_wait = self._backoff_seconds(attempt)
                logger.warning(
                    "Canvas 请求异常（第 %d/%d 次）：%s -> %s", attempt, self.max_attempts, path, exc
                )
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    # Retry-After: 0 也要照办，不能用 or 短路（0.0 是假值）
                    retry_after = _retry_after_seconds(response)
                    retry_wait = (
                        retry_after if retry_after is not None else self._backoff_seconds(attempt)
                    )
                    logger.warning(
                        "Canvas 返回 %d（第 %d/%d 次），%.1fs 后重试：%s",
                        response.status_code,
                        attempt,
                        self.max_attempts,
                        retry_wait,
                        path,
                    )
                else:
                    response.raise_for_status()
                    return response
            if retry_wait is not None and attempt < self.max_attempts:
                await asyncio.sleep(retry_wait)
        raise RuntimeError(f"Canvas 请求重试 {self.max_attempts} 次后仍失败：{path}")

    def _backoff_seconds(self, attempt: int) -> float:
        """指数退避秒数。"""
        return float(self.backoff_base**attempt)


def _next_link(link_header: str | None) -> str | None:
    """从 Link header 中取出 rel="next" 的 URL；没有则返回 None。"""
    if not link_header:
        return None
    match = _NEXT_LINK_PATTERN.search(link_header)
    return match.group(1) if match else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """读取 Retry-After（秒）；缺失或非法时返回 None。"""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _parse_iso(value: Any) -> datetime | None:
    """宽松解析 ISO 8601 时间串（兼容 Z 结尾）；失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_before_lookback(value: Any, cutoff: datetime) -> bool:
    """时间字段是否**确定**早于回溯窗口。

    无值（None / 空串）或解析失败一律返回 False，即"不确定就不丢弃"，
    保证"先发作业、后补 due_at"的条目不会被永久漏掉。
    """
    parsed = _parse_iso(value)
    return parsed is not None and parsed < cutoff


def _utc_now() -> datetime:
    """当前 UTC 时间（单独抽出便于测试替换）。"""
    return datetime.now(timezone.utc)


