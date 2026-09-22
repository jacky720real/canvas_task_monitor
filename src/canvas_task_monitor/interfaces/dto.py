"""对外 DTO 转换层。

职责：把存储层的原始 dict 转成业务层对外的标准 dict。
所有宿主（CLI / MCP / DSH）看到的输出结构由本文件定义，保证一致。

规则：
- tags_json -> tags (list)，删除 tags_json
- 删除 raw_json（内部字段）
- 其余字段名保持与 SQL 列一致
"""

from __future__ import annotations

import json
from typing import Any


def task_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw_tags = out.pop("tags_json", "[]")
    try:
        out["tags"] = json.loads(raw_tags) if raw_tags else []
    except (json.JSONDecodeError, TypeError):
        out["tags"] = []
    out.pop("raw_json", None)
    return out
