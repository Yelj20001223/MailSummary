"""OAuth2 服务单元测试：授权 URL、授权码换令牌、刷新、过期判断。"""
from __future__ import annotations

import httpx
import pytest

from app.services import oauth_service
from app.services.providers.base import AuthError, ProviderError


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_authorize_url_gmail():
    url = oauth_service.build_authorize_url("gmail", "cid", "http://x/cb", "st")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url
    assert "access_type=offline" in url  # 保证拿到 refresh_token
    assert "prompt=consent" in url
    assert "state=st" in url


def test_build_authorize_url_outlook_contains_offline_access():
    url = oauth_service.build_authorize_url("outlook", "cid", "http://x/cb", "st")
    assert "login.microsoftonline.com" in url
    assert "offline_access" in url
    assert "response_mode=query" in url


def test_callback_redirect_uri_uses_localhost_for_outlook():
    # 微软不接受 127.0.0.1，必须换成 localhost
    assert oauth_service.callback_redirect_uri("outlook", "http://127.0.0.1:8000") == (
        "http://localhost:8000/api/oauth/outlook/callback"
    )
    # gmail 保持原样（Google 两种回环地址都接受，注册的是什么就用什么）
    assert oauth_service.callback_redirect_uri("gmail", "http://127.0.0.1:8000") == (
        "http://127.0.0.1:8000/api/oauth/gmail/callback"
    )


def test_build_authorize_url_rejects_unknown_provider():
    with pytest.raises(ProviderError):
        oauth_service.build_authorize_url("yahoo", "cid", "http://x/cb", "st")


def test_exchange_code_parses_bundle():
    def handler(request):
        assert request.url.host == "oauth2.googleapis.com"
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=the-code" in body
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": "s",
                "token_type": "Bearer",
            },
        )

    bundle = oauth_service.exchange_code(
        "gmail", "cid", "cs", "the-code", "http://x/cb", client=_client(handler)
    )
    assert bundle["access_token"] == "at"
    assert bundle["refresh_token"] == "rt"
    assert bundle["expires_at"] > 0
    assert oauth_service.token_is_expired(bundle) is False


def test_refresh_keeps_prev_refresh_token_when_absent():
    def handler(request):
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

    bundle = oauth_service.refresh_access_token(
        "outlook", "cid", "cs", "rt-old", client=_client(handler)
    )
    assert bundle["access_token"] == "at2"
    assert bundle["refresh_token"] == "rt-old"  # 端点未返回时保留旧的


def test_token_endpoint_error_raises_auth_error():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(AuthError):
        oauth_service.exchange_code(
            "gmail", "cid", "cs", "bad", "http://x/cb", client=_client(handler)
        )


def test_token_is_expired_variants():
    assert oauth_service.token_is_expired({}) is True
    assert oauth_service.token_is_expired({"access_token": "x"}) is False  # 无过期时间交给 401
    assert oauth_service.token_is_expired({"access_token": "x", "expires_at": 0}) is False
    assert oauth_service.token_is_expired({"access_token": "x", "expires_at": 1}) is True