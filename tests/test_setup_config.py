"""setup_config.py 的纯逻辑测试（**不测试网络**）。

配置向导的"测试连接"按钮背后是 httpx 真发请求，那部分靠人工 / curl 验证（见 README）；
这里只钉住配置文件读写的确定性行为：
判定是否已配置 / 从零创建 .env / 备份既有文件 / 更新 poll.sources / 保留无关行与注释。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import setup_config  # 根级模块，需先把项目根加进 sys.path

VALID_ENV = (
    "CANVAS_BASE_URL=https://demo.instructure.com\n"
    "CANVAS_TOKEN=canvas-token\n"
    "LLM_BASE_URL=https://api.deepseek.com/v1\n"
    "LLM_API_KEY=sk-demo\n"
    "LLM_MODEL=deepseek-chat\n"
)

SETTINGS = {
    "app": {"db_path": "./data/tasks.db", "log_level": "INFO"},
    "poll": {"interval_seconds": 600, "sources": ["canvas", "mail"]},
    "mail": {"provider": "graph", "graph": {}, "imap": {}},
    "ai": {"score_weights": {"urgency": 10, "importance": 8}},
    "template_path": "./config/templates/task_extract_template.yaml",
}


def _write_settings(tmp_path: Path) -> Path:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(SETTINGS, allow_unicode=True), encoding="utf-8")
    return settings_path


def _canvas() -> dict[str, str]:
    return {"base_url": "https://demo.instructure.com", "token": "canvas-token"}


def _ai() -> dict[str, str]:
    return {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-demo",
        "model": "deepseek-chat",
    }


def _imap() -> dict[str, Any]:
    return {
        "provider": "imap",
        "host": "imap.qq.com",
        "username": "u@qq.com",
        "password": "auth-code",
    }


def test_is_configured_returns_false_when_env_missing(tmp_path: Path) -> None:
    assert setup_config.is_configured(tmp_path / ".env", _write_settings(tmp_path)) is False


def test_is_configured_returns_false_when_placeholder(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "CANVAS_BASE_URL=${CANVAS_BASE_URL}\n"
        "CANVAS_TOKEN=${CANVAS_TOKEN}\n"
        "LLM_BASE_URL=${LLM_BASE_URL}\n"
        "LLM_API_KEY=${LLM_API_KEY}\n",
        encoding="utf-8",
    )

    assert setup_config.is_configured(env_path, _write_settings(tmp_path)) is False


def test_is_configured_returns_true_when_valid(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(VALID_ENV, encoding="utf-8")

    assert setup_config.is_configured(env_path, _write_settings(tmp_path)) is True


def test_save_config_creates_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    settings_path = _write_settings(tmp_path)

    setup_config.save_config(_canvas(), _ai(), None, env_path, settings_path)

    env = setup_config.read_env(env_path)
    assert env["CANVAS_BASE_URL"] == "https://demo.instructure.com"
    assert env["CANVAS_TOKEN"] == "canvas-token"
    assert env["LLM_MODEL"] == "deepseek-chat"
    assert setup_config.is_configured(env_path, settings_path) is True


def test_save_config_backs_up_existing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("CANVAS_TOKEN=old-token\n", encoding="utf-8")
    settings_path = _write_settings(tmp_path)

    setup_config.save_config(_canvas(), _ai(), None, env_path, settings_path)

    backups = sorted(tmp_path.glob(".env.backup.*"))
    assert len(backups) == 1
    # 备份里是"改动前"的内容（硬约束：改配置前先备份，且带时间戳不覆盖旧备份）
    assert "old-token" in backups[0].read_text(encoding="utf-8")
    assert sorted(tmp_path.glob("settings.yaml.backup.*"))
    assert setup_config.read_env(env_path)["CANVAS_TOKEN"] == "canvas-token"


def test_save_config_updates_settings_sources(tmp_path: Path) -> None:
    settings_path = _write_settings(tmp_path)
    env_path = tmp_path / ".env"

    setup_config.save_config(_canvas(), _ai(), None, env_path, settings_path)
    skipped = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert skipped["poll"]["sources"] == ["canvas"]
    assert skipped["app"]["db_path"] == "./data/tasks.db"  # 其余段落保持不动
    assert skipped["mail"]["provider"] == "graph"  # 没配邮箱就不动 provider

    setup_config.save_config(_canvas(), _ai(), _imap(), env_path, settings_path)
    with_mail = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert with_mail["poll"]["sources"] == ["canvas", "mail"]
    assert with_mail["mail"]["provider"] == "imap"  # 否则预检会去要 graph 的字段
    assert setup_config.read_env(env_path)["IMAP_HOST"] == "imap.qq.com"


def test_save_config_preserves_other_env_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# 我自己的注释\nMS_TENANT_ID=tenant-keep\nCANVAS_TOKEN=old-token\n", encoding="utf-8"
    )

    setup_config.save_config(_canvas(), _ai(), None, env_path, _write_settings(tmp_path))

    text = env_path.read_text(encoding="utf-8")
    assert "# 我自己的注释" in text
    assert setup_config.read_env(env_path)["MS_TENANT_ID"] == "tenant-keep"
    assert setup_config.read_env(env_path)["CANVAS_TOKEN"] == "canvas-token"
