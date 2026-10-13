"""账号与 provider 元信息路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AccountDep, AccountServiceDep
from app.schemas.account import (
    PROVIDER_PRESETS,
    AccountCreate,
    AccountRead,
    AccountTestRequest,
    AccountUpdate,
)
from app.services.account_service import test_account


router = APIRouter(prefix="/api", tags=["accounts"])


def _to_read(account) -> AccountRead:  # noqa: ANN001
    preset = PROVIDER_PRESETS.get(account.provider)
    return AccountRead(
        id=account.id or 0,
        name=account.name,
        email=account.email,
        provider=account.provider,
        provider_label=preset.label if preset else account.provider,
        auth_mode=account.auth_mode,
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        use_ssl=account.use_ssl,
        color=account.color,
        is_active=account.is_active,
        oauth_client_id=account.oauth_client_id,
        oauth_connected=bool(account.oauth_token_enc),
        oauth_connected_at=account.oauth_connected_at,
        last_sync_at=account.last_sync_at,
        last_sync_status=account.last_sync_status,
        last_error=account.last_error,
        unread_count=account.unread_count,
        created_at=account.created_at,
    )


@router.get("/providers")
def list_providers() -> list[dict]:
    """供前端『添加账号』表单动态渲染。"""
    return [p.model_dump() for p in PROVIDER_PRESETS.values()]


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(service: AccountServiceDep) -> list[AccountRead]:
    return [_to_read(a) for a in service.list_accounts()]


@router.post("/accounts", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, service: AccountServiceDep) -> AccountRead:
    account = service.create(payload)
    return _to_read(account)


@router.post("/accounts/test")
def test_account_endpoint(payload: AccountTestRequest) -> dict:
    ok, message = test_account(payload)
    return {"ok": ok, "message": message}


@router.patch("/accounts/{account_id}", response_model=AccountRead)
def update_account(account: AccountDep, payload: AccountUpdate, service: AccountServiceDep) -> AccountRead:
    updated = service.update(account, payload)
    return _to_read(updated)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account: AccountDep, service: AccountServiceDep) -> None:
    service.delete(account)