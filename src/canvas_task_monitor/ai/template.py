"""Prompt 模板加载与输出校验（Prompt as Code 的模板侧）。

职责：把 config/templates/*.yaml 读成对象，并提供 JSON Schema 校验能力。
本模块不含任何业务判断，也不拼提示词（拼装见 prompt_builder.py）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml

logger = logging.getLogger(__name__)

# 模板里只允许这两个占位符（规格第三部分的硬性约束）
_ALLOWED_PLACEHOLDERS = frozenset({"now_iso", "changes_json"})
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class PromptTemplate:
    """任务抽取模板的只读视图。"""

    def __init__(self, raw: dict[str, Any], path: Path | None = None) -> None:
        self.raw = raw
        self.path = path
        self.version = int(raw.get("version") or 0)
        self.name = str(raw.get("name") or "")
        self.description = str(raw.get("description") or "")
        self.system_prompt = str(raw.get("system_prompt") or "")
        self.user_prompt_template = str(raw.get("user_prompt_template") or "")
        self.output_schema: dict[str, Any] = raw.get("output_schema") or {}
        self.allow_tags: list[str] = list(raw.get("allow_tags") or [])
        self.allowed_categories: list[str] = list(raw.get("allowed_categories") or [])
        self._validator = jsonschema.Draft202012Validator(self.output_schema)
        self._check_placeholders()

    @classmethod
    def load(cls, path: str | Path) -> PromptTemplate:
        """从 YAML 文件加载模板。

        :raises FileNotFoundError: 模板文件不存在
        :raises TypeError: 模板顶层不是映射
        """
        template_path = Path(path).expanduser().resolve()
        if not template_path.is_file():
            raise FileNotFoundError(f"Prompt 模板不存在：{template_path}")
        with template_path.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
        if not isinstance(raw, dict):
            raise TypeError(f"Prompt 模板顶层必须是映射（mapping）：{template_path}")
        return cls(raw, template_path)

    def validate_output(self, data: dict[str, Any]) -> None:
        """用 output_schema 校验 LLM 输出。

        :raises ValueError: 校验不通过，错误信息里带前 5 条错误的 path + message
        """
        errors = sorted(self._validator.iter_errors(data), key=_error_sort_key)
        if errors:
            msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
            raise ValueError(f"LLM 输出不符合 schema：{msgs}")
        logger.debug("LLM 输出通过 schema 校验：%d 条任务", len(data.get("tasks") or []))

    def _check_placeholders(self) -> None:
        """出现未登记的占位符就直接失败，防止有人偷偷加第三个占位符。"""
        found = set(_PLACEHOLDER_PATTERN.findall(self.user_prompt_template))
        unknown = found - _ALLOWED_PLACEHOLDERS
        if unknown:
            raise ValueError(f"user_prompt_template 出现未允许的占位符：{sorted(unknown)}")


def _error_sort_key(error: jsonschema.ValidationError) -> list[str]:
    """把 path 统一转成字符串再排序：数组下标是 int，直接比较会 TypeError。"""
    return [str(part) for part in error.path]
