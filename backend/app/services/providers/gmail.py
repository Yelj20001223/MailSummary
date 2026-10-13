"""Gmail API 提供方（OAuth2 + historyId 增量）。

只读：仅调用 ``users.getProfile``、``users.messages.list/get``、``users.history.list``、
``users.labels.list``，全程无写操作。

增量策略：
- 首次：按回看天数用 ``messages.list(q=after:...)`` 回填，并记录当前 ``profile.historyId``
  作为起始游标（保证之后 ``history.list`` 能覆盖回填期间的新邮件）。
- 之后：``history.list(startHistoryId=cursor, historyTypes=messageAdded)`` 只取新增，
  游标过期（404）时返回 ``needs_full_resync``，由上层回退全量。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Iterator

import httpx

from app.core.logging import logger
from app.core.utils import stable_int_id, strip_html
from app.models.account import Account
from app.services.providers.base import (
    AuthError,
    DeltaBatch,
    FolderInfo,
    MailProvider,
    ProviderError,
    RateLimitError,
    RawAttachment,
    RawMessage,
)

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = 30.0
_PAGE_SIZE = 100


def _headers_map(headers: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers or []:
        name = (h.get("name") or "").lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _b64url_decode(data: str) -> str:
    """Gmail 正文为 base64url 编码，缺省补齐 padding。"""
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _walk_payload(
    part: dict,
    text_parts: list[str],
    html_parts: list[str],
    attachments: list[RawAttachment],
) -> None:
    mime = part.get("mimeType") or ""
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    children = part.get("parts") or []

    if children:
        for child in children:
            _walk_payload(child, text_parts, html_parts, attachments)
        return

    if filename:
        headers = _headers_map(part.get("headers") or [])
        disposition = headers.get("content-disposition", "")
        attachments.append(
            RawAttachment(
                filename=filename,
                size=int(body.get("size") or 0),
                mime=mime or "application/octet-stream",
                content_id=(headers.get("content-id") or "").strip("<>") or None,
                is_inline="inline" in disposition.lower(),
            )
        )
        return

    data = body.get("data")
    if not data:
        return
    decoded = _b64url_decode(data)
    if mime == "text/html":
        html_parts.append(decoded)
    else:
        text_parts.append(decoded)


class GmailProvider(MailProvider):
    """基于 Gmail REST API 的只读提供方。"""

    def __init__(self, account: Account, secret: str, *, client: httpx.Client | None = None) -> None:
        super().__init__(account, secret)
        self._bundle = self._parse_bundle(secret)
        self._client = client
        self._owns_client = client is None

    # --------------------------- 令牌 ---------------------------
    @staticmethod
    def _parse_bundle(secret: str) -> dict:
        if not secret:
            return {}
        try:
            data = json.loads(secret)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def supports_delta(self) -> bool:  # noqa: D102
        return True

    def supports_oauth(self) -> bool:  # noqa: D102
        return True

    def token_bundle(self) -> dict | None:  # noqa: D102
        return dict(self._bundle)

    def _persist_bundle(self) -> None:
        """令牌刷新后写回账号（账号对象由上层持有，随其后的 commit 落库）。"""
        from app.core.crypto import encrypt

        self.account.oauth_token_enc = encrypt(json.dumps(self._bundle, ensure_ascii=False))

    def _ensure_token(self) -> None:
        if not self._bundle.get("access_token"):
            raise AuthError("尚未完成 Gmail OAuth 授权，请先在账号页点击『连接并授权』")
        from app.services.oauth_service import token_is_expired

        if token_is_expired(self._bundle):
            self._refresh()

    def _refresh(self) -> None:
        from app.core.crypto import decrypt
        from app.services.oauth_service import refresh_access_token

        refresh_token = self._bundle.get("refresh_token")
        if not refresh_token:
            raise AuthError("Gmail 令牌已过期且无 refresh_token，请重新授权")
        client_secret = decrypt(self.account.oauth_client_secret_enc)
        bundle = refresh_access_token(
            "gmail",
            self.account.oauth_client_id,
            client_secret,
            refresh_token,
            client=self.client,
        )
        self._bundle.update(bundle)
        self._persist_bundle()
        logger.info(f"[GMAIL] 已刷新 access_token（{self.account.email}）")

    # --------------------------- HTTP ---------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_TIMEOUT)
        return self._client

    def connect(self) -> None:  # noqa: D102
        self._ensure_token()

    def close(self) -> None:  # noqa: D102
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bundle.get('access_token', '')}"}

    def _get(self, path: str, params: dict | None = None, *, allow_404: bool = False) -> dict | None:
        self._ensure_token()
        url = f"{_API}{path}"
        resp = self.client.get(url, params=params, headers=self._auth_headers())
        if resp.status_code == 401:
            self._refresh()
            resp = self.client.get(url, params=params, headers=self._auth_headers())
        if resp.status_code == 429:
            raise RateLimitError("Gmail API 触发限流（429），请稍后重试")
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(f"Gmail API {resp.status_code}：{resp.text[:200]}")
        return resp.json()

    # --------------------------- 能力实现 ---------------------------
    def test_connection(self) -> tuple[bool, str]:  # noqa: D102
        try:
            data = self._get("/profile") or {}
            email = data.get("emailAddress", self.account.email)
            return True, f"Gmail API 连接成功（{email}，共 {data.get('messagesTotal', 0)} 封）"
        except (AuthError, ProviderError) as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连接异常：{exc}"

    def list_folders(self) -> list[FolderInfo]:  # noqa: D102
        data = self._get("/labels") or {}
        folders: list[FolderInfo] = []
        for lb in data.get("labels", []) or []:
            lb_id = lb.get("id") or ""
            if not lb_id or lb_id in ("CHAT", "SPAM", "TRASH"):
                continue
            folders.append(FolderInfo(name=lb_id, delimiter="/"))
        return folders

    def _profile_history_id(self) -> str:
        data = self._get("/profile") or {}
        return str(data.get("historyId", "") or "")

    def fetch_messages(  # noqa: D102
        self,
        folder: str,
        *,
        start_uid: int | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[RawMessage]:
        # Gmail 走游标增量，此处仅作兜底：复用首次回填逻辑
        yield from self._delta_initial(since=since, limit=limit).messages

    def fetch_delta(  # noqa: D102
        self,
        folder: str,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> DeltaBatch:
        self._ensure_token()
        if not cursor:
            return self._delta_initial(since=since, limit=limit)
        return self._delta_incremental(cursor, limit=limit)

    # --------------------------- 增量实现 ---------------------------
    @staticmethod
    def _query(since: datetime | None) -> str:
        if since is None:
            return ""
        return f"after:{int(since.timestamp())}"

    def _delta_initial(self, *, since: datetime | None, limit: int | None) -> DeltaBatch:
        start_history = self._profile_history_id()
        query = self._query(since)
        messages: list[RawMessage] = []
        page_token: str | None = None
        while True:
            params: dict = {"maxResults": _PAGE_SIZE}
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._get("/messages", params=params) or {}
            for item in data.get("messages", []) or []:
                if limit is not None and len(messages) >= limit:
                    break
                raw = self._get_message(item.get("id"))
                if raw is not None:
                    messages.append(raw)
            page_token = data.get("nextPageToken")
            if not page_token or (limit is not None and len(messages) >= limit):
                break
        logger.info(
            f"[GMAIL] {self.account.email} 首次回填 {len(messages)} 封，"
            f"起始 historyId={start_history}"
        )
        return DeltaBatch(messages=messages, cursor=start_history or None, is_initial=True)

    def _delta_incremental(self, cursor: str, *, limit: int | None) -> DeltaBatch:
        message_ids: list[str] = []
        seen: set[str] = set()
        newest = cursor
        page_token: str | None = None
        while True:
            params: dict = {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
                "maxResults": _PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("/history", params=params, allow_404=True)
            if data is None:
                # historyId 过期（Gmail 只保留一段时间的历史）
                logger.warning(f"[GMAIL] {self.account.email} historyId 已过期，需回退全量")
                return DeltaBatch(needs_full_resync=True)
            for rec in data.get("history", []) or []:
                for m in rec.get("messages", []) or []:
                    mid = m.get("id")
                    if mid and mid not in seen:
                        seen.add(mid)
                        message_ids.append(mid)
            newest = str(data.get("historyId", "") or newest)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        messages: list[RawMessage] = []
        for mid in message_ids:
            if limit is not None and len(messages) >= limit:
                break
            raw = self._get_message(mid)
            if raw is not None:
                messages.append(raw)
        return DeltaBatch(messages=messages, cursor=newest or None)

    def _get_message(self, message_id: str | None) -> RawMessage | None:
        if not message_id:
            return None
        data = self._get(f"/messages/{message_id}", params={"format": "full"})
        if not data:
            return None
        return self._parse_message(data)

    # --------------------------- 解析 ---------------------------
    @staticmethod
    def _parse_message(data: dict) -> RawMessage:
        payload = data.get("payload") or {}
        headers = _headers_map(payload.get("headers") or [])
        label_ids = data.get("labelIds") or []

        flags: list[str] = []
        if "UNREAD" not in label_ids:
            flags.append("\\Seen")
        if "STARRED" in label_ids:
            flags.append("\\Flagged")

        date_utc: datetime | None = None
        internal = data.get("internalDate")
        if internal:
            try:
                date_utc = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
            except (ValueError, OSError, TypeError):
                date_utc = None

        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[RawAttachment] = []
        _walk_payload(payload, text_parts, html_parts, attachments)
        body_text = "\n".join(p for p in text_parts if p).strip()
        body_html = "\n".join(html_parts).strip() or None
        if not body_text and body_html:
            body_text = strip_html(body_html)

        from_addrs = getaddresses([headers.get("from", "")])
        from_name, from_email = (from_addrs[0][0], from_addrs[0][1]) if from_addrs else ("", "")

        def _addr_list(key: str) -> list[dict]:
            return [{"name": n, "email": e} for n, e in getaddresses([headers.get(key, "")]) if e]

        gmail_id = data.get("id") or ""
        return RawMessage(
            uid=stable_int_id(f"gmail:{gmail_id}"),
            flags=flags,
            message_id=(headers.get("message-id") or "").strip() or None,
            in_reply_to=(headers.get("in-reply-to") or "").strip() or None,
            references=(headers.get("references") or "").split(),
            subject=headers.get("subject", ""),
            from_name=from_name,
            from_email=from_email,
            to_list=_addr_list("to"),
            cc_list=_addr_list("cc"),
            date_utc=date_utc,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            raw_bytes=None,  # Gmail full 格式不含原始 RFC822，不落 .eml
            has_attachment_flag=bool(attachments),
        )