"""邮件相关 schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MessageRead(BaseModel):
    id: int
    account_id: int
    account_email: str | None = None
    folder: str
    subject: str
    from_name: str
    from_email: str
    date_utc: datetime | None = None
    is_read: bool
    is_starred: bool
    is_archived: bool
    has_attachment: bool
    importance_score: int
    category: str
    tags: list[str] = Field(default_factory=list)
    preview: str = ""


class MessageDetail(MessageRead):
    to: list[dict] = Field(default_factory=list)
    cc: list[dict] = Field(default_factory=list)
    body_text: str = ""
    body_html: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    company_id: int | None = None
    message_id: str | None = None


class MessageListResponse(BaseModel):
    total: int
    unread_total: int
    items: list[MessageRead]


class SyncResultOut(BaseModel):
    account_id: int
    account_email: str
    folder: str
    fetched: int = 0
    inserted: int = 0
    skipped_dup: int = 0
    uidvalidity: int = 0
    last_uid: int = 0
    rebuilt: bool = False
    status: str = "ok"
    error: str | None = None