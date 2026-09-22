"""变更检测。

职责：把本轮从连接器拉到的最新条目，与本地快照对比，
产出 ChangeRecord 列表。不调用 LLM，不写快照。

设计要点：
- 参数 source（连接器名，如 "canvas"）仅用于日志分组。
- 快照键、hash 白名单、变更记录，全部使用条目级 source
  （如 "canvas_assignment"），来自 RawItem.source。
- 远端消失的条目暂不产生 ChangeRecord（避免 LLM 处理删除事件浪费 token），
  留 TODO 见下方。
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.hashing import canonical_hash, hash_fields
from ..core.models import ChangeRecord, RawItem
from ..storage.snapshot_repo import SnapshotRepo

logger = logging.getLogger(__name__)

# TODO(远端消失检测): 需要 BaseConnector 提供 source_kinds 属性
# （返回该连接器覆盖的条目级 source 集合，如 {"canvas_assignment", "canvas_announcement"}），
# Poller 才能遍历这些 kind 调用 repo.list_ids() 找出消失的条目。
# 当前暂缓：删除事件价值低，且需要改动 BaseConnector 接口。
# 详见规格第八部分坑 #4。


def detect_changes(
    source: str,
    items: list[RawItem],
    repo: SnapshotRepo,
) -> list[ChangeRecord]:
    """对比本轮条目与本地快照，产出变更列表。

    本函数**只读快照、不写快照**：落库由调用方（Poller）在 LLM 抽取成功之后执行，
    这样 LLM 失败时下一轮还能拿同一批变更重试。

    :param source: 连接器名（canvas / mail），仅用于日志分组
    :param items: 本轮连接器拉到的原始条目
    :param repo: 快照仓储（只做读操作）
    """
    changes: list[ChangeRecord] = []
    for item in items:
        # 全流程使用条目级 source：它既决定 hash 白名单，也是快照表 / 任务表的键
        new_hash = canonical_hash(item.source, item.payload)
        prev_hash = repo.get_hash(item.source, item.external_id)

        if prev_hash is None:
            record = ChangeRecord.from_item(item, "new", new_hash)
            # 新建时全部白名单字段都算"变化"，便于日后审计这批值的来源
            record.diff["changed_fields"] = list(hash_fields(item.source))
        elif prev_hash != new_hash:
            record = ChangeRecord.from_item(item, "updated", new_hash, prev_hash)
            # 旧 payload 只用于算 changed_fields；读不到（历史上从未落库/JSON 损坏）时按空字典兜底
            old_payload = repo.get_payload(item.source, item.external_id) or {}
            record.diff["changed_fields"] = _diff_fields(item.source, old_payload, item.payload)
        else:
            continue
        changes.append(record)

    if changes:
        logger.info("[%s] 检测到 %d 条变更", source, len(changes))
    else:
        # ★ 这行日志是"省 token 策略"是否生效的观察点：出现它 = 本轮不会调用 LLM
        logger.debug("[%s] 无变更，跳过 LLM", source)
    return changes


def _diff_fields(source: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """对比旧 payload 与新 payload，返回发生变化的哈希白名单字段名。"""
    return [name for name in hash_fields(source) if old.get(name) != new.get(name)]
