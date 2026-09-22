"""领域模型定义：原始条目、变更记录、结构化任务与快照行。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RawItem(BaseModel):
    """采集器返回的原始条目（事实层，未经任何 AI 处理）。

    source 取条目级来源：canvas_assignment / canvas_announcement / mail。
    它既决定内容哈希的字段白名单（见 core/hashing.py），也是 tasks 表主键的一半。
    """

    source: str
    # 边界说明：邮件 Message-ID 理论上可能重复（极少见）。
    # 现实场景中学邮箱几乎不会遇到；若遇到，UNIQUE 约束会保留首次入库版本，
    # 后续同 ID 邮件被视作同一封，属于可接受的降级行为。
    external_id: str
    course_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ChangeRecord(BaseModel):
    """一条检测到的变更，同时是 LLM 抽取的输入素材。

    扁平字段（source / external_id / change_type / course_id / data）是送进提示词的部分；
    item 保留完整原始条目，供 Poller 写入快照表使用。
    """

    source: str
    external_id: str
    change_type: Literal["new", "updated", "removed"]
    course_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
    item: RawItem

    @classmethod
    def from_item(
        cls,
        item: RawItem,
        change_type: Literal["new", "updated", "removed"],
        new_hash: str,
        prev_hash: str | None = None,
    ) -> ChangeRecord:
        """由原始条目构造变更记录，保证扁平字段与 item 永不脱节。

        本方法只负责"基础字段 + hash diff"这一件事；
        changed_fields 需要旧 payload 才能算，由 diff/change_detector.py 补填
        （单一职责：这里拿不到历史快照）。
        """
        return cls(
            source=item.source,
            external_id=item.external_id,
            change_type=change_type,
            course_id=item.course_id,
            data=item.data,
            diff={"hash": {"from": prev_hash, "to": new_hash}},
            item=item,
        )


class TaskItem(BaseModel):
    """LLM 抽取后落库的结构化任务。"""

    id: int | None = None
    source: str
    external_id: str
    category: Literal["assignment", "activity", "reminder"]
    title: str
    summary: str = ""
    course: str = ""
    due_at: str | None = None
    urgency: int = Field(default=0, ge=0, le=5)
    importance: int = Field(default=0, ge=0, le=5)
    score: int = Field(default=0, ge=0, le=100)
    # 注意：score 不由 LLM 输出，由 extractor 按 config.ai.score_weights 计算后填充。
    tags: list[str] = Field(default_factory=list)
    is_rule: bool = False
    urgency_reason: str = ""
    importance_reason: str = ""
    status: Literal["pending", "done"] = "pending"
    # 注意：status 由本地用户维护（勾选完成 / 取消完成），LLM 不参与，
    # task_repo.upsert() 的 SET 子句也刻意排除它，见 storage/task_repo.py。
    raw_json: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class SnapshotRow(BaseModel):
    """snapshots 表的一行快照（事实层）。"""

    id: int | None = None
    source: str
    external_id: str
    course_id: str | None = None
    content_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: str | None = None
    last_seen_at: str | None = None
