"""文件夹分区（垃圾邮件 / 其他邮件）单元测试。"""
from __future__ import annotations

import json
import time

import httpx
from sqlmodel import select

from app.models.message import Message
from app.services.message_service import MessageService
from app.services.providers.graph import GraphProvider
from app.services.sync_service import SyncService
from tests.conftest import FakeProvider, make_message


def _bundle() -> str:
    return json.dumps(
        {"access_token": "at", "refresh_token": "rt", "expires_at": int(time.time()) + 3600}
    )


def _client(handler) -> httpx.Client:  # noqa: ANN001
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------- Graph provider ---------------------------
def test_graph_default_folders_include_junk(account):
    provider = GraphProvider(account, _bundle())
    assert provider.default_folders() == ["INBOX", "Junk"]
    provider.close()


def test_graph_junk_folder_maps_to_junkemail(account):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": "d"})

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    provider.fetch_delta("Junk", cursor=None, since=None, limit=10)
    assert "mailFolders/junkemail/messages/delta" in seen["url"]


def test_graph_parses_inference_classification(account):
    def handler(request):
        item = {
            "id": "g1",
            "internetMessageId": "<g1@outlook.com>",
            "subject": "其他邮件",
            "from": {"emailAddress": {"name": "x", "address": "x@outlook.com"}},
            "toRecipients": [],
            "ccRecipients": [],
            "receivedDateTime": "2026-10-10T08:00:00Z",
            "body": {"contentType": "text", "content": "hi"},
            "bodyPreview": "hi",
            "isRead": False,
            "flag": {"flagStatus": "notFlagged"},
            "hasAttachments": False,
            "inferenceClassification": "other",
        }
        return httpx.Response(200, json={"value": [item], "@odata.deltaLink": "d"})

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=None, since=None, limit=10)
    assert batch.messages[0].inference_classification == "other"


# --------------------------- 同步：多文件夹 + 去重刷新 ---------------------------
class _FolderFake(FakeProvider):
    """按文件夹返回不同邮件，并记录被同步过的文件夹。"""

    def __init__(self, account, by_folder) -> None:  # noqa: ANN001
        super().__init__(account, "secret", [])
        self.by_folder = by_folder
        self.touched: list[str] = []

    def default_folders(self) -> list[str]:  # noqa: D102
        return ["INBOX", "Junk"]

    def folder_uidvalidity(self, folder: str) -> tuple[int, int]:  # noqa: D102
        return 1000, len(self.by_folder.get(folder, []))

    def fetch_messages(self, folder, *, start_uid=None, since=None, limit=None):  # noqa: D102, ANN001
        self.touched.append(folder)
        yield from self.by_folder.get(folder, [])


def test_sync_account_folders_covers_inbox_and_junk(session, account, monkeypatch):
    provider = _FolderFake(account, {"INBOX": [make_message(1)], "Junk": [make_message(2)]})
    monkeypatch.setattr(SyncService, "_resolve_secret", lambda self, a: "secret")
    svc = SyncService(session, provider_factory=lambda _a, _s: provider)

    results = svc.sync_account_folders(account)

    assert provider.touched == ["INBOX", "Junk"]
    assert [r.folder for r in results] == ["INBOX", "Junk"]
    rows = {m.uid: m.folder for m in session.exec(select(Message)).all()}
    assert rows == {1: "INBOX", 2: "Junk"}


def test_duplicate_refreshes_folder_and_inference(session, account):
    raw = make_message(1)
    raw.inference_classification = "focused"
    provider = FakeProvider(account, "secret", [raw])
    svc = SyncService(session, provider_factory=lambda _a, _s: provider)
    svc._sync_folder(account, provider, "INBOX", full=False)

    msg = session.exec(select(Message)).first()
    assert msg.folder == "INBOX"
    assert msg.inference_classification == "focused"

    # 同一封邮件再次出现（分区变为 other、来源文件夹变为 Junk）→ 只刷新不新增
    again = make_message(1)
    again.inference_classification = "other"
    assert svc._store_message(account, again, "Junk", 0) is False

    session.refresh(msg)
    assert msg.folder == "Junk"
    assert msg.inference_classification == "other"
    assert len(session.exec(select(Message)).all()) == 1


# --------------------------- 列表过滤 ---------------------------
def test_list_messages_filters_by_inference(session, account):
    session.add(
        Message(
            account_id=account.id,
            uid=1,
            folder="INBOX",
            inference_classification="other",
            body_hash="h1",
        )
    )
    session.add(
        Message(
            account_id=account.id,
            uid=2,
            folder="INBOX",
            inference_classification="focused",
            body_hash="h2",
        )
    )
    session.commit()

    res = MessageService(session).list_messages(inference="other")
    assert res.total == 1
    assert res.items[0].folder == "INBOX"


def test_list_messages_filters_by_chinese_tag(session, account):
    """标签以 JSON 存储（中文会被转义成 \\uXXXX），筛选中文标签必须能命中。"""
    session.add(
        Message(account_id=account.id, uid=1, tags_json=["待付款", "通知"], body_hash="t1")
    )
    session.add(Message(account_id=account.id, uid=2, tags_json=["通知"], body_hash="t2"))
    session.add(Message(account_id=account.id, uid=3, tags_json=[], body_hash="t3"))
    session.commit()

    svc = MessageService(session)
    assert svc.list_messages(tag="待付款").total == 1
    assert svc.list_messages(tag="通知").total == 2
    assert svc.list_messages(tag="广告").total == 0
