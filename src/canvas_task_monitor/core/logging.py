"""日志初始化：统一日志级别与输出格式，可重复调用而不会重复挂载 handler。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 统一日志格式：时间 | 级别 | 模块 | 消息
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 标记属性：用于识别"由本模块挂载的 handler"，避免误删宿主自带 handler
_HANDLER_MARKER = "_canvas_task_monitor_handler"


def setup_logging(level: str = "INFO", log_file: str | Path | None = None) -> None:
    """初始化根 logger。

    设计说明：只清理本模块自己挂载的 handler，不动宿主（MCP / DSH）已有的日志配置，
    这样本模块作为插件被加载时不会破坏宿主的日志行为。

    :param level: 日志级别名（DEBUG / INFO / WARNING / ERROR），无法识别时回落到 INFO
    :param log_file: 可选日志文件路径，父目录不存在时自动创建；为 None 时输出到 stdout
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(_normalize_level(level))

    for handler in [h for h in root_logger.handlers if getattr(h, _HANDLER_MARKER, False)]:
        root_logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler: logging.Handler
    if log_file is None:
        handler = logging.StreamHandler(sys.stdout)
    else:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")

    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def _normalize_level(level: str | int) -> int:
    """把级别名转换为标准库级别常量，无法识别时回落到 INFO。"""
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO
