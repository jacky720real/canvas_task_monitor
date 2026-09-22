"""依赖装配中枢：全项目唯一的 Container。

CLI / MCP / DSH 三个入口都只调这里；业务层不感知宿主。
装配顺序：配置 → 日志 → 存储 → 业务 → AI → 连接器 → Poller → Facade。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..ai.extractor import DEFAULT_BATCH_SIZE, DEFAULT_SCORE_WEIGHTS, TaskExtractor
from ..ai.llm_client import OpenAICompatClient
from ..ai.template import PromptTemplate
from ..connectors.base import BaseConnector
from ..connectors.canvas import CanvasConnector
from ..connectors.graph_mail import GraphMailConnector
from ..connectors.imap_mail import ImapMailConnector
from ..core.config import AppConfig, missing_required
from ..core.logging import setup_logging
from ..storage.change_repo import ChangeRepo
from ..storage.db import Database
from ..storage.snapshot_repo import SnapshotRepo
from ..storage.task_repo import TaskRepo
from .facade import CanvasTaskMonitorFacade
from .poller import DEFAULT_INTERVAL_SECONDS, DEFAULT_JITTER_SECONDS, Poller
from .task_service import TaskService

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = "./config/settings.yaml"
DEFAULT_TEMPLATE_PATH = "./config/templates/task_extract_template.yaml"
DEFAULT_DB_PATH = "./data/tasks.db"


class Container:
    """依赖装配容器：唯一的"接线"地点。"""

    def __init__(self, settings_path: str | Path = DEFAULT_SETTINGS_PATH) -> None:
        # 1. 加载配置（含 ${ENV_VAR} 替换）
        self.cfg = AppConfig.load(settings_path)

        # 2. 初始化日志（幂等，不会覆盖宿主已有 handler）
        setup_logging(str(self.cfg.get("app.log_level", "INFO")))
        logger.debug("配置已加载：%s", self.cfg.source_path)

        # 3. ★ 配置预检（零副作用）：校验所有启用数据源 + LLM 的必需配置。
        #    必须放在任何创建文件/连接的操作之前 —— 否则"配置错误"会留下
        #    data/tasks.db 之类的怪异物（用户只是想看报错，却发现自己多了一个库）。
        self._validate_all_config()

        # 4. 存储层（第一个有副作用的操作）
        self.db = Database(self.cfg.get("app.db_path", DEFAULT_DB_PATH))
        self.snapshots = SnapshotRepo(self.db)
        self.tasks_repo = TaskRepo(self.db)
        self.changes = ChangeRepo(self.db)

        # 5. 业务层
        self.task_service = TaskService(self.tasks_repo)

        # 6. AI 层
        template_path = self.cfg.get("template_path") or DEFAULT_TEMPLATE_PATH
        template = PromptTemplate.load(template_path)
        self._llm = OpenAICompatClient(self.cfg.get("ai") or {})
        self.extractor = TaskExtractor(
            template,
            self._llm,
            batch_size=int(self.cfg.get("ai.batch_size", DEFAULT_BATCH_SIZE)),
            score_weights=self.cfg.get("ai.score_weights", DEFAULT_SCORE_WEIGHTS),
        )

        # 7. 连接器（纯构造，校验已在第 3 步完成）
        self.connectors: list[BaseConnector] = self._build_connectors()

        # 8. Poller
        self.poller = Poller(
            connectors=self.connectors,
            snapshot_repo=self.snapshots,
            task_repo=self.tasks_repo,
            change_repo=self.changes,
            extractor=self.extractor,
            interval=float(self.cfg.get("poll.interval_seconds", DEFAULT_INTERVAL_SECONDS)),
            jitter=float(self.cfg.get("poll.jitter_seconds", DEFAULT_JITTER_SECONDS)),
        )

        # 9. Facade（业务层唯一对外出口）
        self.facade = CanvasTaskMonitorFacade(self)

        logger.info(
            "Container 装配完成：db=%s sources=%s", self.db.path, [c.name for c in self.connectors]
        )

    def _validate_all_config(self) -> None:
        """配置预检：校验所有启用的外部依赖的必需字段。

        失败即抛 RuntimeError，且**不产生任何副作用**（此方法不建库、不建连接）。

        与 _build_connectors() 的分工：
        - _validate_all_config：只校验，不构造
        - _build_connectors：只构造，不校验

        只校验**启用**的数据源（未启用的 provider 允许字段为空），
        错误信息必须同时说明"缺哪个字段"和"怎么修"——用户是学生不是运维。
        """
        sources = self.cfg.get("poll.sources") or []

        if "canvas" in sources:
            missing = missing_required(self.cfg.get("canvas") or {}, ["base_url", "token"])
            if missing:
                fields = ", ".join(f"canvas.{key}" for key in missing)
                raise RuntimeError(
                    f"Canvas 连接器缺少必需配置：{fields}。"
                    f" 请检查 .env 或 config/settings.yaml。"
                )

        if "mail" in sources:
            provider = self.cfg.get("mail.provider", "graph")
            if provider == "graph":
                missing = missing_required(
                    self.cfg.get("mail.graph") or {},
                    ["tenant_id", "client_id", "client_secret", "user"],
                )
                if missing:
                    fields = ", ".join(f"mail.graph.{key}" for key in missing)
                    raise RuntimeError(
                        f"Graph 邮箱连接器缺少必需配置：{fields}。"
                        f" 若租户拿不到应用权限，请在 settings.yaml 把 mail.provider 改成 'imap'。"
                    )
            elif provider == "imap":
                missing = missing_required(
                    self.cfg.get("mail.imap") or {}, ["host", "username", "password"]
                )
                if missing:
                    fields = ", ".join(f"mail.imap.{key}" for key in missing)
                    raise RuntimeError(
                        f"IMAP 邮箱连接器缺少必需配置：{fields}。"
                        f" 请检查 .env 或 config/settings.yaml。"
                    )
            else:
                raise RuntimeError(
                    f"未知的 mail.provider：{provider!r}。允许值：'graph' | 'imap'。"
                )

        # LLM 是三个入口都必需的，无条件校验
        missing_ai = missing_required(self.cfg.get("ai") or {}, ["base_url", "api_key", "model"])
        if missing_ai:
            fields = ", ".join(f"ai.{key}" for key in missing_ai)
            raise RuntimeError(
                f"AI 缺少必需配置：{fields}。 请检查 .env 或 config/settings.yaml。"
            )

    def _build_connectors(self) -> list[BaseConnector]:
        """按 poll.sources 装配连接器（**纯构造，不做校验**）。

        配置合法性由 _validate_all_config() 预先保证，所以这里不再出现
        任何 raise —— 校验与构造分离，才能让预检先于一切副作用执行。
        """
        out: list[BaseConnector] = []
        sources = self.cfg.get("poll.sources") or []

        if "canvas" in sources:
            out.append(CanvasConnector(self.cfg.get("canvas") or {}))

        if "mail" in sources:
            provider = self.cfg.get("mail.provider", "graph")
            if provider == "graph":
                out.append(GraphMailConnector(self.cfg.get("mail.graph") or {}))
            elif provider == "imap":
                out.append(ImapMailConnector(self.cfg.get("mail.imap") or {}))

        return out

    async def aclose(self) -> None:
        """释放所有资源。幂等，可重复调用。"""
        for connector in self.connectors:
            try:
                await connector.aclose()
            except Exception:  # 收尾阶段：任何一个关不掉都不该影响其它
                logger.warning(
                    "连接器 %s 关闭失败", getattr(connector, "name", "unknown"), exc_info=True
                )
        try:
            await self._llm.aclose()
        except Exception:  # 收尾阶段：同上
            logger.warning("LLM 客户端关闭失败", exc_info=True)
        try:
            self.db.close()
        except Exception:  # 收尾阶段：同上
            logger.warning("DB 关闭失败", exc_info=True)

