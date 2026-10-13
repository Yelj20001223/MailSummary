"""账号相关 schema 与 provider 预设。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class ProviderPreset(BaseModel):
    """provider 元信息，供前端『添加账号』表单动态渲染。"""

    provider: str
    label: str
    auth_mode: str = "imap"
    imap_host: str = ""
    imap_port: int = 993
    use_ssl: bool = True
    credential_label: str = "授权码"
    credential_hint: str = ""
    help_url: str = ""


# 常见服务商预设（P0 支持 imap 系）
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "qq": ProviderPreset(
        provider="qq",
        label="QQ 邮箱",
        imap_host="imap.qq.com",
        imap_port=993,
        credential_label="授权码",
        credential_hint="设置 → 账户 → POP3/IMAP/SMTP 服务 → 开启 IMAP 并生成授权码",
        help_url="https://service.mail.qq.com/detail/0/75",
    ),
    "163": ProviderPreset(
        provider="163",
        label="163 邮箱",
        imap_host="imap.163.com",
        imap_port=993,
        credential_label="授权码",
        credential_hint="设置 → POP3/SMTP/IMAP → 开启 IMAP 服务并获取授权码",
        help_url="https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2",
    ),
    "exmail": ProviderPreset(
        provider="exmail",
        label="企业微信邮箱",
        imap_host="imap.exmail.qq.com",
        imap_port=993,
        credential_label="登录密码 / 授权码",
        credential_hint="企业微信邮箱管理后台开启 IMAP 后使用密码或专用密码",
        help_url="https://work.weixin.qq.com/",
    ),
    "outlook": ProviderPreset(
        provider="outlook",
        label="Outlook / Microsoft 365",
        auth_mode="graph",
        imap_host="outlook.office365.com",
        imap_port=993,
        credential_label="应用密码（可留空改用 OAuth2）",
        credential_hint="推荐：填写 Azure 应用的 Client ID/Secret 后点『连接并授权』走 Microsoft Graph + OAuth2；也可填应用密码走 IMAP 降级",
        help_url="https://learn.microsoft.com/graph/auth-register-app-v2",
    ),
    "gmail": ProviderPreset(
        provider="gmail",
        label="Gmail",
        auth_mode="gmail_api",
        imap_host="imap.gmail.com",
        imap_port=993,
        credential_label="应用专用密码（可留空改用 OAuth2）",
        credential_hint="推荐：填写 Google Cloud 的 Client ID/Secret 后点『连接并授权』走 Gmail API + OAuth2；也可填应用专用密码走 IMAP 降级",
        help_url="https://developers.google.com/gmail/api/quickstart/python",
    ),
}


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    email: EmailStr
    provider: str
    secret: str = Field(
        default="",
        description="授权码 / 应用专用密码，明文传入，落库前加密；走 OAuth2 的账号可留空",
    )
    username: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    use_ssl: bool | None = None
    color: str | None = None

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        if v not in PROVIDER_PRESETS:
            raise ValueError(f"不支持的 provider：{v}，可选：{list(PROVIDER_PRESETS)}")
        return v


class AccountUpdate(BaseModel):
    name: str | None = None
    secret: str | None = Field(default=None, description="如填写则更新凭证")
    color: str | None = None
    is_active: bool | None = None


class AccountTestRequest(BaseModel):
    provider: str
    email: EmailStr
    secret: str
    username: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    use_ssl: bool | None = None


class AccountRead(BaseModel):
    id: int
    name: str
    email: str
    provider: str
    provider_label: str = ""
    auth_mode: str
    imap_host: str
    imap_port: int
    use_ssl: bool
    color: str
    is_active: bool
    oauth_client_id: str = ""
    oauth_connected: bool = False
    oauth_connected_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_status: str = "idle"
    last_error: str | None = None
    unread_count: int = 0
    created_at: datetime


class OAuthAuthorizeRequest(BaseModel):
    """发起 OAuth2 授权：目标账号 + 应用注册得到的 Client ID / Secret。"""

    account_id: int
    client_id: str = Field(min_length=1, description="OAuth 应用 Client ID")
    client_secret: str = Field(default="", description="OAuth 应用 Client Secret（公开客户端可留空）")