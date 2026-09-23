"""本文件是 Web UI 适配器（标准库 http.server，零第三方依赖）。业务逻辑一律在 services/ 里。

【职责】
- 把 canvas_task_monitor 的能力暴露为 5 个本地 REST 端点。
- 只做三件事：路由、把 query 解析成 params、把 facade.invoke 的结果原样透传。
- 所有业务动作都通过 container.facade.invoke(action, params) 完成。
- /api/poll 会真实调用外部 API 并消耗 LLM token（用户明确要求把它做成页面按钮），
  因此用 _state.poll_lock 防并发：第二个请求直接 409，避免重复烧 token。
  命令行等价功能仍是 `cli_main.py poll` / `watch`。
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
  GET  /api/setup/current                   → setup_config.load_current_config(...)（不含敏感值）
  GET  /api/tasks?category=&status=&limit=  → facade.invoke("list_tasks", {...})
  GET  /api/tasks/{id}                      → facade.invoke("get_task", {...})
  POST /api/tasks/{id}/done | /undone       → facade.invoke("mark_task", {...})
  POST /api/poll                            → facade.invoke("poll_now", {...})（带并发锁）
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
import threading
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
DEFAULT_STATE = "./data/state.json"
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
        state_path: Path | str = DEFAULT_STATE,
    ) -> None:
        self.container: Container | None = container
        self.setup_mode = setup_mode
        self.settings_path = Path(settings_path)
        self.env_path = Path(env_path)
        # 连通性测试结果的落盘位置（设置页预填 / 首页 banner 都读它）
        self.state_path = Path(state_path)
        # 防并发 poll：第二个请求返回 409，避免重复调 LLM 烧 token
        self.poll_lock = threading.Lock()


# HTTP 服务是长驻进程，Container 只装配一次（改配置时由 reload 就地重建）
_state = _AppState()

# 进程级**共享**事件循环：容器里的 httpx 客户端是惰性创建的，会绑在"创建它的那个 loop"上。
# 若每个请求都 asyncio.run() 一个新 loop，第一次请求建的连接就绑在一个马上被关掉的 loop 上
# —— 第二次点"拉取"以及关闭容器时都会撞 RuntimeError: Event loop is closed（真机现象）。
# 所以整个进程共用一个 loop；http.server 是多线程的，用 _loop_lock 串行化 facade 调用。
_loop = asyncio.new_event_loop()
_loop_lock = threading.Lock()


def _run(coro: Any) -> Any:
    """在共享 loop 上跑一个协程并等它结束（并发请求串行执行，单机单人使用足够）。"""
    with _loop_lock:
        return _loop.run_until_complete(coro)


def _close_loop() -> None:
    """进程退出前关掉共享 loop（此时容器资源已释放）。"""
    try:
        if not _loop.is_closed():
            _loop.close()
    except Exception:  # noqa: BLE001, S110 —— loop 可能已半关闭，不影响进程退出
        pass


def _close_container() -> None:
    """关掉旧 Container（改配置重建前必须释放旧的 SQLite 连接）。

    【为什么不用 asyncio.run（真机踩过）】
    容器里的 httpx 客户端绑在**创建它的那个 loop** 上。asyncio.run() 会另起一个 loop
    并在返回后立即关掉它，而 httpx 的 aclose() 里关 TLS 连接要往"它自己那个 loop"上
    call_soon → RuntimeError: Event loop is closed（bootstrap 会把"连接器关闭失败"
    记成一条带堆栈的 WARNING，用户看到的就是那段像崩溃的输出）。

    所以在**同一个共享 loop** 上关闭，并补一次 asyncio.sleep(0) 让挂起的清理回调跑完。
    关闭期的任何异常都吞掉——不影响进程退出。
    """
    container, _state.container = _state.container, None
    if container is None:
        return
    try:
        _run(container.aclose())
        # 给挂起的异步清理（httpx 关闭 TLS 连接等）一个执行机会
        _run(asyncio.sleep(0))
    except Exception:  # noqa: BLE001, S110 —— 关闭期异常不影响进程退出
        pass


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

    http.server 是同步模型，而 facade 是 async —— 用进程级共享 loop 过桥
    （见 _run 的说明：不能用 asyncio.run，否则 httpx 客户端会绑在死 loop 上）。
    """
    container = _state.container
    if container is None:  # pragma: no cover - 只有装配失败才会走到
        return {"ok": False, "error": "container not initialised"}
    return _run(container.facade.invoke(action, params))


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

        # 当前配置（不含敏感值）：配置向导预填 + 首页 banner 都用它，两种模式下都可用
        if parsed.path == "/api/setup/current":
            self._json(
                {
                    "ok": True,
                    "data": setup_config.load_current_config(_state.env_path, _state.settings_path),
                }
            )
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

        # 页面上的"拉取"按钮（会真调外部 API + LLM，故带并发锁）
        if parsed.path == "/api/poll":
            self._handle_poll()
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

    # ---------- 业务动作端点 ----------

    def _handle_poll(self) -> None:
        """触发一次 poll。同步阻塞（单机单人使用，30-60 秒可接受）。

        用 _state.poll_lock 防并发：第二个请求返回 409，避免重复调 LLM 烧 token。
        """
        if _state.setup_mode:
            self._json({"ok": False, "error": "配置未完成"}, status=400)
            return
        if not _state.poll_lock.acquire(blocking=False):
            self._json({"ok": False, "error": "已有拉取任务在执行中，请稍候"}, status=409)
            return
        try:
            container = _state.container
            if container is None:  # pragma: no cover - 正常模式下不会是 None
                self._json({"ok": False, "error": "container not initialised"}, status=500)
                return
            result = _run(container.facade.invoke("poll_now", {}))
        except Exception as exc:  # noqa: BLE001 —— 异常也要回给页面，不能让它变成空响应
            self._json({"ok": False, "error": f"拉取失败：{exc}"}, status=500)
            return
        finally:
            _state.poll_lock.release()

        if not result.get("ok"):
            self._json({"ok": False, "error": result.get("error", "拉取失败")}, status=500)
            return

        # 记录 Canvas 体检结果（供首页 banner 用）；体检是附加信息，失败不该影响本次结论
        try:
            setup_config.check_canvas(_state.env_path, _state.state_path, _state.settings_path)
        except Exception:  # noqa: BLE001, S110 —— 体检失败只忽略，不要连累拉取结果
            pass

        self._json({"ok": True, "data": result["data"]})

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

        # 敏感字段留空时用 .env 里已保存的值（预填页面上用户没重新粘贴）
        token = body.get("token") or setup_config.saved_secret(_state.env_path, "CANVAS_TOKEN")
        ok, result = setup_config.test_canvas(body.get("base_url", ""), token)
        # 测试结果落盘：设置页顶部状态条 / 首页 banner 都靠它
        setup_config.record_test("canvas", ok, result.get("error"), _state.state_path)
        self._json({"ok": ok, **result})

    def _handle_setup_test_ai(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return

        api_key = body.get("api_key") or setup_config.saved_secret(_state.env_path, "LLM_API_KEY")
        ok, result = setup_config.test_ai(
            body.get("base_url", ""), api_key, body.get("model", "")
        )
        setup_config.record_test("ai", ok, result.get("error"), _state.state_path)
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
    # state.json 同目录的 data/ 下（setup_config 统一约定，CLI 也用同一处）
    _state.state_path = setup_config.state_path_for(_state.settings_path)

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
        _close_loop()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

