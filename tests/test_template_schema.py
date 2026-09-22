"""模板与 JSON Schema 的硬约束：score 不得出现在 schema 里。

对应临时冒烟 p5_smoke_ai.py 的模板部分。
"""

from __future__ import annotations

from typing import Any

import pytest

from canvas_task_monitor.ai.template import PromptTemplate

VALID_ENTRY: dict[str, Any] = {
    "source": "canvas_assignment",
    "external_id": "course:1:assignment:9",
    "change_type": "new",
    "category": "assignment",
    "title": "论文初稿",
    "summary": "3000 字",
    "course": "机器学习",
    "due_at": "2026-10-08T23:59:00+08:00",
    "urgency": 5,
    "importance": 3,
    "tags": ["paper"],
    "is_rule": False,
    "urgency_reason": "今天截止",
    "importance_reason": "占比 20%",
}


def _entry(**overrides: Any) -> dict[str, Any]:
    merged = dict(VALID_ENTRY)
    merged.update(overrides)
    return merged


def test_template_basic_metadata(template: PromptTemplate) -> None:
    assert template.version == 1
    assert template.name == "task_extract"
    assert template.allowed_categories == ["assignment", "activity", "reminder"]
    assert "paper" in template.allow_tags


def test_score_must_not_be_in_schema(template: PromptTemplate) -> None:
    """★ score 由代码计算，绝不能出现在 schema 的属性或必填项里。"""
    items = template.output_schema["properties"]["tasks"]["items"]
    assert "score" not in items["properties"]
    assert "score" not in items["required"]


def test_prompt_states_score_is_computed_externally(template: PromptTemplate) -> None:
    """提示词必须显式禁止输出 score（Prompt as Code 的硬规则）。"""
    assert "禁止输出 score 字段" in template.system_prompt
    assert "score 由系统计算" in template.user_prompt_template


def test_change_type_is_optional_property(template: PromptTemplate) -> None:
    """change_type 保留为可选属性，但不在必填项里（tasks 表没有这一列）。"""
    items = template.output_schema["properties"]["tasks"]["items"]
    assert "change_type" in items["properties"]
    assert "change_type" not in items["required"]


def test_valid_output_passes(template: PromptTemplate) -> None:
    template.validate_output({"tasks": [dict(VALID_ENTRY)]})


def test_minimal_output_passes(template: PromptTemplate) -> None:
    """只给必填字段（不含 change_type）也能通过。"""
    items = template.output_schema["properties"]["tasks"]["items"]
    minimal = {key: VALID_ENTRY[key] for key in items["required"]}

    template.validate_output({"tasks": [minimal]})


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"tasks": [{"source": "mail"}]}, id="missing-required"),
        pytest.param({"tasks": [_entry(urgency=99)]}, id="urgency-out-of-range"),
        pytest.param({"tasks": [_entry(category="unknown")]}, id="bad-category"),
        pytest.param({"tasks": [_entry(tags=["whatever"])]}, id="bad-tag"),
        pytest.param({"tasks": [_entry(score=74)]}, id="extra-field-score"),
    ],
)
def test_invalid_output_is_rejected(template: PromptTemplate, payload: dict) -> None:
    """非法输出必须抛 ValueError（上层据此置 llm_ok=False、不写快照）。"""
    with pytest.raises(ValueError, match="不符合 schema"):
        template.validate_output(payload)


def test_error_message_contains_path_and_message(template: PromptTemplate) -> None:
    with pytest.raises(ValueError) as excinfo:
        template.validate_output({"tasks": [_entry(urgency=99)]})

    message = str(excinfo.value)
    assert "['tasks', 0, 'urgency']" in message
    assert "maximum" in message
