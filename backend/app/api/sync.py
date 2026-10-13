"""同步路由。同步是阻塞 IO，放到线程池执行，并使用独立数据库会话。"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.api.deps import AccountServiceDep
from app.db.session import engine
from app.models.account import Account
from app.schemas.message import SyncResultOut
from app.services.sync_service import SyncService


router = APIRouter(prefix="/api/sync", tags=["sync"])


class SyncRequest(BaseModel):
    account_ids: list[int] | None = None
    folder: str | None = None  # 显式指定单一文件夹（如 "INBOX"）
    folders: list[str] | None = None  # 显式指定多文件夹；两者都为空时按 provider 默认文件夹同步
    full: bool = False


def _run_sync(
    account_ids: list[int] | None,
    folder: str | None,
    folders: list[str] | None,
    full: bool,
) -> list[dict]:
    """在线程池中执行；每个账号使用独立 Session，避免跨线程共享会话。"""
    results: list[dict] = []
    with Session(engine) as session:
        service = SyncService(session)
        if account_ids:
            accounts = session.exec(select(Account).where(Account.id.in_(account_ids))).all()
        else:
            accounts = session.exec(select(Account).where(Account.is_active == True)).all()  # noqa: E712
        for account in accounts:
            if folders:
                rs = service.sync_account_folders(account, folders, full=full)
            elif folder:
                rs = [service.sync_account(account, folder=folder, full=full)]
            else:
                # 默认：由 provider 决定（收件箱 + 垃圾邮件）
                rs = service.sync_account_folders(account, full=full)
            results.extend(asdict(r) for r in rs)
    return results


@router.post("", response_model=list[SyncResultOut])
async def trigger_sync(payload: SyncRequest) -> list[SyncResultOut]:
    results = await run_in_threadpool(
        _run_sync, payload.account_ids, payload.folder, payload.folders, payload.full
    )
    return [SyncResultOut(**r) for r in results]


@router.post("/{account_id}", response_model=SyncResultOut)
async def trigger_sync_one(
    account_id: int, folder: str | None = None, full: bool = False
) -> SyncResultOut:
    results = await run_in_threadpool(_run_sync, [account_id], folder, None, full)
    if not results:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return SyncResultOut(**results[0])