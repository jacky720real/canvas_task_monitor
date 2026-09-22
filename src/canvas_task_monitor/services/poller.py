"""轮询编排：拉数据 → 检测变更 → LLM 抽取 → 落库。

【顺序为什么重要】
快照只在 LLM 成功走完流程之后才写。若 LLM 失败却先写了快照，
下一轮检测会认为"这批变更已经处理过"而跳过，任务就永久丢失了。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from ..ai.extractor import TaskExtractor
from ..connectors.base import BaseConnector
from ..diff.change_detector import detect_changes
from ..storage.change_repo import ChangeRepo
from ..storage.snapshot_repo import SnapshotRepo
from ..storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_JITTER_SECONDS = 30
MIN_INTERVAL_SECONDS = 1.0


class Poller:
    """编排一次（或持续）轮询。"""

    def __init__(
        self,
        connectors: list[BaseConnector],
        snapshot_repo: SnapshotRepo,
        task_repo: TaskRepo,
        change_repo: ChangeRepo,
        extractor: TaskExtractor,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        jitter: float = DEFAULT_JITTER_SECONDS,
    ) -> None:
        self.connectors = connectors
        self.snapshots = snapshot_repo
        self.tasks_repo = task_repo
        self.changes = change_repo
        self.extractor = extractor
        self.interval = max(float(interval), MIN_INTERVAL_SECONDS)
        self.jitter = max(float(jitter), 0.0)

    async def poll_once(self) -> dict[str, Any]:
        """跑一轮轮询，返回统计。

        ★ 核心顺序：
          1. 拉数据
          2. 检测变更（不写快照）
          3. 无变更 → continue（跳过 LLM）
          4. 有变更 → 调 LLM（拿 tasks + llm_ok）
          5. ★ LLM 成功后才写快照
          6. 任务落库
          7. 变更日志落库（processed=llm_ok）
        """
        stats: dict[str, Any] = {"sources": 0, "changes": 0, "tasks": 0, "llm_calls": 0}

        for connector in self.connectors:
            stats["sources"] += 1
            try:
                items = await connector.fetch()
            except Exception:  # 单个数据源失败不能让整轮挂掉
                logger.exception("[%s] 拉取失败", connector.name)
                continue

            changes = detect_changes(connector.name, items, self.snapshots)
            stats["changes"] += len(changes)
            if not changes:
                continue  # ★ 无变更跳过 LLM

            tasks, llm_ok = await self.extractor.extract(changes)
            stats["llm_calls"] += 1 if llm_ok else 0
            stats["tasks"] += len(tasks)

            # ★ 只有 LLM 成功走完流程，才写快照；失败则本轮不写，
            #   下轮会重新检测到同一批变更并重试。
            if not llm_ok:
                logger.warning(
                    "[%s] LLM 流程失败，本轮快照与任务不写入，下次轮询重试", connector.name
                )
                self.changes.record_many(changes, processed=False)
                continue

            self.snapshots.upsert_many([change.item for change in changes])
            for task in tasks:
                try:
                    self.tasks_repo.upsert(task)
                except Exception:  # 单条任务写入失败不影响本轮其余任务
                    logger.exception("任务写入失败 %s/%s", task.source, task.external_id)
            self.changes.record_many(changes, processed=True)

        logger.info("轮询完成：%s", stats)
        return stats

    async def run_forever(self) -> None:
        """持续轮询；单轮异常只记录日志，绝不退出循环。"""
        logger.info(
            "开始持续轮询：interval=%.0fs jitter=±%.0fs sources=%s",
            self.interval,
            self.jitter,
            [connector.name for connector in self.connectors],
        )
        while True:
            try:
                await self.poll_once()
            except Exception:  # 守护进程语义：任何异常都不许退出
                logger.exception("轮询轮次异常，%.0fs 后重试", self.interval)
            await asyncio.sleep(self._next_delay())

    def _next_delay(self) -> float:
        """下一轮等待时长：interval ± jitter，且不小于 MIN_INTERVAL_SECONDS。"""
        jittered = self.interval + random.uniform(-self.jitter, self.jitter)
        return max(jittered, MIN_INTERVAL_SECONDS)
