"""数据库层。"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

settings.ensure_dirs()

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    """SQLite 参数：外键约束 + WAL 提升并发读写。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db() -> None:
    """建表。导入 models 以触发元数据注册，再补齐既有表的新增列。"""
    from app import models  # noqa: F401  确保所有表被注册
    from app.db.migrate import apply_migrations

    SQLModel.metadata.create_all(engine)
    apply_migrations(engine)


def get_session():  # noqa: ANN201 - FastAPI 依赖
    """FastAPI 依赖注入用的会话生成器。"""
    with Session(engine) as session:
        yield session


def create_all_for_test(target_engine: Engine) -> None:
    """测试用：在指定 engine 上建表。"""
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(target_engine)