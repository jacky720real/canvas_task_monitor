"""本文件是 Web UI 适配器（标准库 http.server，零第三方依赖）。业务逻辑一律在 services/ 里。

【职责】
- 把 canvas_task_monitor 的能力暴露为 4 个本地 REST 端点。
- 只做三件事：路由、把 query 解析成 params、把 facade.invoke 的结果原样透传。
- 所有业务动作都通过 container.facade.invoke(action, params) 完成。
- 刻意**不暴露** /api/poll：轮询会真实调用外部 API 并消耗 LLM token，
  不该是一个能在浏览器里一键触发的动作（要轮询请用 `cli_main.py poll` / `watch`）。
- 配置向导（/setup + /api/setup/*）只做"解析 JSON → 调 setup_config → 返回 JSON"，
  配置的读写与连通性测试全部在根级模块 setup_config.py 里。

【两种模式】
- 配置模式（.env 缺失或没填全）：`/` 302 跳 `/setup`，业务 API 一律 503
- 正常模式：`/` 直接给任务列表；右上角 ⚙️ 设置可随时回 `/setup` 改配置
  （改完 `POST /api/setup/reload` 就地重建 Container，**服务不重启**）

【允许 import 的东西】
- 标准库：argparse / asyncio / http.server / json / pathlib / sys / urllib.parse / webbrowser
- 本项目：services.bootstrap.Container
- 本项目根级模块：setup_config（配置工具，非业务层）

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector /
任何 domain 类 / interfaces.dto。

【端点】
  GET  /                                    → 正常模式给 web_ui/index.html；配置模式 302 → /setup
  GET  /setup                               → web_ui/setup.html（配置向导）
  GET  /api/tasks?category=&status=&limit=  → facade.invoke("list_tasks", {...})
  GET  /api/tasks/{id}                      → facade.invoke("get_task", {...})
  POST /api/tasks/{id}/done | /undone       → facade.invoke("mark_task", {...})
  POST /api/setup/test-canvas               → setup_config.test_canvas(...)
  POST /api/setup/test-ai                   → setup_config.test_ai(...)
  POST /api/setup/save                      → setup_config.save_config(...)
  POST /api/setup/reload                    → 重新装配 Container（用于保存后生效）

【退出码】0 = 正常 ／ 2 = 配置错误（非"缺配置"类，例如 settings.yaml 写坏了）
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
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
for _entry in (_SRC, _ROOT):
    if _entry.exists() and str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import setup_config  # 根级模块：配置读写 + 连通性测试（不含任何业务逻辑）
from canvas_task_monitor.services.bootstrap import Container

PROG = "ctm-web"
DEFAULT_PORT = 8765
DEFAULT_SETTINGS = "./config/settings.yaml"
DEFAULT_ENV = "./.env"
UI_DIR = Path(__file__).resolve().parent / "web_ui"
INDEX_FILE = UI_DIR / "index.html"
SETUP_FILE = UI_DIR / "setup.html"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2

# Container 的配置预检在"缺配置"时抛的 RuntimeError 信息里都带这四个字
# （见 services/bootstrap.py 的 _validate_all_config）
CONFIG_MISSING_MARKER = "缺少必需配置"
SETUP_MODE_ERROR = "服务尚未配置"


class _AppState:
    """进程级状态：Container + 当前模式（配置模式 / 正常模式）+ 配置路径。"""

    def __init__(
        self,
        container: Container | None = None,
        setup_mode: bool = True,  # 初始假设需要配置，装配成功才切到正常模式
        settings_path: Path | str = DEFAULT_SETTINGS,
        env_path: Path | str = DEFAULT_ENV,
    ) -> None:
        self.container: Container | None = container
        self.setup_mode = setup_mode
        self.settings_path = Path(settings_path)
        self.env_path = Path(env_path)


# HTTP 服务是长驻进程，Container 只装配一次（改配置时由 reload 就地重建）
_state = _AppState()


def _close_container() -> None:
    """关掉旧 Container（改配置重建前必须释放旧的 SQLite 连接）。

    Container.aclose() 内部已对连接器 / LLM / DB 逐个兜底并记日志，这里不必再包 try。
    """
    container, _state.container = _state.container, None
    if container is not None:
        asyncio.run(container.aclose())


def _try_init_container(settings_path: str | Path | None = None) -> bool:
    """尝试装配 Container。

    返回 True → 配置就绪，切换到正常模式
    返回 False → 配置缺失，保持 setup_mode
    其他异常（非配置缺失）→ 继续抛给上层
    """
    _close_container()
    try:
        _state.container = Container(str(settings_path or _state.settings_path))
    except RuntimeError as exc:
        if CONFIG_MISSING_MARKER in str(exc):
            _state.container = None
            _state.setup_mode = True
            return False
        raise

    _state.setup_mode = False
    return True


def _invoke(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """调 facade 并原样返回其结果（{"ok": bool, "data"/"error": ...}）。

    http.server 是同步模型，而 facade 是 async —— 用 asyncio.run 过桥。
    这些端点都不会碰 LLM，因此"每次请求一个新 event loop"没有副作用。
    """
    container = _state.container
    if container is None:  # pragma: no cover - 只有装配失败才会走到
        return {"ok": False, "error": "container not initialised"}
    return asyncio.run(container.facade.invoke(action, params))


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

        if parsed.path == "/setup":
            self._serve_file(SETUP_FILE, "setup.html")
            return

        if parsed.path in ("/", "/index.html"):
            if _state.setup_mode:
                self._redirect("/setup")
                return
            self._serve_file(INDEX_FILE, "index.html")
            return

        if self._blocked_in_setup_mode(parsed.path):
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

        # 配置向导端点：两种模式下都可用（"配置完成后仍允许改配置"）
        if parsed.path.startswith("/api/setup/"):
            self._route_setup(parsed.path)
            return

        if self._blocked_in_setup_mode(parsed.path):
            return

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

    def _serve_file(self, path: Path, label: str) -> None:
        """把 web_ui 下的静态页原样吐出去（本地小工具，不做模板渲染）。"""
        if not path.is_file():
            self._json({"ok": False, "error": f"{label} 不存在：{path}"}, status=500)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _blocked_in_setup_mode(self, path: str) -> bool:
        """配置模式下，业务 API 一律 503（此时前端只该看到配置向导）。"""
        if _state.setup_mode and path.startswith("/api/"):
            self._json({"ok": False, "error": SETUP_MODE_ERROR}, status=503)
            return True
        return False

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 配置向导端点（两种模式下都可用） ----------

    def _route_setup(self, path: str) -> None:
        handlers = {
            "/api/setup/test-canvas": self._handle_setup_test_canvas,
            "/api/setup/test-ai": self._handle_setup_test_ai,
            "/api/setup/save": self._handle_setup_save,
            "/api/setup/reload": self._handle_setup_reload,
        }
        handler = handlers.get(path)
        if handler is None:
            self._json({"ok": False, "error": f"unknown path: {path}"}, status=404)
            return
        handler()

    def _read_json_body(self) -> dict[str, Any]:
        """读并解析请求体；空体返回 {}，非法 JSON 抛 ValueError（调用方转 400）。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            # 请求体不合规属于"外部数据不合规"，与项目其它处一致用 ValueError 表达
            raise ValueError("请求体必须是 JSON 对象")  # noqa: TRY004
        return payload

    def _handle_setup_test_canvas(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return

        ok, result = setup_config.test_canvas(body.get("base_url", ""), body.get("token", ""))
        self._json({"ok": ok, **result})

    def _handle_setup_test_ai(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return

        ok, result = setup_config.test_ai(
            body.get("base_url", ""), body.get("api_key", ""), body.get("model", "")
        )
        self._json({"ok": ok, **result})

    def _handle_setup_save(self) -> None:
        try:
            body = self._read_json_body()
            setup_config.save_config(
                canvas=body.get("canvas") or {},
                ai=body.get("ai") or {},
                mail=body.get("mail"),
                env_path=_state.env_path,
                settings_path=_state.settings_path,
            )
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        except OSError as exc:
            self._json({"ok": False, "error": f"写入配置失败：{exc}"}, status=500)
            return

        # 后端也校验一遍（前端可能被绕过），避免"存下去了但根本跑不起来"
        if not setup_config.is_configured(_state.env_path, _state.settings_path):
            self._json(
                {"ok": False, "error": "配置不完整：Canvas 网址/令牌 与 AI Base URL/Key 都是必填"},
                status=400,
            )
            return

        self._json({"ok": True})

    def _handle_setup_reload(self) -> None:
        # 先把 .env 的值刷新进本进程环境：core/config.py 用 load_dotenv(override=False)，
        # 否则"启动时读到过的旧值（例如空 Token）"会压住刚保存的新值。
        setup_config.refresh_process_env(_state.env_path)
        try:
            if _try_init_container():
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "配置仍不完整，请检查 .env"}, status=400)
        except Exception as exc:  # noqa: BLE001 —— 任何装配失败都要原样回给页面，用户自己修
            self._json({"ok": False, "error": f"配置有误：{exc}"}, status=500)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Canvas 任务监控 Web UI（本地）")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）"
    )
    parser.add_argument("--settings", default=DEFAULT_SETTINGS, help="配置文件路径")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser


def main() -> int:
    """启动本地 Web UI。

    两种开局：
    - 配置模式（.env 缺失或没填全）：`/` 会 302 到 `/setup`，浏览器直接落在配置向导
    - 正常模式：`/` 直接是任务列表；右上角 ⚙️ 设置随时可回配置向导改配置

    配置错误（非"缺配置"类，例如 settings.yaml 写坏）打印到 stderr 并返回退出码 2。
    """
    args = build_parser().parse_args()
    _state.settings_path = Path(args.settings)
    # 与 core/config.py 的约定一致：.env 放在 settings.yaml 所在目录的上一级（项目根）
    _state.env_path = _state.settings_path.parent.parent / ".env"

    configured = setup_config.is_configured(_state.env_path, _state.settings_path)
    try:
        if configured or setup_config.env_credentials_present():
            _try_init_container(_state.settings_path)
        else:
            # 完全没配：直接进配置模式，连 Container 都不试着装配——
            # 否则会先刷一屏"环境变量未设置，配置项保留占位符"的告警，小白用户看了心慌
            _state.setup_mode = True
    except RuntimeError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        print("请检查 .env 或 config/settings.yaml", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), WebHandler)

    if _state.setup_mode:
        print("[web] 检测到未配置，启动配置模式")
        print(f"[web] 浏览器已打开：{url}")
        print("[web] 跟着页面引导走三步：Canvas → AI →（邮箱可选）")
    else:
        print(f"[web] 服务已启动：{url}（Ctrl-C 退出）")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")
    finally:
        server.server_close()
        _close_container()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

