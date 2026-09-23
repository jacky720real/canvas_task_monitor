"""LLM 客户端：OpenAI 兼容 /chat/completions，强制 JSON 输出。

【为什么只发 response_format=json_object】
DeepSeek 等 OpenAI 兼容服务主要支持 json_object 模式，不保证支持 json_schema 严格模式。
结构化约束由我们自己的 jsonschema 校验兜底（见 ai/template.py），
所以 complete_json 收到的 schema 只用于日志与排障，不发给服务端。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_WAIT_MULTIPLIER = 1.5
_WAIT_MIN = 2
_WAIT_MAX = 20
_DEFAULT_TIMEOUT_SECONDS = 60.0
_FENCE = "```"


class LLMClient(Protocol):
    """LLM 客户端契约：任何实现只需提供 complete_json。"""

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class RetryableLLMError(RuntimeError):
    """可重试的 LLM 错误（429 / 5xx / 网络异常）。"""


class EmptyLLMResponseError(RetryableLLMError):
    """LLM 返回了空 content（DeepSeek 偶发）。

    【为什么单独成类，并且归入"可重试"】
    真机跑 DeepSeek 时遇到偶发空 content：HTTP 200、choices 也在、finish_reason=stop，
    但 message.content 是空串。以前这种情况会让 json.loads("") 抛 JSONDecodeError，
    而 JSONDecodeError 不在 tenacity 的重试名单里 —— 于是**整批（5 条变更）直接判失败、
    这 5 条任务的 LLM 调用全部白花**，日志里只有一句"Expecting value: line 1 column 1"。

    空响应属于典型的瞬时故障，原样重发一次多半就正常了，所以这里明确标成可重试：
    交给 complete_json 里既有的重试策略处理（次数与退避由 settings.ai.retry 控制）。
    """


class OpenAICompatClient:
    """OpenAI 兼容客户端（按 settings.yaml 的 ai 段构造）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.api_key = str(cfg.get("api_key", ""))
        self.model = str(cfg.get("model", ""))
        self.temperature = float(cfg.get("temperature", 0.1))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 2000))
        self.timeout = float(cfg.get("timeout", _DEFAULT_TIMEOUT_SECONDS))
        retry_cfg = cfg.get("retry") or {}
        self.max_attempts = max(int(retry_cfg.get("max_attempts", _MAX_ATTEMPTS)), 1)
        self.backoff_multiplier = float(retry_cfg.get("backoff_multiplier", _WAIT_MULTIPLIER))
        self.backoff_min = float(retry_cfg.get("backoff_min", _WAIT_MIN))
        self.backoff_max = float(retry_cfg.get("backoff_max", _WAIT_MAX))
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        """惰性创建并复用 AsyncClient。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """调用模型并返回解析后的 JSON 对象。

        :raises ValueError: 返回内容不是合法 JSON，或顶层不是对象
        :raises EmptyLLMResponseError: 重试耗尽后 content 仍为空（RetryableLLMError 子类）
        :raises RetryableLLMError: 重试耗尽后仍是 429 / 5xx / 网络异常
        :raises RuntimeError: 不可重试的 HTTP 错误（如 400 / 401）
        """
        logger.debug(
            "调用 LLM：model=%s schema_properties=%s",
            self.model,
            sorted((schema.get("properties") or {}).keys()),
        )
        # 重试参数按实例读（值来自 settings.ai.retry），所以不能用装饰器：
        # tenacity 的 @retry 参数在类定义时求值，拿不到实例属性。
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(
                multiplier=self.backoff_multiplier,
                min=self.backoff_min,
                max=self.backoff_max,
            ),
            retry=retry_if_exception_type(RetryableLLMError),
            reraise=True,
        )
        content = ""
        async for attempt in retrying:
            with attempt:
                content = await self._post_chat(system, user)
                if not content.strip():
                    # 【DeepSeek 偶发空 content 的补救】
                    # 空响应是瞬时故障：标成可重试，由上面的重试策略（settings.ai.retry）
                    # 再试；次数用完仍为空才抛给上层 —— extractor 记 llm_ok=False，
                    # Poller 不写快照，下一轮重试同一批变更。
                    raise EmptyLLMResponseError(
                        "LLM 返回空 content（HTTP 200 但 message.content 为空）"
                    )
        return _loads_json_object(content)

    async def _post_chat(self, system: str, user: str) -> str:
        """发一次请求并返回 assistant 的 content 原文（重试由调用方控制）。"""
        client = await self._http()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise RetryableLLMError(f"LLM 请求异常：{exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableLLMError(f"LLM 返回 {response.status_code}：{response.text[:200]}")
        if response.status_code != 200:
            raise RuntimeError(f"LLM 返回 {response.status_code}：{response.text[:200]}")

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("LLM 响应缺少 choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        if not content.strip():
            # 这里只记日志、不抛异常：重试决策统一放在 complete_json 的重试循环里。
            # finish_reason 是排查关键：stop=真·偶发空响应，length=输出被 max_tokens 截断。
            logger.warning(
                "LLM 返回空 content：finish_reason=%s"
                "（stop=偶发空响应；length=被 max_tokens 截断，需调大 ai.max_output_tokens）",
                choices[0].get("finish_reason"),
            )
        return content


def _strip_code_fence(content: str) -> str:
    """剥掉 ``` / ```json 围栏。

    system_prompt 里已经禁了 markdown 代码块，但模型未必听话，这里做一层兜底。
    """
    text = content.strip()
    if not text.startswith(_FENCE):
        return text
    text = text[len(_FENCE) :]
    # 去掉首行的语言标识（如 json）；没有换行就保持原样，避免把内容整段丢掉
    newline = text.find("\n")
    if newline != -1:
        text = text[newline + 1 :]
    stripped = text.rstrip().removesuffix(_FENCE)
    return stripped.strip()


def _loads_json_object(content: str) -> dict[str, Any]:
    """解析 JSON 对象；解析失败让 json.JSONDecodeError 抛给上层计入 llm_ok=False。"""
    parsed = json.loads(_strip_code_fence(content))
    if not isinstance(parsed, dict):
        # 与 template.validate_output 保持一致：这是"外部数据不合规"，不是调用方类型错误，
        # 项目内统一用 ValueError 表达
        raise ValueError(  # noqa: TRY004
            f"LLM 输出顶层必须是 JSON 对象，实际是 {type(parsed).__name__}"
        )
    return parsed
