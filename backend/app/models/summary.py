"""摘要模型（P4）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from app.core.utils import utcnow


class Summary(SQLModel, table=True):
    """邮件摘要与信息抽取结果。缓存键 = message_id + 正文 hash。"""

    __tablename__ = "summaries"

    id: int | None = Field(default=None, primary_key=True)
    message_pk: int = Field(foreign_key="messages.id", index=True)
    summary: str = Field(default="", sa_column=Column(Text))
    entities_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    action_items: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    urgency: str = Field(default="normal", description="low | normal | high")
    source: str = Field(default="rule", description="rule | llm")
    content_hash: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=utcnow)