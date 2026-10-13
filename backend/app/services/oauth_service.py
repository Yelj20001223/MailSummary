"""OAuth2 授权码流程（Gmail / Microsoft Graph）。

纯 httpx 实现，不引入 google-auth / msal 依赖：既保持轻量，又能让单元测试
通过注入 ``httpx.MockTransport`` 完全离线运行。

令牌包（bundle）统一结构（整体加密后存入 ``Account.oauth_token_enc``）::

    {"access_token": str, "refresh_token": str, "expires_at": int(epoch秒),
     "scopes": str, "token_type": str}
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from app.services.providers.base import AuthError, ProviderError

GMAIL = "gmail"
OUTLOOK = "outlook"

# 各 provider 默认申请的最小只读权限
_SCOPES: dict[str, list[str]] = {
    GMAIL: [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
    OUTLOOK: [
        "offline_access",  # 必须：否则不返回 refresh_token
        "https://graph.microsoft.com/Mail.Read",
        "https://graph.microsoft.com/User.Read",
    ],
}

_AUTHORIZE_ENDPOINT = {
    GMAIL: "https://accounts.google.com/o/oauth2/v2/auth",
    OUTLOOK: "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
}
_TOKEN_ENDPOINT = {
    GMAIL: "https://oauth2.googleapis.com/token",
    OUTLOOK: "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}

SUPPORTED = tuple(_AUTHORIZE_ENDPOINT)


def scopes_for(provider: str) -> list[str]:
    return list(_SCOPES.get(provider, []))


def callback_redirect_uri(provider: str, base_url: str) -> str:
    """拼出 OAuth 回调地址。

    微软只接受 https 或 ``http://localhost`` 形式的回环回调，**不接受 127.0.0.1**，
    否则注册应用时会报「必须以 HTTPS 或 http://localhost 开头」，授权时也会提示
    redirect_uri 不匹配。这里对 Outlook 统一改用 localhost。
    """
    base = base_url.rstrip("/")
    if provider == OUTLOOK:
        base = base.replace("127.0.0.1", "localhost")
    return f"{base}/api/oauth/{provider}/callback"


def build_authorize_url(
    provider: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    extra_scopes: list[str] | None = None,
) -> str:
    """构造用户授权页 URL（前端在新标签页打开）。"""
    if provider not in _AUTHORIZE_ENDPOINT:
        raise ProviderError(f"不支持的 OAuth provider：{provider}")
    scopes = scopes_for(provider) + list(extra_scopes or [])
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if provider == GMAIL:
        # 离线访问 + 强制同意，确保每次都能拿到 refresh_token
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    else:
        params["response_mode"] = "query"
    return f"{_AUTHORIZE_ENDPOINT[provider]}?{urlencode(params)}"


def _post_token(provider: str, data: dict, *, client: httpx.Client | None = None) -> dict:
    endpoint = _TOKEN_ENDPOINT.get(provider)
    if not endpoint:
        raise ProviderError(f"不支持的 OAuth provider：{provider}")
    owns = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.post(endpoint, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise AuthError(f"OAuth 令牌请求失败：{exc}") from exc
    finally:
        if owns:
            http.close()
    if resp.status_code >= 400:
        raise AuthError(f"OAuth 令牌端点返回 {resp.status_code}：{resp.text[:300]}")
    return resp.json()


def build_bundle(payload: dict, *, prev_refresh: str | None = None) -> dict:
    """把令牌端点的响应规整为统一的 bundle。"""
    expires_at = 0
    expires_in = payload.get("expires_in")
    try:
        if expires_in is not None:
            expires_at = int(time.time()) + int(expires_in)
    except (TypeError, ValueError):
        expires_at = 0
    return {
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token") or prev_refresh or "",
        "expires_at": expires_at,
        "scopes": payload.get("scope", ""),
        "token_type": payload.get("token_type", "Bearer"),
    }


def exchange_code(
    provider: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """授权码 → 令牌包。"""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return build_bundle(_post_token(provider, data, client=client))


def refresh_access_token(
    provider: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """用 refresh_token 换取新的 access_token（原 refresh_token 无返回时保留）。"""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    payload = _post_token(provider, data, client=client)
    return build_bundle(payload, prev_refresh=refresh_token)


def token_is_expired(bundle: dict | None, *, skew: int = 60) -> bool:
    """令牌是否已过期（留 ``skew`` 秒提前刷新余量）。

    无 ``expires_at`` 时返回 False，交给服务端 401 触发刷新。
    """
    if not bundle or not bundle.get("access_token"):
        return True
    try:
        expires_at = int(bundle.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    if expires_at <= 0:
        return False
    return time.time() >= (expires_at - skew)