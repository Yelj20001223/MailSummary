"""标签模型。"""
from __future__ import annotations

from sqlmodel import Field, SQLModel


class Label(SQLModel, table=True):
    """标签字典（待回复/待付款/合同/发票/招聘/通知/广告/垃圾...）。"""

    __tablename__ = "labels"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    color: str = "#64748b"
    kind: str = Field(default="tag", description="tag | category")


class MessageLabel(SQLModel, table=True):
    """邮件 <-> 标签 多对多关联。"""

    __tablename__ = "message_labels"

    id: int | None = Field(default=None, primary_key=True)
    message_pk: int = Field(foreign_key="messages.id", index=True)
    label_id: int = Field(foreign_key="labels.id", index=True)