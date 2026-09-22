"""连接器抽象基类：定义"从某个数据源拉取原始条目"的统一契约。

职责边界（重要）：连接器只负责"拉数据"，既不判断变更、也不碰数据库。
变更判断归 diff 层，落库归 storage 层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import RawItem
from ..core.rate_limiter import TokenBucket


class BaseConnector(ABC):
    """所有数据源连接器的基类。

    每个连接器在构造函数里创建**自己**的 TokenBucket，
    因此 Canvas 与 Mail（以及 Graph / IMAP）的限流互不干扰，配置项也是分开的。
    """

    #: 连接器名，用于日志与 Poller 编排；子类必须覆写
    name: str = ""

    def __init__(self, rate_limit_rps: float, burst: int = 1) -> None:
        self.bucket = TokenBucket(rate_per_sec=rate_limit_rps, burst=burst)

    @abstractmethod
    async def fetch(self) -> list[RawItem]:
        """拉取一批原始条目。失败时抛异常，由 Poller 负责捕获并记录。"""

    async def aclose(self) -> None:
        """释放连接资源；默认无可释放资源，子类按需覆写。"""
        return
