"""统一邮件提供方接口（MailProvider）。

所有 provider（IMAP / Gmail API / Graph）必须实现同一套接口，
上层同步逻辑只依赖抽象，不关心底层协议。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from app.models.account import Account


# --------------------------- 异常 ---------------------------
class ProviderError(Exception):
    """provider 通用错误。"""


class AuthError(ProviderError):
    """认证失败（授权码错误 / 未开启 IMAP / 被风控）。"""


class RateLimitError(ProviderError):
    """触发服务商频率限制，需退避重试。"""


# --------------------------- 数据传输对象 ---------------------------
@dataclass(slots=True)
class FolderInfo:
    name: str
    uidvalidity: int = 0
    exists: int = 0
    delimiter: str = "/"


@dataclass(slots=True)
class RawAttachment:
    filename: str
    size: int
    mime: str
    content_id: str | None = None
    is_inline: bool = False


@dataclass(slots=True)
class RawMessage:
    """协议无关的邮件中间表示。"""

    uid: int
    flags: list[str] = field(default_factory=list)
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    to_list: list[dict] = field(default_factory=list)
    cc_list: list[dict] = field(default_factory=list)
    date_utc: datetime | None = None
    body_text: str = ""
    body_html: str | None = None
    attachments: list[RawAttachment] = field(default_factory=list)
    raw_bytes: bytes | None = None
    # REST API 可直接告知是否含附件（无需先拉附件元数据）；None 表示回退到 attachments 判定
    has_attachment_flag: bool | None = None
    # Outlook 焦点收件箱分区：focused / other；空串表示该 provider 无此概念（IMAP/Gmail）
    inference_classification: str = ""

    @property
    def is_read(self) -> bool:
        return "\\Seen" in self.flags

    @property
    def is_starred(self) -> bool:
        return "\\Flagged" in self.flags

    @property
    def has_attachment(self) -> bool:
        if self.has_attachment_flag is not None:
            return self.has_attachment_flag
        return len(self.attachments) > 0


@dataclass(slots=True)
class DeltaBatch:
    """游标（delta）型增量的一批结果。

    - messages：本批邮件。
    - cursor：新的游标（Gmail historyId / Graph deltaLink），供下次续拉。
    - needs_full_resync：旧游标已失效，需要回退全量回填。
    - is_initial：本次是否为首次回填（无历史游标）。
    """

    messages: list[RawMessage] = field(default_factory=list)
    cursor: str | None = None
    needs_full_resync: bool = False
    is_initial: bool = False


# --------------------------- 抽象基类 ---------------------------
class MailProvider(ABC):
    """只读邮件提供方接ロ。"""

    def __init__(self, account: Account, secret: str) -> None:
        self.account = account
        self.secret = secret

    # -- 生命周期 --
    def connect(self) -> None:  # noqa: B027 - 默认无操作
        """建立连接（IMAP 等需要显式连接）。"""

    def close(self) -> None:  # noqa: B027 - 默认无操作
        """释放连接。"""

    def __enter__(self) -> "MailProvider":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- 能力 --
    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """测试连通性与认证，返回 (是否成功, 人类可读信息)。"""

    @abstractmethod
    def list_folders(self) -> list[FolderInfo]:
        """列出文件夹及其 UIDVALIDITY。"""

    def folder_uidvalidity(self, folder: str) -> tuple[int, int]:
        """返回 (uidvalidity, exists)。非 IMAP provider 默认返回 (0, 0)。"""
        return 0, 0

    def supports_delta(self) -> bool:
        """是否为游标型增量（Gmail historyId / Graph deltaLink）。"""
        return False

    def supports_oauth(self) -> bool:
        """是否使用 OAuth2 令牌（而非 IMAP 授权码）。"""
        return False

    def token_bundle(self) -> dict | None:
        """返回当前 OAuth 令牌包（供上层判断是否需要持久化）。默认无。"""
        return None

    def resolve_folder(self, folder: str) -> str:
        """把内部通用文件夹名（INBOX / Junk）解析为 provider 实际可用名。

        默认原样返回；IMAP 等需要按服务商实际夹名转换的 provider 覆写。
        """
        return folder

    def default_folders(self) -> list[str]:
        """该账号默认要同步的文件夹（内部通用名）。默认仅收件箱。"""
        return ["INBOX"]

    def fetch_delta(
        self,
        folder: str,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> DeltaBatch:
        """游标型增量拉取。仅 ``supports_delta()`` 为 True 的 provider 实现。"""
        raise ProviderError("该 provider 不支持游标增量同步")

    @abstractmethod
    def fetch_messages(
        self,
        folder: str,
        *,
        start_uid: int | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[RawMessage]:
        """按 UID 增量拉取邮件。

        - start_uid：只返回 UID >= start_uid 的邮件（增量）。
        - since：首次同步时按 SINCE 日期限定回溯范围。
        - limit：单次最多返回多少封（分批）。
        """