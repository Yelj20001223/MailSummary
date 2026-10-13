"""账号与 provider 元信息路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlmodel import func, select

from app.api.deps import AccountDep, AccountServiceDep
from app.models.message import Message
from app.schemas.account import (
    PROVIDER_PRESETS,
    AccountCreate,
    AccountRead,
    AccountTestRequest,
    AccountUpdate,
)
from app.services.account_service import AccountService, test_account


router = APIRouter(prefix="/api", tags=["accounts"])


def _unread_count_by_account(service: AccountService) -> dict[int, int]:
    """按账号实时统计未读数（未读且未归档）。

    账号表里的 unread_count 只在同步时刷新；读取/归档/删除邮件后不会更新，
    导致点开邮件后顶部徽标仍停在旧值。这里直接查 messages 实时统计，
    保证徽标与列表一致，无需等下一次同步。
    """
    rows = service.session.exec(
        select(Message.account_id, func.count())
        .where(Message.is_read == False, Message.is_archived == False)  # noqa: E712
        .group_by(Message.account_id)
    ).all()
    return {int(acc_id): int(cnt) for acc_id, cnt in rows}


def _to_read(account, unread_count: int | None = None) -> AccountRead:  # noqa: ANN001
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
        unread_count=account.unread_count if unread_count is None else unread_count,
        created_at=account.created_at,
    )


@router.get("/providers")
def list_providers() -> list[dict]:
    """供前端『添加账号』表单动态渲染。"""
    return [p.model_dump() for p in PROVIDER_PRESETS.values()]


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(service: AccountServiceDep) -> list[AccountRead]:
    counts = _unread_count_by_account(service)
    return [_to_read(a, counts.get(a.id or 0, 0)) for a in service.list_accounts()]


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