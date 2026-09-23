"""本文件是 Web UI 适配器（标准库 http.server，零第三方依赖）。业务逻辑一律在 services/ 里。

【职责】
- 把 canvas_task_monitor 的能力暴露为 4 个本地 REST 端点。
- 只做三件事：路由、把 query 解析成 params、把 facade.invoke 的结果原样透传。
- 所有业务动作都通过 container.facade.invoke(action, params) 完成。
- 刻意**不暴露** /api/poll：轮询会真实调用外部 API 并消耗 LLM token，
  不该是一个能在浏览器里一键触发的动作（要轮询请用 `cli_main.py poll` / `watch`）。

【允许 import 的东西】
- 标准库：argparse / asyncio / http.server / json / pathlib / sys / urllib.parse / webbrowser
- 本项目：services.bootstrap.Container

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector /
任何 domain 类 / interfaces.dto。

【端点】
  GET  /                                    → web_ui/index.html
  GET  /api/tasks?category=&status=&limit=  → facade.invoke("list_tasks", {...})
  GET  /api/tasks/{id}                      → facade.invoke("get_task", {...})
  POST /api/tasks/{id}/done | /undone       → facade.invoke("mark_task", {...})

【退出码】0 = 正常 ／ 2 = 配置错误
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 开发期未 pip install -e . 时的兜底：把 src/ 加进 sys.path。
# 注意：绝不要写成 from src.canvas_task_monitor.xxx —— 那会让同一模块被加载两次。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from canvas_task_monitor.services.bootstrap import Container

PROG = "ctm-web"
DEFAULT_PORT = 8765
DEFAULT_SETTINGS = "./config/settings.yaml"
INDEX_FILE = Path(__file__).resolve().parent / "web_ui" / "index.html"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2

# 进程级单例：HTTP 服务是长驻进程，Container 只装配一次
_container: Container | None = None


def _invoke(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """调 facade 并原样返回其结果（{"ok": bool, "data"/"error": ...}）。

    http.server 是同步模型，而 facade 是 async —— 用 asyncio.run 过桥。
    这些端点都不会碰 LLM，因此"每次请求一个新 event loop"没有副作用。
    """
    if _container is None:  # pragma: no cover - 只有装配失败才会走到
        return {"ok": False, "error": "container not initialised"}
    return asyncio.run(_container.facade.invoke(action, params))


def _first(query: dict[str, list[str]], key: str) -> str | None:
    """取 querystring 里某个键的第一个非空值；缺失/空串一律返回 None。"""
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _resource_id(path: str) -> int | None:
    """从 /api/tasks/{id} 里取出整数 id；不匹配或非数字返回 None。"""
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "tasks":
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


class WebHandler(BaseHTTPRequestHandler):
    """极薄路由层：只做解析与转发，不含任何业务判断。"""

    server_version = "canvas-task-monitor-web/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        """静默 HTTP 访问日志（本地小工具，不刷屏）。"""

    # ---------- 路由 ----------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._serve_index()
            return

        if parsed.path == "/api/tasks":
            query = parse_qs(parsed.query)
            limit = _first(query, "limit")
            self._json(
                _invoke(
                    "list_tasks",
                    {
                        "category": _first(query, "category"),
                        "status": _first(query, "status"),
                        "limit": int(limit) if limit and limit.isdigit() else 200,
                    },
                )
            )
            return

        task_id = _resource_id(parsed.path)
        if task_id is not None:
            self._json(_invoke("get_task", {"task_id": task_id}))
            return

        self._json({"ok": False, "error": f"unknown path: {parsed.path}"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [segment for segment in parsed.path.split("/") if segment]

        # 形如 /api/tasks/{id}/done 或 /api/tasks/{id}/undone
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "tasks"
            and parts[3] in ("done", "undone")
        ):
            if not parts[2].isdigit():
                self._json({"ok": False, "error": f"invalid task id: {parts[2]}"}, status=400)
                return
            self._json(
                _invoke("mark_task", {"task_id": int(parts[2]), "done": parts[3] == "done"})
            )
            return

        self._json({"ok": False, "error": f"unknown path: {parsed.path}"}, status=404)

    # ---------- 响应 ----------

    def _serve_index(self) -> None:
        if not INDEX_FILE.is_file():
            self._json({"ok": False, "error": f"index.html 不存在：{INDEX_FILE}"}, status=500)
            return
        body = INDEX_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Canvas 任务监控 Web UI（本地）")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）"
    )
    parser.add_argument("--settings", default=DEFAULT_SETTINGS, help="配置文件路径")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser


def main() -> int:
    """启动本地 Web UI；配置错误时打印到 stderr 并返回退出码 2。"""
    global _container

    args = build_parser().parse_args()
    try:
        _container = Container(args.settings)
    except RuntimeError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        print("请检查 .env 或 config/settings.yaml", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), WebHandler)
    print(f"Web UI 已启动：{url}（Ctrl-C 退出）")
    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        asyncio.run(_container.aclose())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

