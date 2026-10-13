"""游标型增量同步（_sync_delta）单元测试：回填、续拉、去重、游标失效回退。"""
from __future__ import annotations

from sqlmodel import select

from app.models.message import Message
from app.models.sync_state import SyncState
from app.services.providers.base import DeltaBatch, FolderInfo, MailProvider
from app.services.sync_service import SyncService
from tests.conftest import make_message


class FakeDeltaProvider(MailProvider):
    """内存版游标 provider：按调用次序依次返回预设的 DeltaBatch。"""

    def __init__(self, account, secret, batches: list[DeltaBatch]) -> None:
        super().__init__(account, secret)
        self.batches = batches
        self.calls: list[dict] = []

    def supports_delta(self) -> bool:  # noqa: D102
        return True

    def test_connection(self) -> tuple[bool, str]:  # noqa: D102
        return True, "ok"

    def list_folders(self) -> list[FolderInfo]:  # noqa: D102
        return []

    def fetch_messages(self, folder, *, start_uid=None, since=None, limit=None):  # noqa: ANN001, D102
        return iter(())

    def fetch_delta(self, folder, *, cursor=None, since=None, limit=None):  # noqa: ANN001, D102
        self.calls.append({"cursor": cursor, "since": since, "limit": limit})
        idx = min(len(self.calls) - 1, len(self.batches) - 1)
        return self.batches[idx]


def _svc(session, provider) -> SyncService:
    return SyncService(session, provider_factory=lambda _a, _s: provider)


def _count(session) -> int:
    return len(session.exec(select(Message)).all())


def test_initial_delta_sync_backfills_and_stores_cursor(session, account):
    provider = FakeDeltaProvider(
        account,
        "secret",
        [DeltaBatch(messages=[make_message(1), make_message(2)], cursor="c1", is_initial=True)],
    )
    result = _svc(session, provider)._sync_folder(account, provider, "INBOX", full=False)

    assert result.inserted == 2
    assert result.fetched == 2
    assert result.delta_token == "c1"
    assert _count(session) == 2

    state = session.exec(select(SyncState)).first()
    assert state.delta_token == "c1"
    assert state.initial_done is True
    assert provider.calls[0]["cursor"] is None  # 首次无游标
    assert provider.calls[0]["since"] is not None  # 首次带回看窗口


def test_second_delta_sync_passes_cursor_and_dedups(session, account):
    provider = FakeDeltaProvider(
        account,
        "secret",
        [
            DeltaBatch(messages=[make_message(1), make_message(2)], cursor="c1", is_initial=True),
            # 第二次：一封新 + 一封重复
            DeltaBatch(messages=[make_message(2), make_message(3)], cursor="c2"),
        ],
    )
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)
    result = svc._sync_folder(account, provider, "INBOX", full=False)

    assert provider.calls[1]["cursor"] == "c1"  # 使用上次保存的游标
    assert provider.calls[1]["since"] is None  # 增量无需回看窗口
    assert result.inserted == 1
    assert result.skipped_dup == 1
    assert result.delta_token == "c2"
    assert _count(session) == 3


def test_cursor_invalidation_triggers_full_resync(session, account):
    provider = FakeDeltaProvider(
        account,
        "secret",
        [
            DeltaBatch(messages=[make_message(1)], cursor="c1", is_initial=True),
            DeltaBatch(needs_full_resync=True),  # 游标失效
            DeltaBatch(messages=[make_message(1), make_message(2)], cursor="c2", is_initial=True),
        ],
    )
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)
    result = svc._sync_folder(account, provider, "INBOX", full=False)

    assert result.rebuilt is True
    assert result.delta_token == "c2"
    assert provider.calls[2]["cursor"] is None  # 回退全量时清空游标
    assert _count(session) == 2  # 重复的 1 被封顶，新增 2


def test_full_flag_ignores_existing_cursor(session, account):
    provider = FakeDeltaProvider(
        account,
        "secret",
        [
            DeltaBatch(messages=[make_message(1)], cursor="c1", is_initial=True),
            DeltaBatch(messages=[make_message(2)], cursor="c2", is_initial=True),
        ],
    )
    svc = _svc(session, provider)
    svc._sync_folder(account, provider, "INBOX", full=False)
    result = svc._sync_folder(account, provider, "INBOX", full=True)

    assert provider.calls[1]["cursor"] is None  # full=True 强制丢弃游标
    assert result.rebuilt is True
    assert result.delta_token == "c2"
    assert _count(session) == 2