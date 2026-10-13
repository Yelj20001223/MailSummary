"""Gmail provider 单元测试：首次回填、historyId 增量、游标过期、令牌自动刷新。"""
from __future__ import annotations

import base64
import json
import time

import httpx

from app.core.crypto import encrypt
from app.services.providers.gmail import GmailProvider

_API_PATH = "/gmail/v1/users/me"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _bundle(access="at", refresh="rt", expires_in=3600) -> str:
    return json.dumps(
        {"access_token": access, "refresh_token": refresh, "expires_at": int(time.time()) + expires_in}
    )


def _message(mid: str, subject: str, labels=None, msg_id="<m@example.com>") -> dict:
    return {
        "id": mid,
        "threadId": mid,
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "张三 <zhang@corp.com>"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Message-ID", "value": msg_id},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("正文内容")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>正文内容</p>")}},
            ],
        },
    }


def test_initial_delta_backfills_and_records_history_cursor(account):
    def handler(request):
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "1000", "emailAddress": "me@gmail.com"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        if path.endswith("/messages/m1"):
            return httpx.Response(200, json=_message("m1", "第一封"))
        if path.endswith("/messages/m2"):
            return httpx.Response(200, json=_message("m2", "第二封", labels=["INBOX", "STARRED"]))
        return httpx.Response(404)

    provider = GmailProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor=None, since=None, limit=200)

    assert batch.is_initial is True
    assert batch.cursor == "1000"  # 起始游标 = 回填时的 profile.historyId
    assert len(batch.messages) == 2

    m1 = batch.messages[0]
    assert m1.subject == "第一封"
    assert m1.from_email == "zhang@corp.com"
    assert m1.from_name == "张三"
    assert "正文内容" in m1.body_text
    assert m1.is_read is False  # 有 UNREAD 标签
    assert batch.messages[1].is_starred is True


def test_incremental_uses_history_and_updates_cursor(account):
    seen = {}

    def handler(request):
        path = request.url.path
        if path.endswith("/history"):
            seen["startHistoryId"] = request.url.params.get("startHistoryId")
            return httpx.Response(
                200,
                json={
                    "history": [{"id": "1", "messages": [{"id": "m9"}]}],
                    "historyId": "2000",
                },
            )
        if path.endswith("/messages/m9"):
            return httpx.Response(200, json=_message("m9", "新邮件"))
        return httpx.Response(404)

    provider = GmailProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor="1000", limit=200)

    assert seen["startHistoryId"] == "1000"
    assert batch.cursor == "2000"
    assert [m.subject for m in batch.messages] == ["新邮件"]


def test_expired_history_cursor_requests_full_resync(account):
    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(404, json={"error": {"message": "historyId too old"}})
        return httpx.Response(404)

    provider = GmailProvider(account, _bundle(), client=_client(handler))
    batch = provider.fetch_delta("INBOX", cursor="old", limit=200)
    assert batch.needs_full_resync is True


def test_expired_access_token_auto_refreshes(account):
    account.oauth_client_id = "cid"
    account.oauth_client_secret_enc = encrypt("cs")
    calls = {"refresh": 0}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            calls["refresh"] += 1
            return httpx.Response(200, json={"access_token": "new-at", "expires_in": 3600})
        assert request.headers.get("authorization") == "Bearer new-at"
        return httpx.Response(200, json={"historyId": "1", "emailAddress": "me@gmail.com"})

    provider = GmailProvider(
        account, _bundle(access="old", expires_in=-10), client=_client(handler)
    )
    ok, message = provider.test_connection()

    assert ok is True
    assert calls["refresh"] == 1
    assert account.oauth_token_enc  # 新令牌已回写账号
    assert "连接成功" in message


def test_missing_token_reports_auth_error(account):
    provider = GmailProvider(account, "", client=_client(lambda r: httpx.Response(500)))
    ok, message = provider.test_connection()
    assert ok is False
    assert "OAuth" in message