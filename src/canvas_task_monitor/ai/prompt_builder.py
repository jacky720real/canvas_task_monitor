"""提示词构建：把变更素材渲染进模板，只做占位符替换。

硬性约束（规格 5.3）：只允许 .replace() 替换 {{ now_iso }} 与 {{ changes_json }}，
禁止字符串拼接、禁止追加任何额外提示词。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..core.models import ChangeRecord
from .template import PromptTemplate

_PLACEHOLDER_NOW = "{{ now_iso }}"
_PLACEHOLDER_CHANGES = "{{ changes_json }}"


def build_user_prompt(template: PromptTemplate, changes: list[ChangeRecord]) -> str:
    """渲染 user prompt。

    变更素材只保留 source / external_id / change_type / course_id / data 五个键
    （规格 5.3 明确点名），其余信息一律不进提示词。
    """
    changes_json = json.dumps(_to_prompt_payload(changes), ensure_ascii=False, default=str)
    return template.user_prompt_template.replace(
        _PLACEHOLDER_CHANGES, changes_json
    ).replace(_PLACEHOLDER_NOW, _now_iso())


def _to_prompt_payload(changes: list[ChangeRecord]) -> list[dict[str, Any]]:
    """把 ChangeRecord 压成 LLM 需要的 5 个字段。"""
    return [
        {
            "source": change.source,
            "external_id": change.external_id,
            "change_type": change.change_type,
            "course_id": change.course_id,
            "data": change.data,
        }
        for change in changes
    ]


def _now_iso() -> str:
    """当前时间，带本地时区偏移的 ISO 8601。

    用本机时区而非硬编码 UTC：urgency 锚点里的"今天截止 / 已逾期"必须按学生所在时区判断。
    这里不引入 zoneinfo（Windows 上缺 IANA 数据库会报错），直接取系统时区偏移。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
