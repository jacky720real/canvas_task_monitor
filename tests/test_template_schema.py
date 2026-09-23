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
        # tags 不再是 enum（见 test_tags_is_free_string_array），但长度仍有约束
        pytest.param({"tasks": [_entry(tags=["x" * 31])]}, id="tag-too-long"),
        pytest.param({"tasks": [_entry(score=74)]}, id="extra-field-score"),
    ],
)
def test_invalid_output_is_rejected(template: PromptTemplate, payload: dict) -> None:
    """非法输出必须抛 ValueError（上层据此置 llm_ok=False、不写快照）。"""
    with pytest.raises(ValueError, match="不符合 schema"):
        template.validate_output(payload)


def test_tags_is_free_string_array(template: PromptTemplate) -> None:
    """★ tags 不能是 enum：真机上 LLM 输出枚举外标签（scholarship 等）曾让整批失败。

    maxItems 故意放宽到 10（留余量防 LLM 偶发多给几个 tag 就整批失败）；
    真正落库上限 5 由 extractor 的 _sanitize_tags() 控制（见 test_ai_extractor.py）。
    """
    tags_schema = template.output_schema["properties"]["tasks"]["items"]["properties"]["tags"]

    assert "enum" not in tags_schema["items"], "tags 不应有 enum 约束"
    assert tags_schema["maxItems"] == 10
    assert tags_schema["items"]["maxLength"] == 30


def test_allow_tags_still_declared(template: PromptTemplate) -> None:
    """allow_tags 仍需声明：它现在是 prompt 引导 + extractor 过滤的白名单。"""
    assert len(template.allow_tags) > 0


def test_error_message_contains_path_and_message(template: PromptTemplate) -> None:
    with pytest.raises(ValueError) as excinfo:
        template.validate_output({"tasks": [_entry(urgency=99)]})

    message = str(excinfo.value)
    assert "['tasks', 0, 'urgency']" in message
    assert "maximum" in message


def test_prompt_lists_output_field_names(template: PromptTemplate) -> None:
    """★ 真机教训：提示词必须把字段清单 + "别用错名字"写清楚。

    背景：只写"必须匹配 schema"时，LLM 会猜成 course_id / course_name / reason，
    整批因 additionalProperties=false 失败。这条是防回退的护栏。
    """
    prompt = template.user_prompt_template

    assert "## 输出字段清单" in prompt
    for field in (
        "source",
        "external_id",
        "category",
        "title",
        "urgency_reason",
        "importance_reason",
    ):
        assert field in prompt
    # 高频错名字必须被点名（真机失败的直接原因）
    assert "course_id" in prompt and "course_name" in prompt
    assert "正确是 course" in prompt
    # 只加说明、不加占位符：模板占位符依然只有 now_iso / changes_json 两个
    assert prompt.count("{{") == 2

