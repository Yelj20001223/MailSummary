"""业务分类模型（P2 增强：分类可自定义增删改）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Category(SQLModel, table=True):
    """邮件业务分类。可在左侧栏增删改，邮件与规则按名称引用。"""

    __tablename__ = "categories"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    sort_order: int = Field(default=0, index=True)
    is_builtin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)