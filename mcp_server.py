"""MCP 适配器。

【职责】
- 把 canvas_task_monitor 的能力暴露为 MCP tool。
- 本文件不写业务逻辑：所有动作都通过 facade.invoke(action, params)。
- MCP 协议破坏性升级时只改本文件。

【允许 import 的东西】
- 标准库：asyncio / sys / pathlib / logging / typing
- 第三方：
  - mcp 1.x: mcp.server.fastmcp.FastMCP
  - mcp 2.x: mcp.server.mcpserver.MCPServer（FastMCP 已改名）
  二选一，运行期由 import 时自动判定。这是唯一允许 import 的第三方。
- 本项目：services.bootstrap.Container

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector /
任何 domain 类 / interfaces.dto。

【薄包装硬性要求】
每个 @mcp.tool() 函数体 ≤ 3 行：只做"解析参数 → _invoke → 返回"。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# 开发期未 pip install -e . 时的兜底：把 src/ 加进 sys.path。
# 注意：绝不要写成 from src.canvas_task_monitor.xxx —— 那会让同一模块被加载两次。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 缺 SDK / 版本不兼容时必须给清晰提示，而不是扔一堆 ImportError 堆栈。
# 放在文件顶层（不能放进函数）——否则 @mcp.tool() 装饰器在 import 阶段就会崩。
try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ImportError:
    try:
        # mcp 2.x 把 FastMCP 改名为 MCPServer（tool() / run() 签名保持一致）。
        # 迁移指南：https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver
        # 这正是"宿主破坏性更新只改适配层"的实例：业务层一行都不用动。
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        sys.stderr.write(
            "错误：未安装 MCP SDK（或版本不兼容）。\n"
            "请运行：pip install 'canvas-task-monitor[mcp]'\n"
        )
        sys.exit(1)

from canvas_task_monitor.services.bootstrap import Container

DEFAULT_SETTINGS = "./config/settings.yaml"

logger = logging.getLogger(__name__)

mcp = FastMCP("canvas-task-monitor")

_container: Container | None = None


def _get_container() -> Container:
    """惰性单例：MCP 服务是长驻进程，Container 只装配一次。"""
    global _container
    if _container is None:
        _container = Container(DEFAULT_SETTINGS)
    return _container


async def _invoke(action: str, params: dict[str, Any]) -> Any:
    """调 facade 并解包。

    - 成功：返回 result["data"]
    - 失败：抛 RuntimeError（MCP 协议会把异常当成 tool 调用失败，客户端能正确识别；
      返回 {"ok": False} 反而会破坏 list_tasks 等返回 list 的工具签名一致性）
    """
    result = await _get_container().facade.invoke(action, params)
    if not result.get("ok"):
        raise RuntimeError(f"canvas_task_monitor: {result.get('error', '未知错误')}")
    return result["data"]


@mcp.tool()
async def poll_now() -> dict:
    """立即触发一次轮询，返回本次拉取/变更/新建任务的统计。"""
    return await _invoke("poll_now", {})


@mcp.tool()
async def list_tasks(
    category: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """列出任务清单。

    category: assignment | activity | reminder（不传则全部）
    status: pending | done（不传则全部）
    """
    return await _invoke("list_tasks", {"category": category, "status": status, "limit": limit})


@mcp.tool()
async def get_task(task_id: int) -> dict | None:
    """按 ID 获取任务详情。未找到返回 None。"""
    return await _invoke("get_task", {"task_id": task_id})


@mcp.tool()
async def mark_task(task_id: int, done: bool = True) -> dict:
    """把任务标记为完成（done=True）或未完成（done=False）。"""
    return await _invoke("mark_task", {"task_id": task_id, "done": done})


@mcp.tool()
async def summarize_pending() -> dict:
    """汇总未完成任务：总数、按类别分布、最高紧迫度。"""
    return await _invoke("summarize_pending", {})


def main() -> None:
    """MCP 服务入口。

    FastMCP.run() 是同步阻塞方法，内部起 asyncio event loop。
    返回后（通常是 Ctrl-C 或宿主关闭），统一清理 Container。
    """
    logging.basicConfig(level=logging.INFO)
    try:
        mcp.run()
    finally:
        if _container is not None:
            asyncio.run(_container.aclose())


if __name__ == "__main__":
    main()
