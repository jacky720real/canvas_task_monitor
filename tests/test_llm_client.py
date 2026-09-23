"""LLM 客户端：空 content 必须可重试（真机遇到 DeepSeek 偶发空响应的补救）。

不碰网络：只把 `_post_chat` 换成"按顺序吐固定 content"的假实现，
重试策略、异常类型、JSON 解析全部走真实代码。
"""

from __future__ import annotations

import pytest

from canvas_task_monitor.ai.llm_client import (
    EmptyLLMResponseError,
    OpenAICompatClient,
    RetryableLLMError,
)

VALID_CONTENT = '{"tasks": []}'


def _client(
    monkeypatch: pytest.MonkeyPatch, contents: list[str]
) -> tuple[OpenAICompatClient, list[int]]:
    """构造不走网络的客户端：_post_chat 依次返回 contents（用尽后重复最后一个）。"""
    client = OpenAICompatClient(
        {
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "sk-test",
            "model": "demo",
            # 退避压到毫秒级：测试别真等几秒（生产值来自 settings.ai.retry）
            "retry": {
                "max_attempts": len(contents),
                "backoff_multiplier": 0.01,
                "backoff_min": 0.01,
                "backoff_max": 0.02,
            },
        }
    )
    calls: list[int] = []
    pending = list(contents)

    async def fake_post_chat(system: str, user: str) -> str:
        calls.append(1)
        return pending.pop(0) if len(pending) > 1 else pending[0]

    monkeypatch.setattr(client, "_post_chat", fake_post_chat)
    return client, calls


async def test_empty_content_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 第一次返回空 content（真机现象）→ 自动重试一次就成功，不再整批失败。"""
    client, calls = _client(monkeypatch, ["", VALID_CONTENT])

    payload = await client.complete_json("sys", "user", {"properties": {}})

    assert payload == {"tasks": []}
    assert len(calls) == 2  # 空了一次 → 触发了重试
    await client.aclose()


async def test_empty_content_exhausts_retries_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一直空 → 抛可读的 EmptyLLMResponseError（且是 RetryableLLMError 子类）。"""
    client, calls = _client(monkeypatch, ["", ""])

    with pytest.raises(EmptyLLMResponseError) as excinfo:
        await client.complete_json("sys", "user", {"properties": {}})

    assert len(calls) == 2  # 用满 max_attempts 才放弃
    assert "空 content" in str(excinfo.value)
    assert isinstance(excinfo.value, RetryableLLMError)
    await client.aclose()
