"""同步逻辑单元测试：增量 / 去重 / UIDVALIDITY 变化 / 断点续传。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from app.models.message import Message
from app.models.sync_state import SyncState
from app.services.providers.base import RawMessage
from app.services.sync_service import SyncService
from tests.conftest import FakeProvider, make_message


def _svc(session, provider: FakeProvider) -> SyncService:
    return SyncService(session, provider_factory=lambda _a, _s: provider)


def _count_messages(session) -> int:
    return len(session.exec(select(Message)).all())


def test_initial_sync_inserts_all(session, account):
    """首次同步：按回看天数全量拉取并入库。"""
    provider = FakeProvider(account, "secret", [make_message(i) for i in range(1, 6)])
    result = _svc(session, provider)._sync_folder(account, provider, "INBOX", full=False)

    assert result.status == "ok"
    assert result.inserted == 5
    assert result.skipped_dup == 0
    assert result.last_uid == 5
    assert result.rebuilt is False
    assert _count_messages(session) == 5

    state = session.exec(select(SyncState)).first()
    assert state is not None
    assert state.last_uid == 5
    assert state.uidvalidity == 1000
    assert state.initial_done is True


def test_incremental_only_fetches_new_uids(session, account):
    """增量同步：只拉取 UID 大于断点的邮件，且不漏。"""
    provider = FakeProvider(account, "secret", [make_message(i) for i in range(1, 4)])
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)
    assert _count_messages(session) == 3

    # 服务器新增两封
    provider.messages.extend([make_message(4), make_message(5)])
    provider.fetch_calls.clear()
    result = svc._sync_folder(account, provider, "INBOX", full=False)

    assert result.inserted == 2
    assert result.skipped_dup == 0
    assert result.last_uid == 5
    assert _count_messages(session) == 5
    # 第二次同步的起点必须是断点 + 1
    assert provider.fetch_calls[0]["start_uid"] == 4


def test_dedup_on_full_resync(session, account):
    """强制全量重扫：靠 Message-ID 去重，不产生重复行。"""
    messages = [make_message(i) for i in range(1, 4)]
    provider = FakeProvider(account, "secret", messages)
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)

    result = svc._sync_folder(account, provider, "INBOX", full=True)
    assert result.inserted == 0
    assert result.skipped_dup == 3
    assert _count_messages(session) == 3


def test_uidvalidity_change_triggers_rebuild(session, account):
    """UIDVALIDITY 变化：必须重建全量同步，且旧邮件不重复入库。"""
    provider = FakeProvider(account, "secret", [make_message(i) for i in range(1, 4)], uidvalidity=1000)
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)

    # 服务器侧 UIDVALIDITY 改变（例如邮箱迁移/重建）
    provider.set_uidvalidity(2000)
    provider.messages.extend([make_message(10), make_message(11)])
    result = svc._sync_folder(account, provider, "INBOX", full=False)

    assert result.rebuilt is True
    assert result.uidvalidity == 2000
    # 旧 3 封按 Message-ID 去重跳过，新增 2 封
    assert result.inserted == 2
    assert result.skipped_dup == 3
    assert _count_messages(session) == 5

    state = session.exec(select(SyncState)).first()
    assert state.uidvalidity == 2000
    assert state.last_uid == 11


def test_dedup_by_body_hash_without_message_id(session, account):
    """无 Message-ID 时，退化用 body_hash 指纹去重。"""
    raw = RawMessage(
        uid=1,
        message_id=None,
        subject="无 Message-ID 的邮件",
        from_email="a@b.com",
        date_utc=datetime.now(timezone.utc),
        body_text="正文内容固定，因此指纹稳定。",
    )
    provider = FakeProvider(account, "secret", [raw])
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)
    assert _count_messages(session) == 1

    result = svc._sync_folder(account, provider, "INBOX", full=True)
    assert result.inserted == 0
    assert result.skipped_dup == 1


def test_batch_loop_covers_more_than_one_batch(session, account, monkeypatch):
    """分批拉取：超过单批上限时循环续拉，不遗漏。"""
    from app.config import settings

    monkeypatch.setattr(settings, "fetch_batch_size", 2)
    provider = FakeProvider(account, "secret", [make_message(i) for i in range(1, 8)])
    result = _svc(session, provider)._sync_folder(account, provider, "INBOX", full=False)

    assert result.inserted == 7
    assert result.last_uid == 7
    assert _count_messages(session) == 7


def test_attachment_metadata_recorded(session, account):
    """附件只记录元数据（文件名/大小/MIME）。"""
    from app.models.message import Attachment
    from app.services.providers.base import RawAttachment

    raw = make_message(1)
    raw.attachments = [RawAttachment(filename="合同.pdf", size=2048, mime="application/pdf")]
    provider = FakeProvider(account, "secret", [raw])
    _svc(session, provider)._sync_folder(account, provider, "INBOX", full=False)

    atts = session.exec(select(Attachment)).all()
    assert len(atts) == 1
    assert atts[0].filename == "合同.pdf"
    assert atts[0].size == 2048
    assert atts[0].mime == "application/pdf"

    msg = session.exec(select(Message)).first()
    assert msg.has_attachment is True