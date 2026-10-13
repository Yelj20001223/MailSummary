"""邮件与附件模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel

from app.core.utils import utcnow


class Message(SQLModel, table=True):
    """一封邮件。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_account_uid", "account_id", "uid"),
        Index("ix_messages_company_date", "company_id", "date_utc"),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)

    # 增量同步标识
    uid: int = Field(default=0, description="IMAP UID")
    uidvalidity: int = Field(default=0)
    folder: str = Field(default="INBOX", index=True)
    # Outlook 焦点收件箱分区：focused / other；空串表示该 provider 无此概念（IMAP/Gmail）
    inference_classification: str = Field(default="", index=True)

    message_id: str | None = Field(default=None, index=True, description="RFC Message-ID")
    thread_id: str | None = None

    is_read: bool = False
    is_starred: bool = False
    is_archived: bool = False

    from_name: str = ""
    from_email: str = Field(default="", index=True)
    to_json: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    cc_json: list[dict] = Field(default_factory=list, sa_column=Column(JSON))

    subject: str = ""
    body_text: str = Field(default="", sa_column=Column(Text))
    body_html: str | None = Field(default=None, sa_column=Column(Text))

    date_utc: datetime | None = Field(default=None, index=True)
    has_attachment: bool = False

    importance_score: int = 0
    category: str = Field(default="其他", index=True)
    tags_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    company_id: int | None = Field(default=None, foreign_key="companies.id", index=True)
    contact_id: int | None = Field(default=None, foreign_key="contacts.id")

    raw_eml_path: str | None = None
    body_hash: str = Field(default="", index=True, description="去重指纹 SHA256")

    created_at: datetime = Field(default_factory=utcnow)


class Attachment(SQLModel, table=True):
    """附件元数据。P0 不落盘原件，只记录文件名/大小/MIME。"""

    __tablename__ = "attachments"

    id: int | None = Field(default=None, primary_key=True)
    message_pk: int = Field(foreign_key="messages.id", index=True)
    filename: str = ""
    size: int = 0
    mime: str = ""
    content_id: str | None = None
    is_inline: bool = False