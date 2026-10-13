"""loguru 日志配置。所有模块统一 `from loguru import logger` 使用。"""
from __future__ import annotations

import sys

from loguru import logger

from app.config import settings

_configured = False


def setup_logging() -> None:
    """初始化日志：控制台彩色输出 + 文件轮转。幂等，可重复调用。"""
    global _configured
    if _configured:
        return

    settings.ensure_dirs()
    log_file = settings.data_dir / "app.log"

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        enqueue=True,
    )
    logger.add(
        str(log_file),
        level=settings.log_level.upper(),
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )
    _configured = True


__all__ = ["logger", "setup_logging"]