"""DSH (DeepSeek Harness) 适配器。

【职责】
- 把 canvas_task_monitor 的能力暴露为 DSH 插件。
- 本文件不写业务逻辑：所有动作都转发给 container.facade。
- DSH 协议破坏性升级时只改本文件。

【允许 import 的东西】
- 标准库：asyncio / sys / pathlib / logging / typing
- 第三方：DSH SDK（待确认包名与 import 路径）
- 本项目：services.bootstrap.Container、contracts.plugin.PluginFacade / Capability / PLUGIN_API_VERSION

【禁止 import】
TaskService / TaskRepo / Poller / TaskExtractor / 任何 connector /
任何 domain 类 / interfaces.dto。

【当前状态：骨架】
DSH 插件 API 文档尚未到位，本文件目前只做三件事：
1. 实现 PluginFacade Protocol 的完整签名（与 MCP / CLI 对齐）
2. 用 TODO(dsh) 标记 4 个待补细节
3. 提供 __main__ 自检块，可在 DSH 缺席时验证适配器可用

【抗破坏性更新】
DSH 升级时只改本文件。业务层零改动。
参照 mcp_server.py 的双版本 shim 模式（mcp 1.x / 2.x 的真实案例）。
"""

# TODO(dsh): 确定 DSH 插件入口函数签名（推测是 register / setup / activate 之一）
# TODO(dsh): 确定 DSH 读取插件元信息的方式（推测是 class 属性 / manifest.json / decorator）
# TODO(dsh): 确定 DSH 是否要求插件实现某个基类
# TODO(dsh): 确定 DSH 是否自带 tool 注册器（若自带，本类的方法可直接挂上去）

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

# 开发期未 pip install -e . 时的兜底：把 src/ 加进 sys.path。
# 注意：绝不要写成 from src.canvas_task_monitor.xxx —— 那会让同一模块被加载两次。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from canvas_task_monitor.contracts.plugin import (
    PLUGIN_API_VERSION,
    Capability,
    PluginFacade,
)
from canvas_task_monitor.services.bootstrap import Container

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = "./config/settings.yaml"

# DSH SDK 的可选 import。文档未到位，先不写死路径。
# 到位后按下面的模式填：
#   try:
#       from dsh.plugin import register as dsh_register   # 待确认
#   except ImportError:
#       dsh_register = None
#
# 骨架阶段：显式声明"DSH SDK 未使用"，且**不因缺席而报错退出**。
# 用 find_spec 而非 import：不执行 SDK 模块级代码（避免触发其副作用，
# 例如某些插件框架在模块顶层注册 handler、起后台线程、读环境变量）。
# 真正的 SDK 调用将在 DSH 文档到位后，在 _register_with_dsh() 里补。
_HAS_DSH_SDK: bool = importlib.util.find_spec("dsh") is not None

if not _HAS_DSH_SDK:
    logger.info("未检测到 DSH SDK（包名待确认）；以骨架模式运行，不影响 CLI / MCP 入口。")


class CanvasTaskMonitorDSHPlugin:
    """DSH 插件实现，遵守 contracts.plugin.PluginFacade Protocol。

    本类目前是**完整的 Protocol 实现**（骨架），所有方法转发给 container.facade。
    DSH 文档到位后，只需在本类上"套一层 DSH 需要的注册/装饰"，转发逻辑不动。

    说明：Python 的 Protocol 是结构化类型，本类**不需要显式继承** PluginFacade；
    只要方法签名匹配即满足协议（可用 isinstance 校验，因为 @runtime_checkable）。
    """

    name: str = "canvas_task_monitor"
    version: str = "0.1.0"
    api_version: str = PLUGIN_API_VERSION

    def __init__(self, settings_path: str = DEFAULT_SETTINGS) -> None:
        self._settings_path = settings_path
        self._container: Container | None = None

    def _get_container(self) -> Container:
        """惰性单例：DSH 可能长时间不调用本插件，不预装配。"""
        if self._container is None:
            self._container = Container(self._settings_path)
        return self._container

    async def health(self) -> dict:
        """探活：转发给 Facade。"""
        return await self._get_container().facade.health()

    async def capabilities(self) -> list[Capability]:
        """能力清单：转发给 Facade。"""
        return await self._get_container().facade.capabilities()

    async def invoke(self, action: str, params: dict[str, Any]) -> dict:
        """统一调用入口：转发给 Facade。"""
        return await self._get_container().facade.invoke(action, params)

    async def shutdown(self) -> None:
        """关闭并释放资源；幂等（重复调用不报错）。"""
        if self._container is not None:
            await self._container.aclose()
            self._container = None


if __name__ == "__main__":
    """自检块：在 DSH 缺席时验证适配器本身可用。

    直接实例化插件、调用两个方法、打印结果。
    这不是单元测试（Phase 9 才写正式 tests），而是"人工 sanity check"。
    """
    import asyncio
    import json

    logging.basicConfig(level=logging.INFO)

    async def _self_check() -> int:
        plugin = CanvasTaskMonitorDSHPlugin()
        print(f"[dsh_plugin] api_version = {plugin.api_version}")
        print(f"[dsh_plugin] _HAS_DSH_SDK = {_HAS_DSH_SDK}")
        print(f"[dsh_plugin] 满足 PluginFacade 协议 = {isinstance(plugin, PluginFacade)}")

        try:
            capabilities = await plugin.capabilities()
        except Exception as exc:  # noqa: BLE001 —— 自检块：任何异常都只想给友好提示
            print(f"[dsh_plugin] capabilities() 失败（可能缺配置）：{exc}")
            print("[dsh_plugin] 提示：请检查 .env / config/settings.yaml")
            return 2

        print(f"[dsh_plugin] capabilities = {len(capabilities)} 项")
        for capability in capabilities:
            print(f"  - {capability.name}: {capability.description}")

        result = await plugin.invoke("summarize_pending", {})
        print(f"[dsh_plugin] summarize_pending = {json.dumps(result, ensure_ascii=False)}")

        await plugin.shutdown()
        return 0

    sys.exit(asyncio.run(_self_check()))

