"""测试公共夹具：内存数据库 + 假 provider。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401  注册全部表元数据
from app.models.account import Account
from app.services.providers.base import FolderInfo, MailProvider, RawMessage


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def account(session) -> Account:
    acc = Account(
        name="测试邮箱",
        email="tester@example.com",
        provider="qq",
        auth_mode="imap",
        imap_host="imap.qq.com",
        imap_port=993,
        username="tester@example.com",
        secret_enc="",
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


def make_message(uid: int, subject: str = "", message_id: str | None = None, body: str = "") -> RawMessage:
    return RawMessage(
        uid=uid,
        flags=[],
        message_id=message_id or f"<msg-{uid}@example.com>",
        subject=subject or f"测试邮件 {uid}",
        from_name="张三",
        from_email="zhangsan@corp.com",
        to_list=[{"name": "我", "email": "tester@example.com"}],
        # 时间保持在最近，避免被「首次同步回看 N 天」过滤掉
        date_utc=datetime.now(timezone.utc) - timedelta(hours=uid),
        body_text=body or f"这是第 {uid} 封邮件的正文内容。",
        raw_bytes=None,  # 测试不落盘
    )


class FakeProvider(MailProvider):
    """内存版 provider，用于同步逻辑的单元测试。"""

    def __init__(self, account: Account, secret: str, messages: list[RawMessage], uidvalidity: int = 1000) -> None:
        super().__init__(account, secret)
        self.messages = messages
        self._uidvalidity = uidvalidity
        self.fetch_calls: list[dict] = []

    def set_uidvalidity(self, value: int) -> None:
        self._uidvalidity = value

    def connect(self) -> None:  # noqa: D102
        return

    def close(self) -> None:  # noqa: D102
        return

    def test_connection(self) -> tuple[bool, str]:  # noqa: D102
        return True, "ok"

    def list_folders(self) -> list[FolderInfo]:  # noqa: D102
        return [FolderInfo(name="INBOX", uidvalidity=self._uidvalidity, exists=len(self.messages))]

    def folder_uidvalidity(self, folder: str) -> tuple[int, int]:  # noqa: D102
        return self._uidvalidity, len(self.messages)

    def fetch_messages(self, folder, *, start_uid=None, since=None, limit=None):  # noqa: D102, ANN001
        self.fetch_calls.append({"start_uid": start_uid, "since": since, "limit": limit})
        msgs = [m for m in self.messages if start_uid is None or m.uid >= start_uid]
        msgs.sort(key=lambda m: m.uid)
        if since is not None:
            msgs = [m for m in msgs if m.date_utc is None or m.date_utc >= since]
        if limit is not None:
            msgs = msgs[:limit]
        yield from msgs