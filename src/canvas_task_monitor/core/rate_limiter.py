"""异步令牌桶限流器：为每个数据源提供互相独立的请求速率控制。"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """异步令牌桶。

    每个连接器持有自己的实例，因此 Canvas 与 Mail 的限流互不干扰
    （settings.yaml 里两个 rate_limit_rps 也是分开配置的）。

    实现要点：不做后台定时器，而是以"上次补充时间"为基准按时间差补令牌。
    """

    def __init__(self, rate_per_sec: float, burst: int = 1) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec 必须大于 0，当前为 {rate_per_sec}")
        if burst < 1:
            raise ValueError(f"burst 必须大于等于 1，当前为 {burst}")
        self.rate_per_sec = float(rate_per_sec)
        self.burst = int(burst)
        self._tokens = float(burst)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """申请令牌，不足时异步等待直到可放行。

        等待期间持有锁：宁可让并发请求排队，也不允许令牌被重复透支。
        """
        if tokens > self.burst:
            raise ValueError(f"单次申请的令牌数 {tokens} 超过桶容量 {self.burst}")

        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep(self._wait_seconds(tokens))

    def _refill(self) -> None:
        """按经过的时间补充令牌，上限为桶容量。"""
        now = time.monotonic()
        elapsed = now - self._updated_at
        if elapsed <= 0:
            return
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_sec)
        self._updated_at = now

    def _wait_seconds(self, tokens: float) -> float:
        """计算还需等待多久才能凑够 tokens 个令牌。"""
        missing = tokens - self._tokens
        return max(missing / self.rate_per_sec, 0.01)
