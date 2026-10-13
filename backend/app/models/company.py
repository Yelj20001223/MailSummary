"""公司 / 联系人模型（P3 公司聚合的核心表）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from app.core.utils import utcnow


class Company(SQLModel, table=True):
    """发件公司实体。域名是主键候选。"""

    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="公司展示名")
    domains: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    logo: str | None = None
    industry: str | None = None
    notes: str | None = None
    starred: bool = False

    message_count: int = 0
    last_message_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    """联系人。多个联系人可归入同一公司（多对一）。"""

    __tablename__ = "contacts"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    name: str = ""
    company_id: int | None = Field(default=None, foreign_key="companies.id", index=True)
    first_seen_at: datetime = Field(default_factory=utcnow)
    message_count: int = 0