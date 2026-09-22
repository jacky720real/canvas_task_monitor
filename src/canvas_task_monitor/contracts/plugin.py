"""插件契约层。

【设计目标】
本文件定义与宿主无关的 Protocol，用于统一 CLI / MCP / DSH 等所有入口
对业务能力的调用方式。任何新宿主接入只需新建一个入口文件，业务层不动。

【版本策略】
- PLUGIN_API_VERSION：契约版本。宿主加载插件时应先读此常量。
- 遵循语义化版本：宿主契约破坏性变更 → 主版本号 +1。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

PLUGIN_API_VERSION = "1.0.0"


@dataclass(frozen=True)
class Capability:
    """一项可被宿主发现与调用的能力描述。"""

    name: str
    description: str
    params_schema: dict
    returns_schema: dict
    is_async: bool = True


@runtime_checkable
class PluginFacade(Protocol):
    """业务层对外暴露的唯一契约，所有宿主都只认这个接口。"""

    name: str
    version: str
    api_version: str

    async def health(self) -> dict: ...
    async def capabilities(self) -> list[Capability]: ...
    async def invoke(self, action: str, params: dict[str, Any]) -> dict: ...
    async def shutdown(self) -> None: ...
