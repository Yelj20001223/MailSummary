"""Microsoft Graph 提供方（OAuth2 + deltaLink 增量）。

只读：仅调用 ``/me``、``/me/mailFolders``、``messages/delta``、``messages/{id}/attachments``。

增量策略（微软官方推荐）：
- 首次：请求 ``.../messages/delta`` 并沿 ``@odata.nextLink`` 翻页到末尾，
  取回 ``@odata.deltaLink`` 作为游标；仅保留回看窗口内的邮件。
- 之后：直接请求上次保存的 ``deltaLink``，只取变化项；``410 Gone`` 表示游标过期，
  返回 ``needs_full_resync`` 由上层回退全量。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

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

_GRAPH = "https://graph.microsoft.com/v1.0"
_TIMEOUT = 30.0
_PAGE_SIZE = 100
_SELECT = (
    "id,internetMessageId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,body,bodyPreview,isRead,flag,hasAttachments,"
    "inferenceClassification"
)
# 我们内部使用的日志文件夹名（小写比较）→ Graph 知名文件夹名
_FOLDER_ALIAS = {
    "inbox": "inbox",
    "sent": "sentitems",
    "sentitems": "sentitems",
    "drafts": "drafts",
    "junk": "junkemail",
    "junkemail": "junkemail",
    "archive": "archive",
}


def _recipients(items: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for it in items or []:
        addr = (it or {}).get("emailAddress") or {}
        out.append({"name": addr.get("name") or "", "email": addr.get("address") or ""})
    return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Graph 返回形如 2026-10-11T09:00:00Z
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class GraphProvider(MailProvider):
    """基于 Microsoft Graph REST API 的只读提供方。"""

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

    def default_folders(self) -> list[str]:  # noqa: D102
        # Graph 的垃圾邮件是知名文件夹 junkemail，用内部名 Junk（见 _FOLDER_ALIAS）
        return ["INBOX", "Junk"]

    def token_bundle(self) -> dict | None:  # noqa: D102
        return dict(self._bundle)

    def _persist_bundle(self) -> None:
        from app.core.crypto import encrypt

        self.account.oauth_token_enc = encrypt(json.dumps(self._bundle, ensure_ascii=False))

    def _ensure_token(self) -> None:
        if not self._bundle.get("access_token"):
            raise AuthError("尚未完成 Microsoft Graph OAuth 授权，请先在账号页点击『连接并授权』")
        from app.services.oauth_service import token_is_expired

        if token_is_expired(self._bundle):
            self._refresh()

    def _refresh(self) -> None:
        from app.core.crypto import decrypt
        from app.services.oauth_service import refresh_access_token

        refresh_token = self._bundle.get("refresh_token")
        if not refresh_token:
            raise AuthError("Graph 令牌已过期且无 refresh_token，请重新授权")
        client_secret = decrypt(self.account.oauth_client_secret_enc)
        bundle = refresh_access_token(
            "outlook",
            self.account.oauth_client_id,
            client_secret,
            refresh_token,
            client=self.client,
        )
        self._bundle.update(bundle)
        self._persist_bundle()
        logger.info(f"[GRAPH] 已刷新 access_token（{self.account.email}）")

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

    def _request(
        self,
        url: str,
        params: dict | None = None,
        *,
        allow_410: bool = False,
    ) -> dict | None:
        """请求（url 可为绝对地址，如 deltaLink / nextLink）。"""
        self._ensure_token()
        resp = self.client.get(url, params=params, headers=self._auth_headers())
        if resp.status_code == 401:
            self._refresh()
            resp = self.client.get(url, params=params, headers=self._auth_headers())
        if resp.status_code == 429:
            raise RateLimitError("Graph API 触发限流（429），请稍后重试")
        if resp.status_code == 410 and allow_410:
            return None
        if resp.status_code >= 400:
            raise ProviderError(f"Graph API {resp.status_code}：{resp.text[:200]}")
        return resp.json()

    # --------------------------- 能力实现 ---------------------------
    def test_connection(self) -> tuple[bool, str]:  # noqa: D102
        try:
            data = self._request(f"{_GRAPH}/me") or {}
            who = data.get("mail") or data.get("userPrincipalName") or self.account.email
            return True, f"Microsoft Graph 连接成功（{who}）"
        except (AuthError, ProviderError) as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连接异常：{exc}"

    def list_folders(self) -> list[FolderInfo]:  # noqa: D102
        data = self._request(f"{_GRAPH}/me/mailFolders", params={"$top": 100}) or {}
        folders: list[FolderInfo] = []
        for f in data.get("value", []) or []:
            folders.append(FolderInfo(name=f.get("displayName") or f.get("id") or "", delimiter="/"))
        return folders

    def fetch_messages(  # noqa: D102
        self,
        folder: str,
        *,
        start_uid: int | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ):
        # Graph 走游标增量，此处兜底复用首次回填
        yield from self._delta_initial(folder, since=since, limit=limit).messages

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
            return self._delta_initial(folder, since=since, limit=limit)
        return self._delta_incremental(cursor, limit=limit)

    # --------------------------- 增量实现 ---------------------------
    def _delta_url(self, folder: str) -> str:
        raw = folder or "INBOX"
        name = _FOLDER_ALIAS.get(raw.lower(), raw)
        return f"{_GRAPH}/me/mailFolders/{name}/messages/delta"

    def _follow(
        self,
        first_url: str,
        *,
        first_params: dict | None,
        since: datetime | None,
        limit: int | None,
        allow_410: bool,
    ) -> tuple[list[RawMessage], str | None, bool]:
        """沿 nextLink 翻页到末尾，返回 (邮件, deltaLink, 游标是否失效)。"""
        collected: list[RawMessage] = []
        delta_link: str | None = None
        url = first_url
        params = first_params
        while url:
            data = self._request(url, params=params, allow_410=allow_410)
            if data is None:
                return [], None, True
            for item in data.get("value", []) or []:
                if "@removed" in item:
                    continue
                raw = self._parse_message(item)
                if since is not None and raw.date_utc is not None and raw.date_utc < since:
                    continue
                if item.get("hasAttachments"):
                    raw.attachments = self._fetch_attachments(item.get("id"))
                    raw.has_attachment_flag = True
                collected.append(raw)
            url = data.get("@odata.nextLink")
            delta_link = data.get("@odata.deltaLink") or delta_link
            params = None  # nextLink 已自带查询参数
        if limit is not None and len(collected) > limit:
            collected.sort(
                key=lambda m: m.date_utc or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            collected = collected[:limit]
        return collected, delta_link, False

    def _delta_initial(self, folder: str, *, since: datetime | None, limit: int | None) -> DeltaBatch:
        params = {"$select": _SELECT, "$top": _PAGE_SIZE}
        # 用 $filter 限定回看窗口，减少首屏数据量
        if since is not None:
            params["$filter"] = f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        messages, delta_link, _gone = self._follow(
            self._delta_url(folder),
            first_params=params,
            since=since,
            limit=limit,
            allow_410=False,
        )
        logger.info(f"[GRAPH] {self.account.email} 首次回填 {len(messages)} 封")
        return DeltaBatch(messages=messages, cursor=delta_link, is_initial=True)

    def _delta_incremental(self, cursor: str, *, limit: int | None) -> DeltaBatch:
        messages, delta_link, gone = self._follow(
            cursor,
            first_params=None,
            since=None,
            limit=limit,
            allow_410=True,
        )
        if gone:
            logger.warning(f"[GRAPH] {self.account.email} deltaLink 已过期，需回退全量")
            return DeltaBatch(needs_full_resync=True)
        return DeltaBatch(messages=messages, cursor=delta_link or cursor)

    def _fetch_attachments(self, message_id: str | None) -> list[RawAttachment]:
        if not message_id:
            return []
        try:
            data = self._request(
                f"{_GRAPH}/me/messages/{message_id}/attachments",
                params={"$select": "name,size,contentType,isInline"},
            )
        except ProviderError as exc:
            logger.warning(f"[GRAPH] 拉取附件元数据失败（已忽略）：{exc}")
            return []
        out: list[RawAttachment] = []
        for att in (data or {}).get("value", []) or []:
            out.append(
                RawAttachment(
                    filename=att.get("name") or "unnamed",
                    size=int(att.get("size") or 0),
                    mime=att.get("contentType") or "application/octet-stream",
                    is_inline=bool(att.get("isInline")),
                )
            )
        return out

    # --------------------------- 解析 ---------------------------
    @staticmethod
    def _parse_message(item: dict) -> RawMessage:
        frm = ((item.get("from") or {}).get("emailAddress")) or {}
        body = item.get("body") or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "text").lower() == "html":
            body_html: str | None = content
            body_text = strip_html(content)
        else:
            body_text = content
            body_html = None
        if not body_text.strip():
            body_text = item.get("bodyPreview") or ""

        flags: list[str] = []
        if item.get("isRead"):
            flags.append("\\Seen")
        if (item.get("flag") or {}).get("flagStatus") == "flagged":
            flags.append("\\Flagged")

        graph_id = item.get("id") or ""
        return RawMessage(
            uid=stable_int_id(f"graph:{graph_id}"),
            flags=flags,
            message_id=(item.get("internetMessageId") or "").strip() or None,
            subject=item.get("subject") or "",
            from_name=frm.get("name") or "",
            from_email=frm.get("address") or "",
            to_list=_recipients(item.get("toRecipients")),
            cc_list=_recipients(item.get("ccRecipients")),
            date_utc=_parse_dt(item.get("receivedDateTime")),
            body_text=body_text,
            body_html=body_html,
            raw_bytes=None,
            has_attachment_flag=bool(item.get("hasAttachments")),
            inference_classification=(item.get("inferenceClassification") or "").strip().lower(),
        )