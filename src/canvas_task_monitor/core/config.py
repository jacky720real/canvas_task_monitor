"""应用配置加载器：读取 YAML 配置，并递归替换其中的 ${ENV_VAR} 环境变量占位符。"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 只匹配由大写字母、数字、下划线组成的占位符，例如 ${CANVAS_TOKEN}
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class AppConfig(BaseModel):
    """应用配置外壳。

    设计说明：这里只把完整配置字典包一层（raw），不为 settings.yaml 的每个字段单独建模。
    原因：配置项会随迭代频繁增删（新增连接器、调整抽取参数），逐字段建模会让"改配置"
    必须同步"改模型"，配置与代码产生不必要的强耦合。字段级校验交给各自的消费方
    （连接器 / 抽取器）在读取时按需进行。
    """

    raw: dict[str, Any] = Field(default_factory=dict)
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path = "./config/settings.yaml") -> AppConfig:
        """加载 YAML 配置并完成环境变量占位符替换。

        :param path: settings.yaml 路径，默认 ./config/settings.yaml
        :raises FileNotFoundError: 配置文件不存在
        :raises ValueError: 文件内容为空
        :raises TypeError: 配置顶层不是映射（mapping）
        """
        settings_path = Path(path).expanduser().resolve()
        if not settings_path.is_file():
            raise FileNotFoundError(f"配置文件不存在：{settings_path}")

        # .env 约定放在项目根目录（即 settings.yaml 所在目录的上一级）
        dotenv_path = settings_path.parent.parent / ".env"
        load_dotenv(dotenv_path=dotenv_path if dotenv_path.is_file() else None, override=False)

        with settings_path.open("r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)

        if loaded is None:
            raise ValueError(f"配置文件内容为空：{settings_path}")
        if not isinstance(loaded, dict):
            raise TypeError(f"配置文件顶层必须是映射（mapping）：{settings_path}")

        resolved = _resolve_placeholders(loaded)
        if not isinstance(resolved, dict):
            raise TypeError(f"环境变量替换后配置顶层异常：{settings_path}")
        return cls(raw=resolved, source_path=settings_path)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按 "a.b.c" 形式读取配置项；路径不存在时返回 default。"""
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _resolve_placeholders(value: Any) -> Any:
    """递归替换 dict / list / str 中的 ${ENV_VAR} 占位符，其他类型原样返回。"""
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    if isinstance(value, str):
        return _replace_env_in_text(value)
    return value


def _replace_env_in_text(text: str) -> str:
    """替换单个字符串内的全部 ${ENV_VAR}；变量未设置时保留原占位符并告警。"""

    def _substitute(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = os.environ.get(env_name)
        if env_value is None:
            logger.warning("环境变量 %s 未设置，配置项保留占位符 %s", env_name, match.group(0))
            return match.group(0)
        return env_value

    return _ENV_PLACEHOLDER.sub(_substitute, text)


def is_unresolved_placeholder(value: Any) -> bool:
    """判断值是否仍是未解析的 ${ENV_VAR} 占位符。

    core/config.py 在环境变量缺失时保留原占位符（不在加载期抛异常）。
    但装配连接器时，这类占位符等于"用户忘了填 .env"，应视为缺失。
    """
    if not isinstance(value, str):
        return False
    return bool(_ENV_PLACEHOLDER.fullmatch(value.strip()))


def missing_required(cfg: dict, keys: list[str]) -> list[str]:
    """返回 cfg 中缺失（空值 或 未解析占位符）的 key 列表。"""
    missing: list[str] = []
    for key in keys:
        value = cfg.get(key)
        if value is None or value == "" or is_unresolved_placeholder(value):
            missing.append(key)
    return missing
