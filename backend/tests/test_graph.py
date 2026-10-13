"""Microsoft Graph provider 单元测试：deltaLink 回填/增量、游标过期、附件元数据。"""
from __future__ import annotations

import json
import time

import httpx

from app.services.providers.graph import GraphProvider

_DELTA = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _bundle(access="at", expires_in=3600) -> str:
    return json.dumps(
        {"access_token": access, "refresh_token": "rt", "expires_at": int(time.time()) + expires_in}
    )


def _message(mid: str, subject: str, *, is_html=False, has_att=False) -> dict:
    return {
        "id": mid,
        "internetMessageId": f"<{mid}@outlook.com>",
        "subject": subject,
        "from": {"emailAddress": {"name": "李四", "address": "li@corp.com"}},
        "toRecipients": [{"emailAddress": {"name": "me", "address": "me@outlook.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-10-10T08:00:00Z",
        "body": {"contentType": "html" if is_html else "text", "content": "<p>你好</p>" if is_html else "你好"},
        "bodyPreview": "预览",
        "isRead": False,
        "flag": {"flagStatus": "flagged"},
        "hasAttachments": has_att,
    }


def test_initial_delta_returns_messages_and_delta_link(account):
    def handler(request):
        url = str(request.url)
        if url.startswith(_DELTA):
            return httpx.Response(
                200,
                json={
                    "value": [_message("g1", "第一封"), _message("g2", "第二封", is_html=True)],
                    "@odata.deltaLink": f"{_DELTA}?$deltatoken=tok-1",
                },
            )
        return httpx.Response(404)

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=None, since=None, limit=200)

    assert batch.is_initial is True
    assert batch.cursor == f"{_DELTA}?$deltatoken=tok-1"
    assert [m.subject for m in batch.messages] == ["第一封", "第二封"]
    assert batch.messages[0].from_email == "li@corp.com"
    assert batch.messages[0].is_starred is True
    assert batch.messages[1].body_html is not None
    assert "你好" in batch.messages[1].body_text


def test_incremental_uses_stored_delta_link(account):
    cursor = f"{_DELTA}?$deltatoken=tok-1"
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"value": [_message("g3", "新邮件")], "@odata.deltaLink": f"{_DELTA}?$deltatoken=tok-2"},
        )

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=cursor, limit=200)

    assert seen["url"] == cursor
    assert batch.cursor == f"{_DELTA}?$deltatoken=tok-2"
    assert [m.subject for m in batch.messages] == ["新邮件"]


def test_expired_delta_link_requests_full_resync(account):
    def handler(request):
        return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=f"{_DELTA}?$deltatoken=stale", limit=200)
    assert batch.needs_full_resync is True


def test_attachment_metadata_is_fetched(account):
    def handler(request):
        url = str(request.url)
        if "/attachments" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "合同.pdf", "size": 2048, "contentType": "application/pdf", "isInline": False}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"value": [_message("g1", "带附件", has_att=True)], "@odata.deltaLink": "d"},
        )

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=None, since=None, limit=200)

    msg = batch.messages[0]
    assert msg.has_attachment is True
    assert msg.attachments[0].filename == "合同.pdf"
    assert msg.attachments[0].size == 2048


def test_deleted_items_are_skipped(account):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [{"id": "gone", "@removed": {"reason": "deleted"}}, _message("g1", "保留")],
                "@odata.deltaLink": "d",
            },
        )

    provider = GraphProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=None, since=None, limit=200)
    assert [m.subject for m in batch.messages] == ["保留"]