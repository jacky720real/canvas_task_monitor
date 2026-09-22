"""插件 Facade 实现：业务层唯一对外出口。

对应 contracts/plugin.py 的 PluginFacade 协议。
所有入口（CLI / MCP / DSH）拿到 Container 后，通过 container.facade 调用。

职责边界：**能力路由 + DTO 转换**。业务逻辑在 TaskService / Poller 里，
本文件不做任何业务判断。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..contracts.plugin import PLUGIN_API_VERSION, Capability
from ..interfaces.dto import task_row_to_dict

if TYPE_CHECKING:  # 只为类型标注：Container 与本文件互相引用，运行时避免循环导入
    from .bootstrap import Container

logger = logging.getLogger(__name__)

CAPABILITY_POLL_NOW = "poll_now"
CAPABILITY_LIST_TASKS = "list_tasks"
CAPABILITY_GET_TASK = "get_task"
CAPABILITY_MARK_TASK = "mark_task"
CAPABILITY_SUMMARIZE_PENDING = "summarize_pending"

_SUMMARY_LIMIT = 1000

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: 业务层对外暴露的 5 项能力（契约层 Capability 的实例）
_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name=CAPABILITY_POLL_NOW,
        description="立即触发一次轮询，返回本次拉取/变更/新建任务的统计。",
        params_schema=_EMPTY_PARAMS,
        returns_schema={
            "type": "object",
            "properties": {
                "sources": {"type": "integer"},
                "changes": {"type": "integer"},
                "tasks": {"type": "integer"},
                "llm_calls": {"type": "integer"},
            },
        },
    ),
    Capability(
        name=CAPABILITY_LIST_TASKS,
        description="列出任务清单，可按类别和状态过滤。",
        params_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": ["string", "null"],
                    "enum": ["assignment", "activity", "reminder", None],
                },
                "status": {"type": ["string", "null"], "enum": ["pending", "done", None]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
        },
        returns_schema={"type": "array", "items": {"type": "object"}},
    ),
    Capability(
        name=CAPABILITY_GET_TASK,
        description="按 ID 获取任务详情。",
        params_schema={
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "integer"}},
        },
        returns_schema={"type": ["object", "null"]},
    ),
    Capability(
        name=CAPABILITY_MARK_TASK,
        description="把任务标记为完成或未完成。",
        params_schema={
            "type": "object",
            "required": ["task_id", "done"],
            "properties": {"task_id": {"type": "integer"}, "done": {"type": "boolean"}},
        },
        returns_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "task_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["pending", "done"]},
            },
        },
    ),
    Capability(
        name=CAPABILITY_SUMMARIZE_PENDING,
        description="汇总未完成任务：总数、按类别分布、最高紧迫度。",
        params_schema=_EMPTY_PARAMS,
        returns_schema={
            "type": "object",
            "properties": {
                "total": {"type": "integer"},
                "assignment": {"type": "integer"},
                "activity": {"type": "integer"},
                "reminder": {"type": "integer"},
                "max_urgency": {"type": "integer", "minimum": 0, "maximum": 5},
            },
        },
    ),
)

class CanvasTaskMonitorFacade:
    """业务层唯一对外出口，实现 contracts/plugin.py 的 PluginFacade 协议。"""

    name: str = "canvas_task_monitor"
    version: str = "0.1.0"
    api_version: str = PLUGIN_API_VERSION

    def __init__(self, container: Container) -> None:
        self._c = container

    async def health(self) -> dict:
        """轻量探活：不实际调 LLM / HTTP，仅检查 DB 可读。"""
        try:
            self._c.tasks_repo.list(limit=1)
            return {
                "status": "ok",
                "detail": {"db": "readable", "connectors": len(self._c.connectors)},
            }
        except Exception as exc:  # noqa: BLE001 —— 探活接口本身绝不能抛异常
            return {"status": "degraded", "detail": {"db": "error", "error": str(exc)}}

    async def capabilities(self) -> list[Capability]:
        """返回本插件暴露的全部能力。"""
        return list(_CAPABILITIES)

    async def invoke(self, action: str, params: dict[str, Any]) -> dict:
        """统一调用入口。

        返回格式统一为：
        - 成功：{"ok": True, "data": <具体返回>}
        - 失败：{"ok": False, "error": "<消息>"}

        未知 action 不抛异常，返回 {"ok": False, "error": "unknown action: xxx"}。
        """
        try:
            if action == CAPABILITY_POLL_NOW:
                data = await self._c.poller.poll_once()
            elif action == CAPABILITY_LIST_TASKS:
                data = await self._list_tasks(params)
            elif action == CAPABILITY_GET_TASK:
                data = await self._get_task(params)
            elif action == CAPABILITY_MARK_TASK:
                data = await self._mark_task(params)
            elif action == CAPABILITY_SUMMARIZE_PENDING:
                data = await self._summarize_pending()
            else:
                return {"ok": False, "error": f"unknown action: {action!r}"}
            return {"ok": True, "data": data}
        except Exception as exc:  # 宿主不该因为业务异常而崩
            logger.exception("Facade.invoke(%s) 失败", action)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def shutdown(self) -> None:
        """关闭并释放全部资源。"""
        await self._c.aclose()

    # ---------- 内部实现：只做"取参数 → 调 service → DTO 转换"，不含业务判断 ----------

    async def _list_tasks(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self._c.task_service.list_tasks(
            category=params.get("category"),
            status=params.get("status"),
            limit=int(params.get("limit", 200)),
        )
        return [task_row_to_dict(row) for row in rows]

    async def _get_task(self, params: dict[str, Any]) -> dict[str, Any] | None:
        row = self._c.task_service.get_task(int(params["task_id"]))
        return None if row is None else task_row_to_dict(row)

    async def _mark_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = int(params["task_id"])
        done = bool(params["done"])
        ok = (
            self._c.task_service.mark_done(task_id)
            if done
            else self._c.task_service.mark_pending(task_id)
        )
        return {"ok": ok, "task_id": task_id, "status": "done" if done else "pending"}

    async def _summarize_pending(self) -> dict[str, Any]:
        rows = self._c.task_service.list_tasks(status="pending", limit=_SUMMARY_LIMIT)
        counts: dict[str, int] = {"assignment": 0, "activity": 0, "reminder": 0}
        max_urgency = 0
        for row in rows:
            category = row.get("category")
            if category in counts:
                counts[category] += 1
            max_urgency = max(max_urgency, int(row.get("urgency") or 0))
        return {"total": len(rows), **counts, "max_urgency": max_urgency}

