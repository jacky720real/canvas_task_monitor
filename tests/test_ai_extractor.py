"""抽取器：无变更不调 LLM、失败语义 (llm_ok)、批次级部分失败、分批调用。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from canvas_task_monitor.ai.extractor import TaskExtractor
from canvas_task_monitor.ai.template import PromptTemplate
from canvas_task_monitor.core.models import ChangeRecord


async def test_empty_changes_skips_llm_entirely(
    template: PromptTemplate, fake_llm: Any
) -> None:
    """★ 无变更 → ([], True) 且**绝不调用 LLM**（省 token 的关键）。"""
    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([])

    assert tasks == []
    assert llm_ok is True
    assert fake_llm.calls == 0


async def test_zero_tasks_is_still_success(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    fake_llm: Any,
) -> None:
    """LLM 判定"全是噪声"（0 条任务）也算流程成功。"""
    fake_llm.payloads = [{"tasks": []}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change()])

    assert tasks == []
    assert llm_ok is True


async def test_llm_exception_returns_not_ok(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    fake_llm: Any,
) -> None:
    """LLM 调用失败 → ([], False)：上层据此不写快照，下轮重试。"""
    fake_llm.error = RuntimeError("LLM 挂了")

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change()])

    assert tasks == []
    assert llm_ok is False


async def test_schema_violation_returns_not_ok(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """schema 校验失败 → ([], False)（不能把非法数据写进库）。"""
    fake_llm.payloads = [{"tasks": [llm_entry(urgency=99)]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change()])

    assert tasks == []
    assert llm_ok is False


async def test_partial_batch_failure_keeps_successful_tasks(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """批次级部分失败：保留成功批次的任务，但 llm_ok=False（Poller 不标 processed）。"""
    fake_llm.payloads = [
        {"tasks": [llm_entry()]},
        RuntimeError("第 2 批炸了"),
        {"tasks": [llm_entry()]},
    ]
    extractor = TaskExtractor(template, fake_llm, batch_size=1)

    tasks, llm_ok = await extractor.extract(
        [make_change(1), make_change(2), make_change(3)]
    )

    assert len(tasks) == 2
    assert llm_ok is False
    assert fake_llm.calls == 3


async def test_batch_size_controls_call_count(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    fake_llm: Any,
) -> None:
    """5 条变更 / batch_size=2 → 3 次调用；默认 batch_size=5 → 1 次。"""
    changes = [make_change(index) for index in range(1, 6)]

    await TaskExtractor(template, fake_llm, batch_size=2).extract(changes)
    assert fake_llm.calls == 3

    another = type(fake_llm)()
    await TaskExtractor(template, another).extract(changes)
    assert another.calls == 1


async def test_prompt_carries_changes_and_no_leftover_placeholders(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    fake_llm: Any,
) -> None:
    """提示词渲染：占位符全替换、变更素材进得去（Prompt as Code 的可见证据）。"""
    await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert "作业1" in fake_llm.last_user
    assert "{{ now_iso }}" not in fake_llm.last_user
    assert "{{ changes_json }}" not in fake_llm.last_user
