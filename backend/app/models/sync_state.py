"""增量同步状态模型。每个账号每个文件夹一条记录。"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint

from app.core.utils import utcnow


class SyncState(SQLModel, table=True):
    """记录 IMAP 增量同步断点：UIDVALIDITY + 最高已同步 UID。"""

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("account_id", "folder", name="uq_sync_account_folder"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)
    folder: str = Field(default="INBOX")

    uidvalidity: int = Field(default=0, description="UIDVALIDITY 变化需重建全量")
    last_uid: int = Field(default=0, description="最高已同步 UID（禁用序号）")

    # 游标型增量（P5）：Gmail historyId / Microsoft Graph deltaLink
    delta_token: str = Field(default="", description="Gmail historyId / Graph deltaLink 游标")

    last_sync_at: datetime | None = None
    last_error: str | None = None

    # 首次全量同步是否完成
    initial_done: bool = False

    created_at: datetime = Field(default_factory=utcnow)