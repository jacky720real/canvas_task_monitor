"""结构化抽取（Prompt as Code 的调用侧）。

职责：把变更记录分批交给 LLM，校验输出，构造可落库的 TaskItem。

【三条硬约束】
1. 无变更 → 立即返回 ([], True)，绝不调用 LLM（省 token 的关键）；
2. score 一律由本模块用代码计算（LLM 输出里若偷偷带 score，会被显式丢弃并覆盖），
   权重来自 settings.yaml 的 ai.score_weights；
3. LLM 输出必须过 jsonschema 校验；单批失败即置 llm_ok=False，
   上层（Poller）据此不写快照，下一轮重试同一批变更。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.models import ChangeRecord, TaskItem
from .llm_client import LLMClient
from .prompt_builder import build_user_prompt
from .template import PromptTemplate

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 5
DEFAULT_SCORE_WEIGHTS: dict[str, int] = {"urgency": 10, "importance": 8}
SCORE_MIN = 0
SCORE_MAX = 100


class TaskExtractor:
    """按固定模板批量抽取结构化任务。"""

    def __init__(
        self,
        template: PromptTemplate,
        client: LLMClient,
        batch_size: int = DEFAULT_BATCH_SIZE,
        score_weights: dict[str, int] | None = None,
    ) -> None:
        self.template = template
        self.client = client
        self.batch_size = max(int(batch_size), 1)
        self.score_weights = dict(score_weights or DEFAULT_SCORE_WEIGHTS)

    async def extract(self, changes: list[ChangeRecord]) -> tuple[list[TaskItem], bool]:
        """返回 (tasks, llm_ok)。

        llm_ok 语义：
        - True  → LLM 成功走完流程（可能产出 0 条任务，那是"全是噪声"，正常）
        - False → LLM 调用失败 / schema 校验失败 / 部分批次失败

        上层 Poller 用它写 change_log.processed：
        - llm_ok=True  → processed=1
        - llm_ok=False → processed=0（下次重试）

        注意：无变更时直接返回 ([], True)——没调 LLM 也算"流程正常"。
        """
        if not changes:
            # ★ 省 token 的关键：没有变更就绝不调用 LLM
            logger.debug("无变更，跳过 LLM 调用")
            return [], True

        batches = _chunk(changes, self.batch_size)
        logger.info(
            "开始抽取：%d 条变更 → %d 批（batch_size=%d）",
            len(changes),
            len(batches),
            self.batch_size,
        )

        tasks: list[TaskItem] = []
        llm_ok = True
        for index, batch in enumerate(batches, start=1):
            try:
                tasks.extend(await self._extract_batch(batch))
            except Exception as exc:  # noqa: BLE001 —— 单批失败不能拖垮整轮，但要记 llm_ok=False
                llm_ok = False
                logger.error("第 %d/%d 批抽取失败：%s", index, len(batches), exc)
        logger.info("抽取结束：%d 条任务，llm_ok=%s", len(tasks), llm_ok)
        return tasks, llm_ok

    async def _extract_batch(self, batch: list[ChangeRecord]) -> list[TaskItem]:
        """单批流程：构建提示词 → 调 LLM → 清洗 → 校验 schema → 构造 TaskItem。"""
        user_prompt = build_user_prompt(self.template, batch)
        raw = await self.client.complete_json(
            self.template.system_prompt, user_prompt, self.template.output_schema
        )
        # ★ 必须先清掉被禁的 score 再走 schema 校验：
        #   schema 是 additionalProperties=false，若不清洗，模型一多吐一个 score
        #   就会让整批校验失败、这批任务全丢；而且下一轮它很可能重犯 → 永久失败。
        #   清洗范围严格限定在 score 这一个键，其它多余字段仍由 schema 严格拦下。
        raw = _drop_forbidden_score(raw)
        self.template.validate_output(raw)
        return [self._to_task(entry) for entry in (raw.get("tasks") or [])]

    def _to_task(self, entry: dict[str, Any]) -> TaskItem:
        """把一条 LLM 输出转成 TaskItem；score 一律由代码覆盖计算。

        score 的清洗已在 _extract_batch 里做过（校验前），这里的 pop 是第二道防线。
        """
        data = dict(entry)
        if "score" in data:
            logger.warning(
                "LLM 输出了被禁止的 score 字段（%s），已丢弃并按代码公式重算",
                data["score"],
            )
        data.pop("score", None)
        data["score"] = compute_score(data["urgency"], data["importance"], self.score_weights)
        # 原始输出留档：日后排查"这条任务是怎么抽出来的"时只认它
        data["raw_json"] = json.dumps(entry, ensure_ascii=False, default=str)
        return TaskItem(**data)


def _drop_forbidden_score(raw: dict[str, Any]) -> dict[str, Any]:
    """在 schema 校验前，专门剥掉 LLM 违规输出的 score 字段。

    【为什么开这个例外】
    system_prompt 已明令"禁止输出 score"，但历史 LLM 训练语料里"任务打分"
    是常见模式，模型有一定概率无视禁令重犯。若不放行，会因为 schema 的
    additionalProperties: false 导致**整批永久失败**——用户看到的是
    "LLM 一直失败"，很难查根因是模型多吐了一个字段。

    【例外边界】
    只剥 score 一个键，其它多余字段（如 LLM 凭空加的 priority、owner）
    依然由 schema 严格拒绝。本函数不是"宽容兜底"，是针对一个已知历史包袱
    的定向清理。
    """
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        return raw
    cleaned: list[Any] = []
    for entry in tasks:
        if isinstance(entry, dict) and "score" in entry:
            logger.warning(
                "LLM 输出了被禁止的 score 字段（%s），已丢弃并按代码公式重算",
                entry["score"],
            )
            entry = {key: value for key, value in entry.items() if key != "score"}
        cleaned.append(entry)
    result = dict(raw)
    result["tasks"] = cleaned
    return result


def compute_score(urgency: int, importance: int, weights: dict[str, int]) -> int:
    """按权重算综合分，并 clamp 到 [0, 100]。

    clamp 是防御性设计：用户可能把 score_weights.urgency 配成 30，
    5*30 + 5*30 = 300 会越过 tasks.score 的取值上限，落库即脏数据。
    """
    raw = int(urgency) * int(weights.get("urgency", DEFAULT_SCORE_WEIGHTS["urgency"]))
    raw += int(importance) * int(weights.get("importance", DEFAULT_SCORE_WEIGHTS["importance"]))
    return max(SCORE_MIN, min(SCORE_MAX, raw))


def _chunk(items: list[ChangeRecord], size: int) -> list[list[ChangeRecord]]:
    """把变更列表按 batch_size 切批。"""
    return [items[index : index + size] for index in range(0, len(items), size)]
