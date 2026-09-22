"""本文件是 CLI 适配器。业务逻辑一律在 services/ 里。

【允许 import 的东西】
- 标准库：argparse / asyncio / sys / pathlib / typing
- 第三方：rich（表格与面板）
- 本项目：services.bootstrap.Container、contracts.plugin.PLUGIN_API_VERSION（元信息，非业务逻辑）

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector / 任何 domain 类。
所有业务动作都通过 container.facade.invoke(action, params) 完成。
唯一例外：watch 子命令直接调 container.poller.run_forever()，理由见该函数 docstring。

【退出码】
0 = 正常 ／ 1 = 业务失败（如任务未找到）／ 2 = 配置错误 ／ 130 = 用户中断
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# 开发期未 pip install -e . 时的兜底：把 src/ 加进 sys.path。
# 注意：绝不要写成 from src.canvas_task_monitor.xxx —— 那会让同一模块被加载两次。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from canvas_task_monitor.contracts.plugin import PLUGIN_API_VERSION
from canvas_task_monitor.services.bootstrap import Container

PROG = "canvas-task-monitor"
DEFAULT_SETTINGS = "./config/settings.yaml"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130

_CATEGORY_LABELS = {"assignment": "作业", "activity": "活动", "reminder": "提醒"}
_DONE_MARK = "✅"
_PENDING_MARK = "☐"

console = Console()


def _get_container(args: argparse.Namespace) -> Container:
    """按 --settings 装配 Container。

    缺配置时 Container 会抛 RuntimeError；本文件不处理，交给 main() 统一兜住并给出友好提示。
    """
    return Container(args.settings)


def _print_error(message: Any) -> None:
    """统一打印错误。

    用 escape() 兜底：异常文本里可能出现方括号（例如 jsonschema 的 "['tasks', 0]"），
    不转义的话会被 rich 当作文本标记解析。
    """
    console.print(f"[red]{escape(str(message))}[/red]")


def _category_label(category: str) -> str:
    """类别英文 → 中文（未知值原样返回）。"""
    return _CATEGORY_LABELS.get(category, category)


def _render_task_table(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    """渲染任务表格；无数据时打印 (空) 面板而不是空表格。"""
    title = f"任务清单 (category={args.category or 'all'}, status={args.status or 'all'})"
    if not rows:
        console.print(Panel("(空)", title=title))
        return

    table = Table(title=title)
    table.add_column("ID", style="dim", width=5, justify="right")
    table.add_column("✓", width=2, justify="center")
    table.add_column("类别", width=4)
    table.add_column("标题", overflow="fold")
    table.add_column("课程", width=14, overflow="ellipsis")
    table.add_column("截止", width=16)
    table.add_column("紧迫/重要", width=9, justify="center")
    table.add_column("分数", width=4, justify="right")

    for row in rows:
        table.add_row(
            str(row["id"]),
            _DONE_MARK if row["status"] == "done" else _PENDING_MARK,
            _category_label(row["category"]),
            row["title"],
            row["course"] or "",
            (row["due_at"] or "")[:16].replace("T", " "),
            f"{row['urgency']}/{row['importance']}",
            str(row["score"]),
        )
    console.print(table)


def _render_task_detail(row: dict[str, Any]) -> None:
    """渲染单条任务详情面板。"""
    console.print(
        Panel.fit(
            f"[bold]{row['title']}[/bold]\n"
            f"ID: {row['id']}  类别: {_category_label(row['category'])}  状态: {row['status']}\n"
            f"课程: {row['course'] or '(未标注)'}\n"
            f"截止: {row['due_at'] or '(无)'}\n"
            f"紧迫/重要: {row['urgency']}/{row['importance']}  综合分: {row['score']}\n"
            f"标签: {', '.join(row['tags']) or '(无)'}\n"
            f"紧迫理由: {row['urgency_reason'] or '(无)'}\n"
            f"重要理由: {row['importance_reason'] or '(无)'}\n\n"
            f"{row['summary'] or '(无摘要)'}",
            title="任务详情",
        )
    )


def cmd_poll(args: argparse.Namespace) -> int:
    """立即轮询一次（走 facade：一次性请求-响应语义）。"""

    async def _run() -> int:
        container = _get_container(args)
        try:
            result = await container.facade.invoke("poll_now", {})
        finally:
            await container.aclose()
        if not result["ok"]:
            _print_error(result["error"])
            return EXIT_FAILURE
        stats = result["data"]
        console.print(
            f"轮询完成：来源 {stats['sources']}，变更 {stats['changes']}，"
            f"任务 {stats['tasks']}，LLM 调用 {stats['llm_calls']}"
        )
        return EXIT_OK

    return asyncio.run(_run())


def cmd_watch(args: argparse.Namespace) -> int:
    """后台持续轮询，Ctrl-C 退出。

    【为什么这里不走 facade】
    watch 是"长驻阻塞任务"，本质不同于 invoke 的"一次请求-响应"语义。
    MCP / DSH 不适合暴露长驻任务（会阻塞 event loop），它们自有调度机制。
    因此 watch 作为 CLI 独有能力，直接调用业务层的 Poller.run_forever()，
    不通过 facade。这是**唯一**一处 CLI 绕开 facade 的地方。
    """

    async def _run() -> int:
        container = _get_container(args)
        console.print("[yellow]开始持续轮询，Ctrl-C 退出[/yellow]")
        try:
            await container.poller.run_forever()
        finally:
            await container.aclose()
        # run_forever() 是 while True，唯一退出路径是 Ctrl-C（被 main() 兜住 → 130）。
        # 这行 return 永远不可达，保留是为了让所有 cmd_* 函数签名一致。
        return EXIT_OK

    return asyncio.run(_run())


def cmd_list(args: argparse.Namespace) -> int:
    """打印任务表格（走 facade）。"""

    async def _run() -> int:
        container = _get_container(args)
        try:
            result = await container.facade.invoke(
                "list_tasks",
                {"category": args.category, "status": args.status, "limit": args.limit},
            )
        finally:
            await container.aclose()
        if not result["ok"]:
            _print_error(result["error"])
            return EXIT_FAILURE
        _render_task_table(result["data"], args)
        return EXIT_OK

    return asyncio.run(_run())


def cmd_show(args: argparse.Namespace) -> int:
    """查看任务详情（走 facade）。"""

    async def _run() -> int:
        container = _get_container(args)
        try:
            result = await container.facade.invoke("get_task", {"task_id": args.task_id})
        finally:
            await container.aclose()
        if not result["ok"]:
            _print_error(result["error"])
            return EXIT_FAILURE
        row = result["data"]
        if row is None:
            console.print(f"[yellow]未找到任务 {args.task_id}[/yellow]")
            return EXIT_FAILURE
        _render_task_detail(row)
        return EXIT_OK

    return asyncio.run(_run())


def _mark_task(args: argparse.Namespace, *, done: bool) -> int:
    """done / undone 的公共实现（走 facade）。"""

    async def _run() -> int:
        container = _get_container(args)
        try:
            result = await container.facade.invoke(
                "mark_task", {"task_id": args.task_id, "done": done}
            )
        finally:
            await container.aclose()
        if not result["ok"]:
            _print_error(result["error"])
            return EXIT_FAILURE
        data = result["data"]
        if not data.get("ok"):
            console.print(f"[yellow]未找到任务 {args.task_id}[/yellow]")
            return EXIT_FAILURE
        console.print(f"[green]OK[/green] 任务 {args.task_id} → {data['status']}")
        return EXIT_OK

    return asyncio.run(_run())


def cmd_done(args: argparse.Namespace) -> int:
    """标记任务完成。"""
    return _mark_task(args, done=True)


def cmd_undone(args: argparse.Namespace) -> int:
    """取消完成标记。"""
    return _mark_task(args, done=False)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Canvas LMS + Microsoft 365 学习任务监控（CLI 形态）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {PLUGIN_API_VERSION}",
    )
    parser.add_argument(
        "--settings",
        default=DEFAULT_SETTINGS,
        help="配置文件路径（默认 ./config/settings.yaml）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    parser_poll = sub.add_parser("poll", help="立即轮询一次")
    parser_poll.set_defaults(func=cmd_poll)

    parser_watch = sub.add_parser("watch", help="后台持续轮询（Ctrl-C 退出）")
    parser_watch.set_defaults(func=cmd_watch)

    parser_list = sub.add_parser("list", help="打印任务表格")
    parser_list.add_argument(
        "--category", choices=["assignment", "activity", "reminder"], default=None
    )
    parser_list.add_argument("--status", choices=["pending", "done"], default=None)
    parser_list.add_argument("--limit", type=int, default=100)
    parser_list.set_defaults(func=cmd_list)

    parser_show = sub.add_parser("show", help="查看任务详情")
    parser_show.add_argument("task_id", type=int)
    parser_show.set_defaults(func=cmd_show)

    parser_done = sub.add_parser("done", help="标记任务完成")
    parser_done.add_argument("task_id", type=int)
    parser_done.set_defaults(func=cmd_done)

    parser_undone = sub.add_parser("undone", help="取消完成标记")
    parser_undone.add_argument("task_id", type=int)
    parser_undone.set_defaults(func=cmd_undone)

    return parser


def main() -> int:
    """CLI 入口，返回进程退出码。

    这里捕获 RuntimeError 是安全的：业务异常全部在 Facade.invoke 内部被兜住并转成
    {"ok": False}，能逃到这一层的 RuntimeError 只可能来自 Container 装配（缺配置）。
    """
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        console.print(f"[red]配置错误：[/red]{escape(str(exc))}")
        console.print("[yellow]请检查 .env 或 config/settings.yaml[/yellow]")
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())

