"""轻量数据库迁移：为既有 SQLite 表补齐新增列。

``SQLModel.metadata.create_all`` 只负责建新表，不会给已存在的表添加列。
本项目是单机单用户场景，用一个基于 ``PRAGMA table_info`` 的极简
``ALTER TABLE ADD COLUMN`` 补列即可，避免引入 Alembic 依赖。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.logging import logger

# 需要确保存在的列：{表名: {列名: 列类型 DDL}}
_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "accounts": {
        "oauth_client_id": "TEXT DEFAULT ''",
        "oauth_client_secret_enc": "TEXT DEFAULT ''",
        "oauth_token_enc": "TEXT DEFAULT ''",
        "oauth_scopes": "TEXT DEFAULT ''",
        "oauth_connected_at": "DATETIME",
    },
    "sync_state": {
        "delta_token": "TEXT DEFAULT ''",
    },
    "messages": {
        "inference_classification": "TEXT DEFAULT ''",
    },
}


def _existing_columns(conn, table: str) -> set[str]:  # noqa: ANN001
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def apply_migrations(engine: Engine) -> None:
    """幂等地为已存在的表补齐缺失列（表不存在则跳过，交给 create_all）。"""
    with engine.begin() as conn:
        for table, columns in _REQUIRED_COLUMNS.items():
            existing = _existing_columns(conn, table)
            if not existing:
                continue
            for name, ddl in columns.items():
                if name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info(f"[MIGRATE] {table} 新增列 {name}")