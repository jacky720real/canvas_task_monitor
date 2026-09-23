"""设置页预填（load_current_config）+ 字段三态（save_config）的测试。

不测网络：test_canvas / test_ai 要真发请求，靠人工与 e2e 脚本验证；
这里只钉住"配置读写"与"敏感值不外泄"这两件确定性的事。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import setup_config  # 根级模块，需先把项目根加进 sys.path

FULL_ENV = (
    "CANVAS_BASE_URL=https://demo.instructure.com\n"
    "CANVAS_TOKEN=super-secret-token\n"
    "LLM_BASE_URL=https://api.deepseek.com/v1\n"
    "LLM_API_KEY=sk-secret\n"
    "LLM_MODEL=deepseek-chat\n"
    "IMAP_HOST=imap.qq.com\n"
    "IMAP_USER=u@qq.com\n"
    "IMAP_PASSWORD=mail-secret\n"
)


def _write_env(tmp_path: Path, text: str) -> Path:
    env_path = tmp_path / ".env"
    env_path.write_text(text, encoding="utf-8")
    return env_path


def _write_settings(tmp_path: Path, sources: list[str]) -> Path:
    """写在 <tmp>/config/settings.yaml —— 与真实布局一致（.env 在其上一级）。"""
    settings_path = tmp_path / "config" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {"poll": {"sources": sources}, "mail": {"provider": "imap"}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return settings_path


def test_load_current_config_empty(tmp_path: Path) -> None:
    """没有任何配置文件时返回空结构，而不是抛异常。"""
    settings_path = _write_settings(tmp_path, sources=[])

    current = setup_config.load_current_config(tmp_path / ".env", settings_path)

    assert current["canvas"] == {"base_url": "", "token_saved": False}
    assert current["ai"] == {"base_url": "", "model": "", "api_key_saved": False}
    assert current["mail"] is None  # 没启用 mail 源
    assert current["last_test"]["canvas_ok"] is None
    assert current["last_test"]["canvas_error"] is None
    assert current["last_test"]["tested_at"] is None


def test_load_current_config_hides_sensitive(tmp_path: Path) -> None:
    """token / api_key / password 只回 True/False，绝不能回明文。"""
    env_path = _write_env(tmp_path, FULL_ENV)
    settings_path = _write_settings(tmp_path, sources=["canvas", "mail"])

    current = setup_config.load_current_config(env_path, settings_path)
    dumped = json.dumps(current, ensure_ascii=False)

    assert current["canvas"] == {
        "base_url": "https://demo.instructure.com",
        "token_saved": True,
    }
    assert current["ai"]["base_url"] == "https://api.deepseek.com/v1"
    assert current["ai"]["model"] == "deepseek-chat"
    assert current["ai"]["api_key_saved"] is True
    assert current["mail"]["provider"] == "imap"
    assert current["mail"]["host"] == "imap.qq.com"
    assert current["mail"]["password_saved"] is True

    for secret in ("super-secret-token", "sk-secret", "mail-secret"):
        assert secret not in dumped, f"敏感值 {secret} 泄露到了设置页数据里"


def test_save_with_null_keeps_old_value(tmp_path: Path) -> None:
    """只改一个字段时，其余字段发 null → 保留 .env 原值。"""
    env_path = _write_env(tmp_path, FULL_ENV)
    settings_path = _write_settings(tmp_path, sources=["canvas"])

    setup_config.save_config(
        {"base_url": "https://new.instructure.com", "token": None},
        {"base_url": None, "api_key": None, "model": None},
        {"provider": "imap", "host": None, "username": None, "password": None},
        env_path,
        settings_path,
    )

    env = setup_config.read_env(env_path)
    assert env["CANVAS_BASE_URL"] == "https://new.instructure.com"  # 改了的
    assert env["CANVAS_TOKEN"] == "super-secret-token"  # 没碰 → 原值还在
    assert env["LLM_API_KEY"] == "sk-secret"
    assert env["IMAP_PASSWORD"] == "mail-secret"
    assert env["LLM_MODEL"] == "deepseek-chat"


def test_save_with_empty_string_clears(tmp_path: Path) -> None:
    """空串 = 清空（和"没碰"的 null 不是一回事）。"""
    env_path = _write_env(tmp_path, FULL_ENV)
    settings_path = _write_settings(tmp_path, sources=["canvas"])

    setup_config.save_config(
        {"base_url": None, "token": ""},
        {"base_url": None, "api_key": "", "model": None},
        None,
        env_path,
        settings_path,
    )

    env = setup_config.read_env(env_path)
    assert env["CANVAS_TOKEN"] == ""
    assert env["LLM_API_KEY"] == ""
    assert env["CANVAS_BASE_URL"] == "https://demo.instructure.com"  # 仍是"没碰"
