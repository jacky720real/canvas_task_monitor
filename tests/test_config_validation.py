"""配置校验：未解析占位符 / 缺 LLM 凭证 → RuntimeError，且**零副作用**。

Phase 6 裁决点名的测试文件。
核心契约：Container 的配置预检必须发生在创建 DB 之前（否则用户只是想看报错，
却发现自己被创建了一个空的 data/tasks.db）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from canvas_task_monitor.core.config import (
    AppConfig,
    is_unresolved_placeholder,
    missing_required,
)
from canvas_task_monitor.services.bootstrap import Container

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "templates" / "task_extract_template.yaml"


def _settings(db_path: str, sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "app": {"db_path": db_path, "log_level": "WARNING"},
        "poll": {"interval_seconds": 600, "sources": sources or [], "jitter_seconds": 30},
        "canvas": {"base_url": "${CANVAS_BASE_URL}", "token": "${CANVAS_TOKEN}"},
        "mail": {
            "provider": "graph",
            "graph": {
                "tenant_id": "${MS_TENANT_ID}",
                "client_id": "${MS_CLIENT_ID}",
                "client_secret": "${MS_CLIENT_SECRET}",
                "user": "${MS_USER_UPN}",
            },
            "imap": {
                "host": "${IMAP_HOST}",
                "username": "${IMAP_USER}",
                "password": "${IMAP_PASSWORD}",
            },
        },
        "ai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "demo"},
        "template_path": str(TEMPLATE),
    }


def _write(tmp_path: Path, data: dict[str, Any], name: str = "settings.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_is_unresolved_placeholder() -> None:
    assert is_unresolved_placeholder("${CANVAS_TOKEN}") is True
    assert is_unresolved_placeholder("  ${CANVAS_TOKEN}  ") is True
    assert is_unresolved_placeholder("real-token") is False
    assert is_unresolved_placeholder("") is False
    assert is_unresolved_placeholder(None) is False
    assert is_unresolved_placeholder(20) is False


def test_missing_required_counts_placeholder_as_missing() -> None:
    cfg = {"base_url": "${CANVAS_BASE_URL}", "token": "real", "timeout": 20}

    assert missing_required(cfg, ["base_url", "token", "timeout"]) == ["base_url"]


def test_empty_env_var_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 CANVAS_TOKEN 设为空环境变量 → 装配直接失败且消息点名 canvas.token。"""
    monkeypatch.setenv("CANVAS_TOKEN", "")
    path = _write(tmp_path, _settings(str(tmp_path / "tasks.db"), sources=["canvas"]))

    with pytest.raises(RuntimeError) as excinfo:
        Container(path)

    assert "canvas.token" in str(excinfo.value)


def test_unresolved_placeholder_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """变量完全未设置 → 加载期保留占位符（延迟 fail-fast），装配期照样拒绝。"""
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    path = _write(tmp_path, _settings(str(tmp_path / "tasks.db"), sources=["canvas"]))

    # 设计前提：加载期不抛异常，而是把占位符留着
    assert AppConfig.load(path).get("canvas.token") == "${CANVAS_TOKEN}"

    with pytest.raises(RuntimeError) as excinfo:
        Container(path)

    assert "canvas.token" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "needle"),
    [("api_key", "ai.api_key"), ("model", "ai.model"), ("base_url", "ai.base_url")],
)
def test_missing_llm_config_is_rejected(
    tmp_path: Path, field: str, needle: str
) -> None:
    """LLM 配置是三个入口的共同依赖，无条件校验。"""
    settings = _settings(str(tmp_path / f"{field}.db"))
    settings["ai"][field] = ""
    path = _write(tmp_path, settings, name=f"{field}.yaml")

    with pytest.raises(RuntimeError) as excinfo:
        Container(path)

    assert needle in str(excinfo.value)


def test_unknown_mail_provider_is_rejected(tmp_path: Path) -> None:
    settings = _settings(str(tmp_path / "tasks.db"), sources=["mail"])
    settings["mail"]["provider"] = "pop3"
    path = _write(tmp_path, settings)

    with pytest.raises(RuntimeError, match="未知的 mail.provider"):
        Container(path)


def test_validation_is_side_effect_free(tmp_path: Path) -> None:
    """★ 预检失败不得留下任何文件（这是把预检提到 Database 之前的原因）。"""
    db_path = tmp_path / "should-not-exist.db"
    path = _write(tmp_path, _settings(str(db_path), sources=["canvas"]))

    with pytest.raises(RuntimeError):
        Container(path)

    assert not db_path.exists()
