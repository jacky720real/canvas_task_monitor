"""抽取器：无变更不调 LLM、失败语义 (llm_ok)、批次级部分失败、分批调用、空响应处理。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from canvas_task_monitor.ai.extractor import TaskExtractor
from canvas_task_monitor.ai.llm_client import EmptyLLMResponseError
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


async def test_empty_content_failure_is_logged_readably(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    fake_llm: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """客户端重试后仍为空 → 该批记 llm_ok=False，且日志能一眼看出是"空 content"。

    真机上原本只看到 `Expecting value: line 1 column 1 (char 0)`，看不出根因是空响应。
    """
    fake_llm.error = EmptyLLMResponseError("LLM 返回空 content（HTTP 200 但 message.content 为空）")

    with caplog.at_level(logging.ERROR):
        tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert tasks == []
    assert llm_ok is False  # 上层据此不写快照，下轮重试这批变更
    assert "空 content" in caplog.text


async def test_tags_filtered_against_allowlist(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """★ 真机：LLM 给出白名单外的 tags（scholarship 等）不再让整批失败，只静默过滤。"""
    fake_llm.payloads = [{"tasks": [llm_entry(tags=["exam", "scholarship", "library_skills"])]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert llm_ok is True  # 不是失败：整批任务保住了
    assert len(tasks) == 1
    assert tasks[0].tags == ["exam"]
    # 原始输出仍留档：日后能查出 LLM 原本给的是什么标签
    assert "scholarship" in (tasks[0].raw_json or "")


async def test_tags_all_invalid_returns_empty(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """全部不在白名单 → tags 变空数组，任务照样创建（不因标签丢任务）。"""
    fake_llm.payloads = [{"tasks": [llm_entry(tags=["scholarship", "foo"])]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert llm_ok is True
    assert len(tasks) == 1
    assert tasks[0].tags == []


async def test_tags_up_to_schema_limit_are_kept(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """5 个标签（= schema 上限）原样保留：extractor 的 [:5] 不会砍掉合法输入。"""
    five = ["exam", "paper", "project", "quiz", "discussion"]
    fake_llm.payloads = [{"tasks": [llm_entry(tags=five)]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert llm_ok is True
    assert tasks[0].tags == five


async def test_tags_over_extractor_limit_are_trimmed(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """7 个 tags（5 个白名单内 + 2 个白名单外）→ schema 放行 → 落库只留 5 个白名单内。

    schema 上限是 10（留余量，防 LLM 偶发多给几个 tag 就整批失败），
    真正落库上限由 _sanitize_tags 的 [:5] 控制。
    """
    seven = ["exam", "paper", "project", "quiz", "discussion", "scholarship", "webwork"]
    fake_llm.payloads = [{"tasks": [llm_entry(tags=seven)]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert llm_ok is True
    assert tasks[0].tags == ["exam", "paper", "project", "quiz", "discussion"]


async def test_tags_missing_is_tolerated(
    template: PromptTemplate,
    make_change: Callable[..., ChangeRecord],
    llm_entry: Callable[..., dict],
    fake_llm: Any,
) -> None:
    """LLM 干脆不输出 tags（可选字段）→ 空数组，不报错。"""
    entry = llm_entry()
    entry.pop("tags")
    fake_llm.payloads = [{"tasks": [entry]}]

    tasks, llm_ok = await TaskExtractor(template, fake_llm).extract([make_change(1)])

    assert llm_ok is True
    assert tasks[0].tags == []


