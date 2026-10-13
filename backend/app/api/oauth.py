"""OAuth2 授权路由（Gmail API / Microsoft Graph）。

流程：
1. ``POST /api/oauth/{provider}/authorize`` 保存 Client 凭证、生成 state，
   返回需在新标签页打开的 ``authorize_url``。
2. 用户同意后浏览器回到 ``GET /api/oauth/{provider}/callback``，
   本端用授权码换取令牌、加密落库，再 302 回前端首页并附带结果标记。

单机单进程：pending state 存在内存即可（带 TTL 清理）。
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.api.deps import SessionDep
from app.config import settings
from app.core.crypto import decrypt, encrypt
from app.core.logging import logger
from app.core.utils import utcnow
from app.db.session import engine
from app.models.account import Account
from app.schemas.account import OAuthAuthorizeRequest
from app.services import oauth_service

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

# state → {provider, account_id, created_at}
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 600  # 10 分钟


def _cleanup_pending() -> None:
    now = time.time()
    for key in [k for k, v in _PENDING.items() if now - v.get("created_at", 0) > _PENDING_TTL]:
        _PENDING.pop(key, None)


def _redirect_uri(provider: str) -> str:
    return oauth_service.callback_redirect_uri(provider, settings.app_public_base_url)


def _frontend_redirect(oauth_status: str, reason: str | None = None, account_id: int | None = None) -> str:
    url = f"/?oauth={oauth_status}"
    if account_id:
        url += f"&account_id={account_id}"
    if reason:
        url += f"&reason={quote(str(reason)[:200])}"
    return url


@router.post("/{provider}/authorize")
def authorize(
    provider: str,
    payload: OAuthAuthorizeRequest,
    session: SessionDep,
) -> dict:
    """保存 OAuth 应用凭证并返回授权页 URL。"""
    if provider not in oauth_service.SUPPORTED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的 provider：{provider}")
    account = session.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    if account.provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provider 与账号类型不匹配")

    account.oauth_client_id = payload.client_id
    account.oauth_client_secret_enc = encrypt(payload.client_secret)
    session.add(account)
    session.commit()

    _cleanup_pending()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {"provider": provider, "account_id": account.id, "created_at": time.time()}
    url = oauth_service.build_authorize_url(
        provider, payload.client_id, _redirect_uri(provider), state
    )
    return {"authorize_url": url, "state": state, "redirect_uri": _redirect_uri(provider)}


@router.get("/{provider}/callback")
def callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """OAuth 回调：换取令牌并跳回前端。"""
    _cleanup_pending()
    context = _PENDING.pop(state, None) if state else None

    if error:
        return RedirectResponse(_frontend_redirect("error", error_description or error))
    if provider not in oauth_service.SUPPORTED:
        return RedirectResponse(_frontend_redirect("error", "不支持的 provider"))
    if context is None or context.get("provider") != provider:
        return RedirectResponse(_frontend_redirect("error", "授权状态无效或已过期，请重试"))
    if not code:
        return RedirectResponse(_frontend_redirect("error", "缺少授权码"))

    account_id = context.get("account_id")
    with Session(engine) as session:
        account = session.get(Account, account_id) if account_id else None
        if account is None:
            return RedirectResponse(_frontend_redirect("error", "账号不存在"))
        try:
            client_secret = decrypt(account.oauth_client_secret_enc)
            bundle = oauth_service.exchange_code(
                provider,
                account.oauth_client_id,
                client_secret,
                code,
                _redirect_uri(provider),
            )
            account.oauth_token_enc = encrypt(json.dumps(bundle, ensure_ascii=False))
            account.oauth_scopes = bundle.get("scopes", "")
            account.oauth_connected_at = utcnow()
            session.add(account)
            session.commit()
            logger.info(f"[OAUTH] {provider} 授权成功（{account.email}）")
            return RedirectResponse(_frontend_redirect("ok", None, account.id))
        except Exception as exc:  # noqa: BLE001 - 授权失败要提示用户而非 500
            logger.error(f"[OAUTH] {provider} 授权失败：{exc}")
            return RedirectResponse(_frontend_redirect("error", str(exc)))