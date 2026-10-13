"""FastAPI 依赖注入。"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlmodel import Session

from app.db.session import get_session
from app.models.account import Account
from app.services.account_service import AccountService
from app.services.classify_service import ClassifyService
from app.services.company_service import CompanyService
from app.services.message_service import MessageService
from app.services.summary_service import SummaryService

SessionDep = Annotated[Session, Depends(get_session)]


def get_account_service(session: SessionDep) -> AccountService:
    return AccountService(session)


def get_message_service(session: SessionDep) -> MessageService:
    return MessageService(session)


def get_classify_service(session: SessionDep) -> ClassifyService:
    return ClassifyService(session)


def get_company_service(session: SessionDep) -> CompanyService:
    return CompanyService(session)


def get_summary_service(session: SessionDep) -> SummaryService:
    return SummaryService(session)


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]
ClassifyServiceDep = Annotated[ClassifyService, Depends(get_classify_service)]
CompanyServiceDep = Annotated[CompanyService, Depends(get_company_service)]
SummaryServiceDep = Annotated[SummaryService, Depends(get_summary_service)]


def get_account_or_404(account_id: int, session: SessionDep) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return account


AccountDep = Annotated[Account, Depends(get_account_or_404)]