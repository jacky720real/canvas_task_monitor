"""内容哈希：按 source 的字段白名单计算稳定指纹，用于识别真实变更。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 字段白名单：只有登记在册的字段参与哈希计算。
# 为什么不哈希全量 payload：Canvas 返回体里的 updated_at / html_url 之类字段几乎每次都变，
# 全量算哈希会把"其实什么都没变"误报成变更，进而白白烧掉 LLM token。
# 刻意保持私有：外部一律通过 hash_fields() 读取，避免各处直接依赖这个字典结构。
_HASH_FIELDS: dict[str, tuple[str, ...]] = {
    "canvas_assignment": ("name", "description", "due_at", "points_possible", "submission_types"),
    "canvas_announcement": ("title", "message", "posted_at"),
    "mail": ("subject", "from", "receivedDateTime", "bodyPreview"),
}


def hash_fields(source: str) -> tuple[str, ...]:
    """返回某个 source 参与哈希的字段白名单（供变更检测计算 changed_fields）。

    :raises ValueError: 该 source 尚未登记白名单
    """
    fields = _HASH_FIELDS.get(source)
    if fields is None:
        raise ValueError(f"未定义哈希字段白名单的 source：{source}（请先在 _HASH_FIELDS 中登记）")
    return fields


def canonical_hash(source: str, payload: dict[str, Any]) -> str:
    """按 source 对应的字段白名单计算 sha256 内容指纹。

    :param source: 条目级来源（canvas_assignment / canvas_announcement / mail）
    :param payload: 原始条目的 data 字典
    :raises ValueError: 该 source 尚未登记字段白名单（刻意不提供隐式兜底，
        避免"悄悄用全量字段算哈希"这种会烧 token 的行为）
    """
    subset = {name: payload.get(name) for name in hash_fields(source)}
    canonical = json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
