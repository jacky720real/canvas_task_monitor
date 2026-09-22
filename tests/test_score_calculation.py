"""score 由代码计算的锚点用例。

覆盖：你点名的 urgency=5 / importance=3 → 74、clamp 到 0~100、
以及"LLM 偷偷输出 score 会被丢弃并按代码重算"。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from canvas_task_monitor.ai.extractor import DEFAULT_SCORE_WEIGHTS, TaskExtractor, compute_score
from canvas_task_monitor.ai.template import PromptTemplate
from canvas_task_monitor.core.models import ChangeRecord


def test_compute_score_anchor_values() -> None:
    """默认权重下：5*10 + 3*8 = 74。"""
    assert compute_score(5, 3, DEFAULT_SCORE_WEIGHTS) == 74
    assert compute_score(0, 0, DEFAULT_SCORE_WEIGHTS) == 0
    assert compute_score(5, 5, DEFAULT_SCORE_WEIGHTS) == 90


def test_compute_score_clamps_into_range() -> None:
    """权重被调大时也必须落在 0~100（防御性 clamp）。"""
    heavy = {"urgency": 30, "importance": 30}

    assert compute_score(5, 5, heavy) == 100
    assert compute_score(-5, -5, heavy) == 0


async def test_extractor_scores_urgency5_importance3_as_74(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """LLM 只给 urgency/importance，score 由代码算出 74。"""
    fake_llm.payloads = [{"tasks": [llm_entry(urgency=5, importance=3)]}]
    extractor = TaskExtractor(template, fake_llm)

    tasks, llm_ok = await extractor.extract([make_change()])

    assert llm_ok is True
    assert len(tasks) == 1
    assert tasks[0].score == 74


async def test_extractor_overrides_llm_supplied_score(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """★ LLM 违规输出 score 时：丢弃它的值，用代码公式重算（且批次不失败）。"""
    fake_llm.payloads = [{"tasks": [llm_entry(urgency=5, importance=3, score=999)]}]
    extractor = TaskExtractor(template, fake_llm)

    tasks, llm_ok = await extractor.extract([make_change()])

    assert llm_ok is True
    assert tasks[0].score == 74
    assert '"score"' not in tasks[0].raw_json


async def test_extractor_applies_configured_weights(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """score_weights 来自配置，改权重即改分数。"""
    fake_llm.payloads = [{"tasks": [llm_entry(urgency=5, importance=5)]}]
    extractor = TaskExtractor(template, fake_llm, score_weights={"urgency": 30, "importance": 30})

    tasks, _ = await extractor.extract([make_change()])

    assert tasks[0].score == 100
