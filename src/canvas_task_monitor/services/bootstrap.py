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

        # 3. 存储层
        self.db = Database(self.cfg.get("app.db_path", DEFAULT_DB_PATH))
        self.snapshots = SnapshotRepo(self.db)
        self.tasks_repo = TaskRepo(self.db)
        self.changes = ChangeRepo(self.db)

        # 4. 业务层
        self.task_service = TaskService(self.tasks_repo)

        # 5. AI 层
        template_path = self.cfg.get("template_path") or DEFAULT_TEMPLATE_PATH
        template = PromptTemplate.load(template_path)
        self._llm = OpenAICompatClient(self.cfg.get("ai") or {})
        self.extractor = TaskExtractor(
            template,
            self._llm,
            batch_size=int(self.cfg.get("ai.batch_size", DEFAULT_BATCH_SIZE)),
            score_weights=self.cfg.get("ai.score_weights", DEFAULT_SCORE_WEIGHTS),
        )

        # 6. 连接器（含延迟 fail-fast 校验）
        self.connectors: list[BaseConnector] = self._build_connectors()

        # 7. Poller
        self.poller = Poller(
            connectors=self.connectors,
            snapshot_repo=self.snapshots,
            task_repo=self.tasks_repo,
            change_repo=self.changes,
            extractor=self.extractor,
            interval=float(self.cfg.get("poll.interval_seconds", DEFAULT_INTERVAL_SECONDS)),
            jitter=float(self.cfg.get("poll.jitter_seconds", DEFAULT_JITTER_SECONDS)),
        )

        # 8. Facade（业务层唯一对外出口）
        self.facade = CanvasTaskMonitorFacade(self)

        logger.info(
            "Container 装配完成：db=%s sources=%s", self.db.path, [c.name for c in self.connectors]
        )

    def _build_connectors(self) -> list[BaseConnector]:
        """按 poll.sources 装配连接器。

        只对**启用的**连接器做必需字段校验。未启用的连接器（例如只跑 IMAP 的用户，
        mail.graph 字段全空）不校验，避免误伤。错误信息必须同时说明"缺哪个字段"
        和"怎么修"——用户是学生不是运维，容错成本高。
        """
        out: list[BaseConnector] = []
        sources = self.cfg.get("poll.sources") or []

        if "canvas" in sources:
            canvas_cfg = self.cfg.get("canvas") or {}
            missing = missing_required(canvas_cfg, ["base_url", "token"])
            if missing:
                fields = "、".join(f"canvas.{key}" for key in missing)
                raise RuntimeError(
                    f"Canvas 连接器缺少必需配置：{fields}。请检查 .env 或 config/settings.yaml。"
                )
            out.append(CanvasConnector(canvas_cfg))

        if "mail" in sources:
            provider = self.cfg.get("mail.provider", "graph")
            if provider == "graph":
                graph_cfg = self.cfg.get("mail.graph") or {}
                missing = missing_required(
                    graph_cfg, ["tenant_id", "client_id", "client_secret", "user"]
                )
                if missing:
                    fields = "、".join(f"mail.graph.{key}" for key in missing)
                    raise RuntimeError(
                        f"Graph 邮箱连接器缺少必需配置：{fields}。"
                        f"若租户拿不到应用权限，请在 settings.yaml 把 mail.provider 改成 'imap'。"
                    )
                out.append(GraphMailConnector(graph_cfg))
            elif provider == "imap":
                imap_cfg = self.cfg.get("mail.imap") or {}
                missing = missing_required(imap_cfg, ["host", "username", "password"])
                if missing:
                    fields = "、".join(f"mail.imap.{key}" for key in missing)
                    raise RuntimeError(
                        f"IMAP 邮箱连接器缺少必需配置：{fields}。"
                        f"请检查 .env 或 config/settings.yaml。"
                    )
                out.append(ImapMailConnector(imap_cfg))
            else:
                raise RuntimeError(
                    f"未知的 mail.provider：{provider!r}。允许值：'graph' | 'imap'。"
                )

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

