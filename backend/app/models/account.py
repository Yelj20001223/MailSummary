"""邮箱账号模型。"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.core.utils import utcnow


class Account(SQLModel, table=True):
    """一个邮箱账号（provider + 认证方式 + 增量依据）。"""

    __tablename__ = "accounts"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(description="展示名，如『工作邮箱』")
    email: str = Field(index=True, description="邮箱地址")
    provider: str = Field(description="qq | 163 | exmail | outlook | gmail")
    auth_mode: str = Field(default="imap", description="imap | graph | gmail_api")

    # IMAP 连接参数
    imap_host: str = ""
    imap_port: int = 993
    use_ssl: bool = True
    username: str = Field(default="", description="登录用户名，通常等于邮箱地址")

    # 加密后的凭证（授权码 / 应用专用密码 / 令牌）
    secret_enc: str = Field(default="", repr=False)

    # OAuth2（P5：Gmail API / Microsoft Graph）
    oauth_client_id: str = ""
    oauth_client_secret_enc: str = Field(default="", repr=False)
    oauth_token_enc: str = Field(default="", repr=False, description="加密后的令牌包 JSON")
    oauth_scopes: str = ""
    oauth_connected_at: datetime | None = None

    color: str = Field(default="#4f46e5", description="界面色标")
    is_active: bool = True

    # 同步状态快照（便于界面直观展示）
    last_sync_at: datetime | None = None
    last_sync_status: str = Field(default="idle", description="idle | syncing | ok | error")
    last_error: str | None = None
    unread_count: int = 0

    created_at: datetime = Field(default_factory=utcnow)