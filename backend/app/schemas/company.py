"""公司聚合相关 schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.classify import CategoryCount, TagCount
from app.schemas.message import MessageRead


class CompanyCard(BaseModel):
    id: int
    name: str
    domains: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    logo: str | None = None
    industry: str | None = None
    notes: str | None = None
    starred: bool = False
    message_count: int = 0
    unread: int = 0
    last_message_at: datetime | None = None


class CompanyUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    starred: bool | None = None
    industry: str | None = None
    logo: str | None = None
    aliases: list[str] | None = None
    domains: list[str] | None = None


class ContactRead(BaseModel):
    id: int
    email: str
    name: str = ""
    message_count: int = 0


class AttachmentRead(BaseModel):
    filename: str
    size: int = 0
    mime: str = ""
    message_id: int


class CompanySummary(BaseModel):
    text: str = ""
    total: int = 0
    unread: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    contacts: list[ContactRead] = Field(default_factory=list)
    categories: list[CategoryCount] = Field(default_factory=list)
    tags: list[TagCount] = Field(default_factory=list)
    key_entities: dict = Field(default_factory=dict)
    attachment_count: int = 0


class CompanyDetail(BaseModel):
    company: CompanyCard
    contacts: list[ContactRead] = Field(default_factory=list)
    messages: list[MessageRead] = Field(default_factory=list)
    attachments: list[AttachmentRead] = Field(default_factory=list)
    summary: CompanySummary


class CompanyMergeRequest(BaseModel):
    primary_id: int
    merge_ids: list[int] = Field(default_factory=list)


class CompanyMergeResult(BaseModel):
    company: CompanyCard
    merged: int


class ReaggregateResult(BaseModel):
    processed: int