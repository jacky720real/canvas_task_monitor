"""配置工具模块。

【职责】
- 读写 .env 和 config/settings.yaml
- 测试 Canvas / AI 的连通性（不是完整业务调用，只是"通不通"探测）
- 备份既有配置文件

【允许 import】
- 标准库：pathlib / os / re / json / datetime
- 第三方：httpx / yaml
- 本项目：无（不 import 业务层）

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector /
Container / facade / interfaces.dto / 任何 domain 类。

【为什么可以直接用 httpx 发测试请求】
本文件是"配置辅助工具"，只做连通性探测——发一个极简请求看通不通，
不涉及分页 / 重试 / 限流等 connector 的完整逻辑。
和四个入口"调 facade.invoke"的定位不同：这里测的是"配置项对不对"，
不是"业务能不能跑"。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

CANVAS_REQUIRED_KEYS = ("CANVAS_BASE_URL", "CANVAS_TOKEN")
AI_REQUIRED_KEYS = ("LLM_BASE_URL", "LLM_API_KEY")

# 连通性测试结果的落盘文件（相对 settings.yaml 所在目录的上一级），见 state_path_for
DEFAULT_STATE_NAME = "state.json"

CANVAS_TIMEOUT = 10.0
AI_TIMEOUT = 15.0

# 未解析的 env 占位符，例如 ${CANVAS_TOKEN}
_PLACEHOLDER = re.compile(r"\$\{[A-Z0-9_]+\}")


# ---------- 读取与判定 ----------


def read_env(env_path: Path) -> dict[str, str]:
    """把 .env 解析成 dict；只认 `KEY=VALUE` 行，注释与空行忽略。"""
    path = Path(env_path)
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key = _env_key(line)
        if key is None:
            continue
        _, _, raw = line.partition("=")
        values[key] = raw.strip().strip('"').strip("'")
    return values


def _is_missing(value: str | None) -> bool:
    """空 / 纯空白 / 未解析占位符，都算"没填"。"""
    if value is None:
        return True
    text = value.strip()
    return not text or bool(_PLACEHOLDER.fullmatch(text))


def is_configured(env_path: Path, settings_path: Path) -> bool:
    """判断配置是否已就绪。

    返回 False 的条件（任一）：
    - .env 不存在
    - .env 里 CANVAS_BASE_URL 或 CANVAS_TOKEN 是空/未解析占位符
    - .env 里 LLM_BASE_URL 或 LLM_API_KEY 是空/未解析占位符
    - settings.yaml 不存在（没有它业务层根本跑不起来）
    """
    if not Path(settings_path).is_file():
        return False

    env = read_env(env_path)
    keys = CANVAS_REQUIRED_KEYS + AI_REQUIRED_KEYS
    return not any(_is_missing(env.get(key)) for key in keys)


def env_credentials_present() -> bool:
    """进程环境变量里是否已经带了 Canvas / AI 凭据？

    给"不用 .env、直接用系统环境变量提供凭据"的人留的后路：
    启动时若 .env 没配好但环境变量齐全，仍按正常模式装配。
    """
    return not any(
        _is_missing(os.environ.get(key)) for key in CANVAS_REQUIRED_KEYS + AI_REQUIRED_KEYS
    )


def refresh_process_env(env_path: Path) -> dict[str, str]:
    """把 .env 的值灌进当前进程的环境变量（**覆盖**已有值）。

    为什么需要：core/config.py 用 `load_dotenv(override=False)` 读 .env，
    进程启动时已经读到过的旧值（典型场景：启动时 Token 是空的）不会被文件里的新值覆盖，
    结果就是"页面上改完配置、reload 回来还是老配置"。
    因此保存配置后由入口显式调一次本函数，保证 reload 立刻生效。
    """
    values = read_env(env_path)
    os.environ.update(values)
    return values


# ---------- 状态文件（data/state.json） ----------


def state_path_for(settings_path: Path) -> Path:
    """state.json 的约定位置：settings.yaml 所在目录的上一级 + data/。"""
    return Path(settings_path).parent.parent / "data" / DEFAULT_STATE_NAME


def read_state(state_path: Path) -> dict[str, Any]:
    """读 state.json；不存在 / 内容坏掉一律返回 {}（状态文件不该拖垮主流程）。"""
    path = Path(state_path)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def record_test(kind: str, ok: bool, error: str | None, state_path: Path) -> dict[str, Any]:
    """记录一次连通性测试结果（kind = "canvas" / "ai"），写进 state.json。

    Web 端据此显示"上次测试失败：401: 令牌无效"，CLI 据此提示 token 过期。
    """
    state = read_state(state_path)
    state[f"last_{kind}_test"] = {
        "ok": bool(ok),
        "error": None if ok else (error or "未知错误"),
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


def read_settings_sections(settings_path: Path) -> dict[str, Any]:
    """读 settings.yaml 的顶层映射；文件不存在 / 坏掉返回 {}。"""
    path = Path(settings_path)
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def configured_sources(settings_path: Path) -> list[str]:
    """读 settings.yaml 的 poll.sources（读不到就返回空列表）。"""
    poll = read_settings_sections(settings_path).get("poll")
    if not isinstance(poll, dict):
        return []
    sources = poll.get("sources")
    return [str(item) for item in sources] if isinstance(sources, list) else []


def canvas_credentials(env_path: Path) -> tuple[str, str]:
    """取 Canvas 凭据：优先 .env（配置向导写的），缺失时才用进程环境变量。"""
    env = read_env(env_path)
    base_url = env.get("CANVAS_BASE_URL") or os.environ.get("CANVAS_BASE_URL", "")
    token = env.get("CANVAS_TOKEN") or os.environ.get("CANVAS_TOKEN", "")
    return base_url.strip(), token.strip()


def check_canvas(env_path: Path, state_path: Path) -> tuple[bool, dict[str, Any]]:
    """用当前凭据探一次 Canvas，并把结果写进 state.json（供 CLI 做 token 体检）。

    凭据完全没配时只返回错误、不写状态——免得在"根本没配 Canvas"的机器上
    留下一条假的失败记录，进而在 Web 上弹出一个莫名其妙的 banner。
    """
    base_url, token = canvas_credentials(env_path)
    if not base_url or not token:
        return False, {"error": "Canvas 未配置（缺少 CANVAS_BASE_URL / CANVAS_TOKEN）"}

    ok, result = test_canvas(base_url, token)
    record_test("canvas", ok, result.get("error"), state_path)
    return ok, result


def saved_secret(env_path: Path, key: str) -> str:
    """取 .env 里某个密钥的已存值。

    用途：设置页预填后敏感字段是空的（前端拿不到明文），用户直接点"测试连接"时
    应当拿**已保存的**密钥去测，而不是报"请先填入"。
    """
    return read_env(env_path).get(key, "").strip()


def load_current_config(env_path: Path, settings_path: Path) -> dict[str, Any]:
    """读取当前配置，供设置页预填。

    返回：
    {
        "canvas": {"base_url": "https://...", "token_saved": True},
        "ai": {"base_url": "...", "model": "...", "api_key_saved": True},
        "mail": None | {"provider": "imap", "host": "...", "username": "...",
                        "password_saved": True},
        "last_test": {
            "canvas_ok": True | False | None,
            "canvas_error": "401: 令牌无效" | None,
            "tested_at": "2026-09-23T15:07:32+08:00" | None,
            "ai_ok": True | False | None,          # 同结构，给 AI 卡片复用
            "ai_error": "401: Key 无效" | None,
            "ai_tested_at": "..." | None,
        },
    }

    注意：
    - **不返回敏感字段的真实值**（token / api_key / password），只返回 `xxx_saved: True/False`
    - mail 只有在 settings.yaml 的 poll.sources 里启用了 mail 时才有值
    """
    env = read_env(env_path)
    sources = configured_sources(settings_path)

    mail: dict[str, Any] | None = None
    if "mail" in sources:
        mail_section = read_settings_sections(settings_path).get("mail")
        provider = str((mail_section or {}).get("provider") or "graph") if isinstance(
            mail_section, dict
        ) else "graph"
        mail = {
            "provider": provider,
            "host": env.get("IMAP_HOST", ""),
            "username": env.get("IMAP_USER", ""),
            "password_saved": not _is_missing(env.get("IMAP_PASSWORD")),
        }

    state = read_state(state_path_for(settings_path))
    canvas_test = state.get("last_canvas_test")
    ai_test = state.get("last_ai_test")
    canvas_test = canvas_test if isinstance(canvas_test, dict) else {}
    ai_test = ai_test if isinstance(ai_test, dict) else {}

    return {
        "canvas": {
            "base_url": env.get("CANVAS_BASE_URL", ""),
            "token_saved": not _is_missing(env.get("CANVAS_TOKEN")),
        },
        "ai": {
            "base_url": env.get("LLM_BASE_URL", ""),
            "model": env.get("LLM_MODEL", ""),
            "api_key_saved": not _is_missing(env.get("LLM_API_KEY")),
        },
        "mail": mail,
        "last_test": {
            "canvas_ok": canvas_test.get("ok"),
            "canvas_error": canvas_test.get("error"),
            "tested_at": canvas_test.get("tested_at"),
            "ai_ok": ai_test.get("ok"),
            "ai_error": ai_test.get("error"),
            "ai_tested_at": ai_test.get("tested_at"),
        },
    }


# ---------- 连通性测试 ----------


def test_canvas(base_url: str, token: str) -> tuple[bool, dict[str, Any]]:
    """测试 Canvas 连通性。

    返回 (ok, result)：
    - ok=True  → result={"courses": ["数据结构", "线性代数", "英语写作"]}
    - ok=False → result={"error": "401: 令牌无效"}

    实现：GET {base_url}/api/v1/courses?per_page=3，超时 10s，Bearer 鉴权。
    区分错误：401=令牌错 / 404=网址错 / 超时=网络问题。
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return False, {"error": "Canvas 网址为空"}
    if not (token or "").strip():
        return False, {"error": "Canvas 令牌为空"}

    try:
        response = httpx.get(
            f"{base_url}/api/v1/courses",
            params={"per_page": 3},
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=CANVAS_TIMEOUT,
        )
    except httpx.TimeoutException:
        return False, {"error": "连接超时：请检查网络，或学校的 Canvas 网址是否正确"}
    except httpx.HTTPError as exc:
        return False, {"error": f"网络错误：{exc}"}

    if response.status_code == 401:
        return False, {"error": "401: 令牌无效（请在 Canvas 里重新生成访问令牌）"}
    if response.status_code == 404:
        return False, {
            "error": "404: 网址错误（应形如 https://school.instructure.com，不要带 /api/v1）"
        }
    if response.status_code >= 400:
        return False, {"error": f"{response.status_code}: {_response_error(response)}"}

    try:
        courses = response.json()
    except ValueError:
        return False, {"error": "返回内容不是 JSON：网址可能填成了别的页面"}

    names = [
        str(item.get("name") or item.get("course_code") or item.get("id"))
        for item in courses
        if isinstance(item, dict)
    ]
    return True, {"courses": names}


def test_ai(base_url: str, api_key: str, model: str) -> tuple[bool, dict[str, Any]]:
    """测试 AI 连通性。

    返回 (ok, result)：
    - ok=True  → result={"sample": "ok"}
    - ok=False → result={"error": "401: Key 无效"}

    实现：POST {base_url}/chat/completions，body 极简
         {"model": model, "messages": [{"role":"user","content":"hi"}],
          "max_tokens": 5}，超时 15s。
    区分错误：401=Key 错 / 402/403=余额或权限 / 超时=网络。
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return False, {"error": "AI Base URL 为空"}
    if not (api_key or "").strip():
        return False, {"error": "API Key 为空"}
    if not (model or "").strip():
        return False, {"error": "模型名为空"}

    body = {
        "model": model.strip(),
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=AI_TIMEOUT,
        )
    except httpx.TimeoutException:
        return False, {"error": "连接超时：请检查网络，或 Base URL 是否正确"}
    except httpx.HTTPError as exc:
        return False, {"error": f"网络错误：{exc}"}

    if response.status_code == 401:
        return False, {"error": "401: API Key 无效"}
    if response.status_code in (402, 403):
        return False, {"error": f"{response.status_code}: 余额不足或没有该模型的权限"}
    if response.status_code >= 400:
        return False, {"error": f"{response.status_code}: {_response_error(response)}"}

    return True, {"sample": "ok"}


def _response_error(response: httpx.Response) -> str:
    """尽量从错误响应体里抠出可读信息（例如"model 不存在"）。"""
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "未知错误"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if payload.get("message"):
            return str(payload["message"])
    return response.reason_phrase or "未知错误"


# ---------- 写入 ----------


def _decided(values: dict[str, str], key: str, candidate: Any) -> None:
    """按"没碰 / 清空 / 覆盖"三态决定是否写入某个 KEY。

    - candidate 为 None → 视为"没碰"：不放进 values，`_update_env` 就保留 .env 里的原行
    - candidate 为 ""   → 清空该项
    - 其它              → 用新值覆盖
    """
    if candidate is None:
        return
    values[key] = str(candidate).strip()


def save_config(
    canvas: dict[str, Any],
    ai: dict[str, Any],
    mail: dict[str, Any] | None,
    env_path: Path,
    settings_path: Path,
) -> None:
    """写入 .env 和 settings.yaml。

    - .env 与 settings.yaml 若已存在，先各备份一份（.backup.{YYYYMMDD_HHMMSS}，不覆盖旧备份）
    - 写入 .env（保留其他行与注释，只更新目标 KEY）
    - 字段三态语义（设置页预填后只改一个字段时用得上）：
        None = 没碰，保留 .env 原值 ／ "" = 清空 ／ 非空字符串 = 覆盖
      canvas.token / ai.api_key / mail.password 三个敏感字段同样支持
    - 更新 settings.yaml 的 poll.sources；配置了邮箱时同时把 mail.provider 改成 imap
      （不改 provider 的话，预检会拿 graph 的必需字段去校验，必然失败）

    :param canvas: {"base_url": "...", "token": None | "..."}
    :param ai: {"base_url": "...", "api_key": None | "...", "model": "..."}
    :param mail: None（跳过邮箱）或
        {"provider": "imap", "host": ..., "username": ..., "password": None | "..."}
    """
    env_path = Path(env_path)
    settings_path = Path(settings_path)

    backup_file(env_path)
    backup_file(settings_path)

    values: dict[str, str] = {}
    _decided(values, "CANVAS_BASE_URL", canvas.get("base_url"))
    _decided(values, "CANVAS_TOKEN", canvas.get("token"))
    _decided(values, "LLM_BASE_URL", ai.get("base_url"))
    _decided(values, "LLM_API_KEY", ai.get("api_key"))
    _decided(values, "LLM_MODEL", ai.get("model"))

    sources = ["canvas"]
    provider: str | None = None
    if mail:
        provider = str(mail.get("provider") or "imap").lower()
        if provider == "imap":
            _decided(values, "IMAP_HOST", mail.get("host"))
            _decided(values, "IMAP_USER", mail.get("username"))
            _decided(values, "IMAP_PASSWORD", mail.get("password"))
        sources.append("mail")

    _update_env(env_path, values)
    _update_settings(settings_path, sources=sources, provider=provider)


def backup_file(path: Path) -> Path | None:
    """把已存在的文件备份为 `{name}.backup.{时间戳}`；不存在则返回 None。

    同一秒内重复调用会追加 `-2` / `-3`，绝不覆盖已有备份。
    """
    path = Path(path)
    if not path.is_file():
        return None

    # 时间戳取本机时区（.astimezone() 顺带满足 DTZ005，也方便用户对时间）
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.backup.{stamp}")
    counter = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup.{stamp}-{counter}")
        counter += 1

    backup.write_bytes(path.read_bytes())
    return backup


def _env_key(line: str) -> str | None:
    """从一行里取 `KEY`；不是 `KEY=VALUE` 形式（注释/空行）返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.partition("=")[0].strip()
    return key or None


def _update_env(env_path: Path, values: dict[str, str]) -> None:
    """逐行更新 .env：命中的 KEY 替换 value，其余行原样保留，缺失的 KEY 追加到末尾。"""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    pending = dict(values)
    output: list[str] = []

    for line in lines:
        key = _env_key(line)
        if key is not None and key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)

    if pending:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={value}" for key, value in pending.items())

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _as_mapping(value: Any, key: str, settings_path: Path) -> dict[str, Any]:
    """把配置片段当映射用；不是映射说明 settings.yaml 被手改坏了 → ValueError。"""
    if not isinstance(value, dict):
        # 外部数据不合规（配置文件被改坏），与项目其它处一致用 ValueError 表达
        raise ValueError(f"配置项 {key} 必须是映射（mapping）：{settings_path}")  # noqa: TRY004
    return value


def _update_settings(settings_path: Path, sources: list[str], provider: str | None) -> None:
    """就地更新 settings.yaml 的 poll.sources（并按需更新 mail.provider）。

    注意：yaml.safe_dump 会丢掉原文件的注释，这是已知取舍（配置向导只在首次/改配置时写）。
    """
    data: dict[str, Any] = {}
    if settings_path.is_file():
        loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        data = _as_mapping({} if loaded is None else loaded, "顶层", settings_path)

    poll = _as_mapping(data.setdefault("poll", {}), "poll", settings_path)
    poll["sources"] = list(sources)

    if provider:
        mail_section = _as_mapping(data.setdefault("mail", {}), "mail", settings_path)
        mail_section["provider"] = provider

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
